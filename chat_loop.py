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
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.text import Text
from rich.live import Live
from rich.table import Table

sys.path.insert(0, str(Path(__file__).parent))

from config import AI_MODELS, CHAT_SYNTHESIS_ENABLED
from ai_worker import (
    send_message_to_pane,
    capture_pane_content,
    wait_for_all_panes_idle,
    synthesize_responses,
)
from tmux_manager import update_pane_status
from conversation import ConversationLog
from orchestrator import TaskOrchestrator, BatchDiscussion

console = Console()

def parse_mentions(
    message: str, active_models: list[str]
) -> tuple[str, list[str] | None]:
    """Parse @mentions from a message.

    Returns (clean_message, targets).
    targets is None if no mentions found (broadcast to all).
    """
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
        disc = BatchDiscussion(work_dir, active_models)
        result = disc.run(topic)
        _print_synthesis(result)
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
            '  \"\"\"             - Multi-line input mode (\"\"\" to submit)\n'
            "  /help           - Show this help\n\n"
            "[bold cyan]Use @model to target specific AIs:[/bold cyan]\n"
            "  @codex analyze this code\n"
            "  @claude @gemini review this approach\n"
            "  (no mention = all AIs respond, auto-synthesis runs)"
        )
        console.print(Panel(help_text, title="Help"))
        return False

    console.print(f"[bold red]Unknown command: {cmd}. Type /help for available commands.[/bold red]")
    return False


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
    auto_synth = CHAT_SYNTHESIS_ENABLED and ("claude" in active_models)

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

        target_models = targets if targets else active_models
        log.add("user", clean_msg, targets)

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
            console.print(f"  [bold green]-> Sent to all {len(sent)} AIs[/bold green]")

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
        console.print()


def main():
    parser = argparse.ArgumentParser(description="Multi-AI Team Chat Loop")
    parser.add_argument("--session", required=True, help="tmux session name")
    parser.add_argument("--work-dir", required=True, help="Working directory")
    parser.add_argument("--models", required=True, help="Active models as JSON list")
    args = parser.parse_args()

    active_models = json.loads(args.models)

    # Build pane map from session name (panes 0-2 are AI CLIs)
    pane_map = {
        "claude": f"{args.session}.0",
        "codex": f"{args.session}.1",
        "gemini": f"{args.session}.2",
    }

    run_chat_loop(args.session, pane_map, args.work_dir, active_models)


if __name__ == "__main__":
    main()
