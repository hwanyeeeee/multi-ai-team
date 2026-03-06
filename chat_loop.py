#!/usr/bin/env python3
"""Interactive chat loop for the multi-AI team tmux interface.

This script runs inside the tmux input pane (pane 3) and relays
user messages to AI CLIs running in interactive mode in other panes.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import re
import sys
import time
import unicodedata
from pathlib import Path

try:
    import tty
    import termios
    _HAS_TERMIOS = True
except ImportError:
    _HAS_TERMIOS = False

from rich.console import Console
from rich.panel import Panel
from rich.text import Text
from rich.live import Live
from rich.table import Table

sys.path.insert(0, str(Path(__file__).parent))

from config import (
    AI_MODELS,
    CHAT_SYNTHESIS_ENABLED,
    CONTEXT_WARNING_CHARS,
    CONTEXT_RESET_CHARS,
    CONTEXT_CHECK_INTERVAL,
    CONTEXT_RESET_SUMMARY_PROMPT,
    INTERACTIVE_TEAM_CONTEXT,
    get_shared_dir,
    set_session_dir,
    update_session_meta,
)
from ai_worker import (
    send_message_to_pane,
    send_message_to_panes_parallel,
    capture_pane_content,
    wait_for_all_panes_idle,
    synthesize_responses,
    run_ai_cli,
    restart_interactive,
)
from tmux_manager import update_pane_status
from conversation import ConversationLog, EventStream
from orchestrator import TaskOrchestrator, BatchDiscussion, LiveBatchDiscussion, LiveTaskOrchestrator

console = Console()


def _read_utf8_char(fd: int) -> bytes:
    """Read one complete UTF-8 character from a file descriptor."""
    first = os.read(fd, 1)
    if not first:
        return b""
    b = first[0]
    if b < 0x80:
        return first
    elif b < 0xE0:
        remaining = 1
    elif b < 0xF0:
        remaining = 2
    else:
        remaining = 3
    data = first
    for _ in range(remaining):
        data += os.read(fd, 1)
    return data


def _char_width(ch: str) -> int:
    """Display width of a character (2 for CJK, 1 otherwise)."""
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def _redraw_tail(buf: list[str], pos: int, clear_extra: int = 0) -> None:
    """Redraw buffer content from pos to end, then reposition cursor.

    After writing the tail characters plus ``clear_extra`` spaces (to erase
    leftover characters from a deletion), moves the cursor back to ``pos``.
    """
    tail = buf[pos:]
    tail_w = sum(_char_width(c) for c in tail)
    sys.stdout.write("".join(tail))
    if clear_extra:
        sys.stdout.write(" " * clear_extra)
    # Move cursor back to pos
    back = tail_w + clear_extra
    if back:
        sys.stdout.write(f"\x1b[{back}D")
    sys.stdout.flush()


def read_line(prompt: str = "You > ") -> str:
    """Read a line of input with CJK-safe editing and arrow key navigation.

    Uses cbreak mode to read character-by-character and manually
    manages cursor movement so that wide characters (Korean, etc.)
    are properly erased on backspace inside tmux.

    Supports: left/right arrows, Home/End, Delete, insert at cursor.
    Falls back to plain input() on platforms without termios (Windows).
    """
    if not _HAS_TERMIOS or not sys.stdin.isatty():
        try:
            return input(prompt)
        except KeyboardInterrupt:
            print()  # newline after ^C
            return ""

    sys.stdout.write(prompt)
    sys.stdout.flush()

    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        # Disable ISIG so Ctrl+C is delivered as raw byte (0x03)
        # instead of generating SIGINT that kills the process
        attrs = termios.tcgetattr(fd)
        attrs[3] = attrs[3] & ~termios.ISIG  # lflags
        termios.tcsetattr(fd, termios.TCSANOW, attrs)
        buf: list[str] = []
        pos = 0  # cursor position (character index into buf)
        while True:
            raw = _read_utf8_char(fd)
            if not raw:
                raise EOFError

            # Enter
            if raw in (b"\r", b"\n"):
                sys.stdout.write("\r\n")
                sys.stdout.flush()
                return "".join(buf)

            # Backspace / DEL
            if raw in (b"\x7f", b"\x08"):
                if pos > 0:
                    removed = buf.pop(pos - 1)
                    pos -= 1
                    w = _char_width(removed)
                    sys.stdout.write("\b" * w)
                    _redraw_tail(buf, pos, clear_extra=w)
                continue

            # Ctrl+C — clear entire input line (not exit)
            if raw == b"\x03":
                # Move cursor to start, clear displayed text, reset buffer
                if pos > 0:
                    left_w = sum(_char_width(c) for c in buf[:pos])
                    sys.stdout.write("\b" * left_w)
                total_w = sum(_char_width(c) for c in buf)
                sys.stdout.write(" " * total_w + "\b" * total_w)
                sys.stdout.flush()
                buf.clear()
                pos = 0
                continue

            # Ctrl+D on empty line
            if raw == b"\x04":
                if not buf:
                    raise EOFError
                continue

            # Ctrl+U — erase to start of line
            if raw == b"\x15":
                if pos > 0:
                    left_w = sum(_char_width(c) for c in buf[:pos])
                    sys.stdout.write("\b" * left_w)
                    del buf[:pos]
                    pos = 0
                    total_w = sum(_char_width(c) for c in buf)
                    sys.stdout.write("".join(buf))
                    sys.stdout.write(" " * left_w)
                    sys.stdout.write("\b" * (total_w + left_w))
                    sys.stdout.flush()
                continue

            # Ctrl+K — erase to end of line
            if raw == b"\x0b":
                if pos < len(buf):
                    tail_w = sum(_char_width(c) for c in buf[pos:])
                    del buf[pos:]
                    sys.stdout.write(" " * tail_w)
                    sys.stdout.write("\b" * tail_w)
                    sys.stdout.flush()
                continue

            # Ctrl+A — move to start
            if raw == b"\x01":
                if pos > 0:
                    left_w = sum(_char_width(c) for c in buf[:pos])
                    sys.stdout.write(f"\x1b[{left_w}D")
                    sys.stdout.flush()
                    pos = 0
                continue

            # Ctrl+E — move to end
            if raw == b"\x05":
                if pos < len(buf):
                    right_w = sum(_char_width(c) for c in buf[pos:])
                    sys.stdout.write(f"\x1b[{right_w}C")
                    sys.stdout.flush()
                    pos = len(buf)
                continue

            # Escape sequences (arrows, Home/End, Delete)
            if raw == b"\x1b":
                next_b = os.read(fd, 1)
                if next_b == b"[":
                    # Read CSI sequence: collect until final byte (0x40-0x7E)
                    seq_bytes = bytearray()
                    while True:
                        sb = os.read(fd, 1)
                        seq_bytes += sb
                        if sb and (0x40 <= sb[0] <= 0x7E):
                            break
                    seq = bytes(seq_bytes)

                    # Left arrow: ESC[D
                    if seq == b"D" and pos > 0:
                        w = _char_width(buf[pos - 1])
                        sys.stdout.write(f"\x1b[{w}D")
                        sys.stdout.flush()
                        pos -= 1

                    # Right arrow: ESC[C
                    elif seq == b"C" and pos < len(buf):
                        w = _char_width(buf[pos])
                        sys.stdout.write(f"\x1b[{w}C")
                        sys.stdout.flush()
                        pos += 1

                    # Home: ESC[H or ESC[1~
                    elif seq in (b"H", b"1~") and pos > 0:
                        left_w = sum(_char_width(c) for c in buf[:pos])
                        sys.stdout.write(f"\x1b[{left_w}D")
                        sys.stdout.flush()
                        pos = 0

                    # End: ESC[F or ESC[4~
                    elif seq in (b"F", b"4~") and pos < len(buf):
                        right_w = sum(_char_width(c) for c in buf[pos:])
                        sys.stdout.write(f"\x1b[{right_w}C")
                        sys.stdout.flush()
                        pos = len(buf)

                    # Delete: ESC[3~
                    elif seq == b"3~" and pos < len(buf):
                        removed = buf.pop(pos)
                        w = _char_width(removed)
                        _redraw_tail(buf, pos, clear_extra=w)

                    # Up/Down arrows: ignore (no history)
                continue

            # Regular printable character
            try:
                ch = raw.decode("utf-8")
                if ch.isprintable():
                    buf.insert(pos, ch)
                    pos += 1
                    if pos == len(buf):
                        # Appending at end — just write the char
                        sys.stdout.write(ch)
                        sys.stdout.flush()
                    else:
                        # Inserting in middle — write char then redraw tail
                        sys.stdout.write(ch)
                        _redraw_tail(buf, pos)
            except UnicodeDecodeError:
                pass
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def parse_mentions(
    message: str, active_models: list[str]
) -> tuple[str, list[str] | None]:
    """Parse @mentions from a message.

    Returns (clean_message, targets).
    targets is None if no mentions found (broadcast to all).
    Special: @all explicitly targets all active models.
    """
    # Check for @all first
    if re.search(r"@all\b", message, re.IGNORECASE):
        clean = re.sub(r"@all\b", "", message, flags=re.IGNORECASE).strip()
        return clean, list(active_models)

    mentioned = []
    clean = message

    for model in active_models:
        pattern = rf"@{re.escape(model)}\b"
        if re.search(pattern, clean, re.IGNORECASE):
            mentioned.append(model)
            clean = re.sub(pattern, "", clean, flags=re.IGNORECASE)

    clean = clean.strip()
    if not mentioned:
        return clean, None
    return clean, mentioned



def _print_synthesis(summary: str) -> None:
    """Print formatted synthesis result."""
    console.print(Panel(summary, title="[bold magenta]AI Synthesis[/bold magenta]", border_style="magenta"))


def _broadcast_result_to_panes(
    result: str,
    context_label: str,
    topic: str,
    pane_map: dict[str, str],
    active_models: list[str],
    work_dir: str,
) -> None:
    """Send result summary to all AI panes so they can continue the conversation.

    This is the key mechanism for context continuity: after /batch or /task
    finishes, the interactive AI sessions receive the synthesis so the user
    can keep chatting without re-explaining everything.

    Long results are written to a file and AIs are told to read it,
    avoiding hex-mode overload and tmux rendering lag.
    """
    import subprocess
    from config import wsl_prefix

    # Write full result to a file so AIs can read it without hex overload
    result_file = f"/tmp/_team_{context_label.lower().replace(' ', '_')}_result.txt"
    subprocess.run(
        wsl_prefix() + ["bash", "-c", f"cat > {result_file}"],
        input=result.encode("utf-8"),
        capture_output=True, timeout=10,
    )

    # Send short instruction (not the full result) to each AI
    context_msg = (
        f"[Previous {context_label} Result] "
        f"Topic: {topic[:200]} . "
        f"The full synthesis result has been saved to {result_file} . "
        f"Read that file if you need the details. "
        f"Wait for the operator's next instruction."
    )
    pane_sends = []
    for model in active_models:
        pane = pane_map.get(model)
        if pane:
            pane_sends.append((pane, context_msg))
    if pane_sends:
        send_message_to_panes_parallel(pane_sends)


def handle_command(
    cmd: str,
    log: ConversationLog,
    active_models: list[str],
    pane_map: dict[str, str],
    work_dir: str = "",
) -> bool:
    """Handle special /commands. Returns True if the loop should exit."""
    lower = cmd.lower().strip()

    if lower in ("/quit", "/exit"):
        console.print("[bold yellow]Goodbye![/bold yellow]")
        return True

    if lower == "/history":
        console.print(Panel(log.display(), title="Conversation History"))
        return False

    if lower == "/clear":
        log.clear()
        console.print("[bold green]Log cleared.[/bold green]")
        return False

    if lower == "/models":
        table = Table(title="Active Models")
        table.add_column("Mention", style="cyan")
        table.add_column("Name", style="green")
        for m in active_models:
            cfg = AI_MODELS.get(m, {})
            label = cfg.get("label", m)
            table.add_row(f"@{m}", label)
        console.print(table)
        return False

    if lower == "/synth":
        console.print("[bold blue]Capturing responses from all panes...[/bold blue]")
        responses = {}
        for model in active_models:
            pane = pane_map.get(model)
            if pane:
                content = capture_pane_content(pane, lines=80)
                responses[model] = content
                label = AI_MODELS.get(model, {}).get("label", model)
                console.print(f"\n[bold]--- {label} (last ~80 lines) ---[/bold]")
                recent = content.strip().splitlines()[-10:]
                for line in recent:
                    console.print(f"  {line}")
        # Run synthesis if Claude is available
        if responses and "claude" in active_models and work_dir:
            console.print("\n[bold magenta]Synthesizing with Claude...[/bold magenta]")
            summary = synthesize_responses(responses, "(manual /synth)", work_dir)
            _print_synthesis(summary)
        return False

    if lower.startswith("/task ") or lower == "/task":
        task_desc = cmd[5:].strip()
        if not task_desc:
            console.print("[bold red]Usage: /task <description>[/bold red]")
            return False
        if not work_dir:
            console.print("[bold red]Error: work_dir not set. Cannot run /task.[/bold red]")
            return False
        orch = LiveTaskOrchestrator(pane_map, work_dir, active_models)
        result = orch.run(task_desc)
        _print_synthesis(result)
        log.add("system", f"[Task Result] {result[:500]}")
        _broadcast_result_to_panes(result, "Task", task_desc, pane_map, active_models, work_dir)
        # Reset statuses
        for m in active_models:
            if m in pane_map:
                update_pane_status(pane_map[m], m, "대기중")
        return False

    if lower.startswith("/batch ") or lower == "/batch":
        topic = cmd[6:].strip()
        if not topic:
            console.print("[bold red]Usage: /batch <topic>[/bold red]")
            return False
        if not work_dir:
            console.print("[bold red]Error: work_dir not set. Cannot run /batch.[/bold red]")
            return False
        disc = LiveBatchDiscussion(work_dir, active_models, pane_map=pane_map)
        result = disc.run(topic)
        _print_synthesis(result)
        log.add("system", f"[Batch Result] {topic}: {result[:500]}")
        _broadcast_result_to_panes(result, "Batch Discussion", topic, pane_map, active_models, work_dir)
        # Reset statuses after batch completion
        for m in active_models:
            if m in pane_map:
                update_pane_status(pane_map[m], m, "대기중")
        return False

    if lower == "/sessions":
        shared_root = Path(work_dir) / "shared"
        if not shared_root.exists():
            console.print("[italic](No sessions found)[/italic]")
            return False
        sessions = []
        for d in sorted(shared_root.iterdir(), reverse=True):
            meta_file = d / "session.json"
            if d.is_dir() and meta_file.exists():
                try:
                    meta = json.loads(meta_file.read_text(encoding="utf-8"))
                    sessions.append((d.name, meta))
                except (json.JSONDecodeError, OSError):
                    sessions.append((d.name, {}))
        if not sessions:
            console.print("[italic](No sessions found)[/italic]")
            return False
        table = Table(title="Past Sessions")
        table.add_column("Timestamp", style="cyan")
        table.add_column("Models", style="green")
        table.add_column("Topic", style="yellow")
        for name, meta in sessions:
            models = ", ".join(meta.get("models", []))
            topic = meta.get("topic", "")
            table.add_row(name, models, topic)
        console.print(table)
        return False

    if lower == "/events" or lower.startswith("/events "):
        parts = cmd.split(maxsplit=1)
        n = 20
        if len(parts) > 1:
            try:
                n = int(parts[1])
            except ValueError:
                pass
        if not work_dir:
            console.print("[bold red]Error: work_dir not set.[/bold red]")
            return False
        es = EventStream(work_dir)
        events = es.recent(n)
        if not events:
            console.print("[italic](No events recorded yet)[/italic]")
            return False
        table = Table(title=f"Recent Events (last {len(events)})")
        table.add_column("Time", style="dim", width=8)
        table.add_column("Type", style="cyan", width=10)
        table.add_column("Phase", style="yellow", width=12)
        table.add_column("Model", style="green", width=8)
        table.add_column("Detail", style="white")
        for evt in events:
            ts = evt.get("timestamp", "")[:19].split("T")[-1]
            table.add_row(
                ts,
                evt.get("type", ""),
                evt.get("phase", ""),
                evt.get("model", ""),
                evt.get("detail", "")[:60],
            )
        console.print(table)
        return False

    if lower == "/help":
        help_text = (
            "[bold cyan]Commands:[/bold cyan]\n"
            "  /quit or /exit  - Exit chat\n"
            "  /history        - Show message log\n"
            "  /clear          - Clear log\n"
            "  /models         - Show available AI models\n"
            "  /synth          - Capture AI responses & synthesize\n"
            "  /autosynth      - Toggle auto-synthesis on/off\n"
            "  /task <desc>    - Auto-orchestrate: plan, assign, execute\n"
            "  /batch <topic>  - AI-to-AI discussion until consensus\n"
            "  /events [n]     - Show recent event stream (default: 20)\n"
            "  /sessions       - List past session history\n"
            '  \"\"\"             - Multi-line input mode (\"\"\" to submit)\n'
            "  /help           - Show this help\n\n"
            "[bold cyan]Targeting AIs:[/bold cyan]\n"
            "  @codex analyze this code     - Send to specific AI\n"
            "  @claude @gemini review this  - Send to multiple AIs\n"
            "  @all what do you think?      - Send to ALL AIs\n"
            "  (no mention = broadcast to all AIs)"
        )
        console.print(Panel(help_text, title="Help"))
        return False

    console.print(f"[bold red]Unknown command: {cmd}. Type /help for available commands.[/bold red]")
    return False


def _team_context_for(model: str, active_models: list[str]) -> str:
    """Build team context string for a single model."""
    teammates = [m for m in active_models if m != model]
    return INTERACTIVE_TEAM_CONTEXT.format(
        teammates=", ".join(teammates) if teammates else "none",
        name=model,
    )


def _summarize_for_reset(model: str, conversation: str, work_dir: str) -> str:
    """Use Claude batch mode to summarize conversation before context reset."""
    prompt = CONTEXT_RESET_SUMMARY_PROMPT.format(
        name=model, conversation=conversation[-8000:]
    )
    output_file = str(get_shared_dir(work_dir) / f"{model}_reset_summary.md")
    result = run_ai_cli("claude", prompt, work_dir, output_file)
    return str(result)


def run_chat_loop(
    session_name: str,
    pane_map: dict[str, str],
    work_dir: str,
    active_models: list[str],
) -> None:
    """Main interactive chat loop.

    Simply relays user messages to AI CLIs running in interactive mode.
    Each CLI maintains its own conversation history.
    """
    log = ConversationLog(work_dir)
    events = EventStream(work_dir)
    auto_synth = CHAT_SYNTHESIS_ENABLED and ("claude" in active_models)
    msg_count = 0
    topic_set = False
    context_chars: dict[str, int] = {m: 0 for m in active_models}

    # Set initial status to 대기중
    for m in active_models:
        if m in pane_map:
            update_pane_status(pane_map[m], m, "대기중")

    welcome_text = Text.assemble(
        ("Multi-AI Team Chat\n", "bold cyan"),
        (f"Models: {', '.join(active_models)}\n", "green"),
        ("AI CLIs are running in interactive mode.\n", "white"),
        ("Auto-synthesis: ", "white"),
        ("ON" if auto_synth else "OFF", "bold green" if auto_synth else "bold red"),
        (" (/autosynth to toggle)\n", "white"),
        ("Type /help for commands", "italic white")
    )
    console.print(Panel(welcome_text, border_style="cyan"))

    while True:
        try:
            user_input = read_line("You > ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\n[bold yellow]Goodbye![/bold yellow]")
            break

        if not user_input:
            continue

        # Multi-line input mode: start with """ and end with """
        if user_input == '"""':
            console.print("[dim]  (Multi-line mode: type \"\"\" to submit)[/dim]")
            lines = []
            while True:
                try:
                    line = sys.stdin.readline()
                    if line.strip() == '"""':
                        break
                    lines.append(line.rstrip("\n"))
                except (EOFError, KeyboardInterrupt):
                    break
            user_input = "\n".join(lines).strip()
            if not user_input:
                continue

        # Handle /commands
        if user_input.startswith("/"):
            if user_input.lower().strip() == "/autosynth":
                auto_synth = not auto_synth
                state = "ON" if auto_synth else "OFF"
                style = "bold green" if auto_synth else "bold red"
                console.print(f"  Auto-synthesis: [{style}]{state}[/{style}]")
                continue
            if handle_command(user_input, log, active_models, pane_map, work_dir):
                break
            continue

        # Parse @mentions
        clean_msg, targets = parse_mentions(user_input, active_models)
        if not clean_msg:
            console.print("[italic red](Empty message after removing mentions)[/italic red]")
            continue

        if targets:
            target_models = targets                          # @mention or @all explicit
        else:
            target_models = list(active_models)              # broadcast to all
        log.add("user", clean_msg, targets)

        # Record topic from first user message
        if not topic_set:
            update_session_meta(topic=clean_msg[:50])
            topic_set = True

        # Send message to all target AI panes in parallel
        sent = []
        pane_sends = []
        for model_name in target_models:
            pane = pane_map.get(model_name)
            if pane:
                update_pane_status(pane, model_name, "실행중")
                pane_sends.append((pane, clean_msg))
                sent.append(model_name)

        if pane_sends:
            send_message_to_panes_parallel(pane_sends)

        if targets:
            labels = [AI_MODELS[m]["label"] for m in sent]
            console.print(f"  [bold green]-> Sent to:[/bold green] {', '.join(labels)}")
        else:
            labels = [AI_MODELS[m]["label"] for m in sent]
            console.print(f"  [bold green]-> Broadcast to:[/bold green] {', '.join(labels)}")

        events.log("message", detail=clean_msg[:80], metadata={"targets": sent})

        # Auto-synthesis when 2+ AIs respond
        if auto_synth and len(sent) >= 2 and work_dir:
            console.print("  [italic]Waiting for AI responses...[/italic]")
            pane_targets = {m: pane_map[m] for m in sent if m in pane_map}
            responses = wait_for_all_panes_idle(pane_targets)
            console.print("  [bold magenta]Synthesizing with Claude...[/bold magenta]")
            summary = synthesize_responses(responses, clean_msg, work_dir)
            _print_synthesis(summary)

            # Share each AI's response with the others via files (cross-session context)
            console.print("  [italic]Sharing responses across AI sessions...[/italic]")
            import subprocess as _sp
            from config import wsl_prefix as _wsl
            cross_sends = []
            for model in sent:
                pane = pane_map.get(model)
                if not pane:
                    continue
                others_text = "\n\n".join(
                    f"=== {AI_MODELS[m]['label']} ===\n{resp}"
                    for m, resp in responses.items()
                    if m != model and resp.strip()
                )
                if others_text:
                    ctx_file = f"/tmp/_team_ctx_for_{model}.txt"
                    _sp.run(
                        _wsl() + ["bash", "-c", f"cat > {ctx_file}"],
                        input=others_text.encode("utf-8"),
                        capture_output=True, timeout=10,
                    )
                    ctx_msg = (
                        f"[Team Context] Other AIs responded to '{clean_msg[:80]}'. "
                        f"Read {ctx_file} for their full responses. "
                        f"Keep this in mind for the next instruction."
                    )
                    cross_sends.append((pane, ctx_msg))

            if cross_sends:
                send_message_to_panes_parallel(cross_sends)

            for m in sent:
                if m in pane_map:
                    update_pane_status(pane_map[m], m, "대기중")
        else:
            console.print("  [italic](Watch their panes for responses)[/italic]")
            # When auto_synth is off, we don't wait — but we still need to
            # reset status after AI finishes. Use a background thread.
            if sent:
                def _reset_status_after_idle(models, pm):
                    try:
                        targets = {m: pm[m] for m in models if m in pm}
                        wait_for_all_panes_idle(targets)
                        for m in models:
                            if m in pm:
                                update_pane_status(pm[m], m, "대기중")
                    except Exception:
                        pass
                import threading
                threading.Thread(
                    target=_reset_status_after_idle,
                    args=(list(sent), dict(pane_map)),
                    daemon=True,
                ).start()

        # Context monitoring
        msg_count += 1
        if msg_count % CONTEXT_CHECK_INTERVAL == 0:
            for model in active_models:
                pane = pane_map.get(model)
                if not pane:
                    continue
                content = capture_pane_content(pane, lines=500)
                context_chars[model] = len(content)

                if context_chars[model] >= CONTEXT_RESET_CHARS:
                    console.print(f"  [bold yellow]⚠ {model} context full, resetting...[/bold yellow]")
                    summary = _summarize_for_reset(model, content, work_dir)
                    ctx = _team_context_for(model, active_models)
                    initial = f"{ctx} [Context Summary] {summary}"
                    restart_interactive(pane, model, initial_prompt=initial)
                    time.sleep(3)
                    context_chars[model] = 0
                elif context_chars[model] >= CONTEXT_WARNING_CHARS:
                    console.print(
                        f"  [bold yellow]⚠ {model} context {context_chars[model] // 1000}K chars "
                        f"(limit: {CONTEXT_RESET_CHARS // 1000}K)[/bold yellow]"
                    )
        console.print()


def main():
    parser = argparse.ArgumentParser(description="Multi-AI Team Chat Loop")
    parser.add_argument("--session", required=True, help="tmux session name")
    parser.add_argument("--work-dir", required=True, help="Working directory")
    parser.add_argument("--models", required=True, help="Active models as JSON list")
    parser.add_argument("--session-dir", default="", help="Timestamped session directory from run.py")
    args = parser.parse_args()

    active_models = json.loads(args.models)

    # Restore session directory so get_shared_dir() returns the correct path
    if args.session_dir:
        set_session_dir(args.session_dir)

    # Build pane map from session name (panes 0-2 are AI CLIs)
    pane_map = {
        "claude": f"{args.session}.0",
        "codex": f"{args.session}.1",
        "gemini": f"{args.session}.2",
    }

    run_chat_loop(args.session, pane_map, args.work_dir, active_models)


if __name__ == "__main__":
    main()
