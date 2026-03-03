#!/usr/bin/env python3
"""Interactive chat loop for the multi-AI team tmux interface.

This script runs inside the tmux input pane (pane 3) and relays
user messages to AI CLIs running in interactive mode in other panes.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.text import Text
from rich.live import Live
from rich.table import Table

sys.path.insert(0, str(Path(__file__).parent))

from config import (
    AI_MODELS,
    CHAT_SYNTHESIS_ENABLED,
    SMART_ROUTING_KEYWORDS,
    DEFAULT_ROUTE_MODEL,
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
    capture_pane_content,
    wait_for_all_panes_idle,
    synthesize_responses,
    run_ai_cli,
    restart_interactive,
)
from tmux_manager import update_pane_status
from conversation import ConversationLog, EventStream
from orchestrator import TaskOrchestrator, BatchDiscussion

console = Console()

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


def smart_route(message: str, active_models: list[str]) -> list[str]:
    """Route message to best AI(s) based on keyword matching."""
    lower = message.lower()
    scores: dict[str, int] = {}
    for model, keywords in SMART_ROUTING_KEYWORDS.items():
        if model not in active_models:
            continue
        scores[model] = sum(1 for kw in keywords if kw in lower)

    # No matches → default model
    if not scores or max(scores.values()) == 0:
        fallback = DEFAULT_ROUTE_MODEL if DEFAULT_ROUTE_MODEL in active_models else active_models[0]
        return [fallback]

    # Return models with score > 0, sorted by score descending
    matched = [m for m, s in sorted(scores.items(), key=lambda x: -x[1]) if s > 0]
    return matched[:2]  # max 2 models


def _print_synthesis(summary: str) -> None:
    """Print formatted synthesis result."""
    console.print(Panel(summary, title="[bold magenta]AI Synthesis[/bold magenta]", border_style="magenta"))


def _broadcast_result_to_panes(
    result: str,
    context_label: str,
    pane_map: dict[str, str],
    active_models: list[str],
    work_dir: str,
) -> None:
    """Send result summary + session path to all AI panes so they have context."""
    session_dir = str(get_shared_dir(work_dir))
    # Truncate result to keep the message manageable for TUI input
    short_result = result[:500] + "..." if len(result) > 500 else result
    context_msg = (
        f"[{context_label} Result] Session directory: {session_dir} . "
        f"Summary: {short_result}"
    )
    for model in active_models:
        pane = pane_map.get(model)
        if pane:
            send_message_to_pane(pane, context_msg)


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
        table.add_column("Label", style="green")
        table.add_column("Strengths", style="yellow")
        for m in active_models:
            cfg = AI_MODELS.get(m, {})
            label = cfg.get("label", m)
            strengths = ", ".join(cfg.get("strengths", []))
            table.add_row(f"@{m}", label, strengths)
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
        orch = TaskOrchestrator(pane_map, work_dir, active_models)
        result = orch.run(task_desc)
        _print_synthesis(result)
        _broadcast_result_to_panes(result, "Task", pane_map, active_models, work_dir)
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
        disc = BatchDiscussion(work_dir, active_models, pane_map=pane_map)
        result = disc.run(topic)
        _print_synthesis(result)
        _broadcast_result_to_panes(result, "Batch", pane_map, active_models, work_dir)
        return False

    if lower == "/route":
        table = Table(title="Smart Routing Keywords")
        table.add_column("Model", style="cyan")
        table.add_column("Keywords", style="yellow")
        for model, keywords in SMART_ROUTING_KEYWORDS.items():
            table.add_row(model, ", ".join(keywords))
        table.add_row("[dim]default[/dim]", f"[dim]{DEFAULT_ROUTE_MODEL}[/dim]")
        console.print(table)
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
            "  /route          - Show smart routing keywords\n"
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
            "  (no mention = smart routing based on keywords)"
        )
        console.print(Panel(help_text, title="Help"))
        return False

    console.print(f"[bold red]Unknown command: {cmd}. Type /help for available commands.[/bold red]")
    return False


def _team_context_for(model: str, active_models: list[str]) -> str:
    """Build team context string for a single model."""
    cfg = AI_MODELS.get(model, {})
    teammates = [
        AI_MODELS[m]["label"] for m in active_models if m != model
    ]
    return INTERACTIVE_TEAM_CONTEXT.format(
        label=cfg.get("label", model),
        strengths=", ".join(cfg.get("strengths", [])),
        teammates=", ".join(teammates) if teammates else "none",
        name=model,
    )


def _summarize_for_reset(model: str, conversation: str, work_dir: str) -> str:
    """Use Claude batch mode to summarize conversation before context reset."""
    label = AI_MODELS.get(model, {}).get("label", model)
    prompt = CONTEXT_RESET_SUMMARY_PROMPT.format(
        label=label, conversation=conversation[-8000:]
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
            # Custom prompt for user input
            console.print("[bold blue]You > [/bold blue]", end="")
            user_input = sys.stdin.readline().strip()
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
            target_models = smart_route(clean_msg, active_models)  # smart routing
        log.add("user", clean_msg, targets)

        # Record topic from first user message
        if not topic_set:
            update_session_meta(topic=clean_msg[:50])
            topic_set = True

        # Send message to each target AI pane
        sent = []
        for model_name in target_models:
            pane = pane_map.get(model_name)
            if pane:
                update_pane_status(pane, model_name, "실행중")
                send_message_to_pane(pane, clean_msg)
                sent.append(model_name)

        if targets:
            labels = [AI_MODELS[m]["label"] for m in sent]
            console.print(f"  [bold green]-> Sent to:[/bold green] {', '.join(labels)}")
        else:
            labels = [AI_MODELS[m]["label"] for m in sent]
            console.print(f"  [bold green]-> Routed to:[/bold green] {', '.join(labels)}")

        events.log("message", detail=clean_msg[:80], metadata={"targets": sent})

        # Auto-synthesis when 2+ AIs respond
        if auto_synth and len(sent) >= 2 and work_dir:
            console.print("  [italic]Waiting for AI responses...[/italic]")
            pane_targets = {m: pane_map[m] for m in sent if m in pane_map}
            responses = wait_for_all_panes_idle(pane_targets)
            console.print("  [bold magenta]Synthesizing with Claude...[/bold magenta]")
            summary = synthesize_responses(responses, clean_msg, work_dir)
            _print_synthesis(summary)
            # Reset statuses to 완료 is handled by wait_for_all_panes_idle
            # But let's make sure they are set to 대기중 eventually or just leave as 완료
            for m in sent:
                if m in pane_map:
                    update_pane_status(pane_map[m], m, "대기중")
        else:
            console.print("  [italic](Watch their panes for responses)[/italic]")

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
