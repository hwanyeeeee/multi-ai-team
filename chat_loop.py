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

sys.path.insert(0, str(Path(__file__).parent))

from config import AI_MODELS
from ai_worker import send_message_to_pane, capture_pane_content
from conversation import ConversationLog


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


def handle_command(
    cmd: str,
    log: ConversationLog,
    active_models: list[str],
    pane_map: dict[str, str],
) -> bool:
    """Handle special /commands. Returns True if the loop should exit."""
    lower = cmd.lower().strip()

    if lower in ("/quit", "/exit"):
        print("Goodbye!")
        return True

    if lower == "/history":
        print(log.display())
        return False

    if lower == "/clear":
        log.clear()
        print("Log cleared.")
        return False

    if lower == "/models":
        print("Active models:")
        for m in active_models:
            cfg = AI_MODELS.get(m, {})
            label = cfg.get("label", m)
            strengths = ", ".join(cfg.get("strengths", []))
            print(f"  @{m} - {label} ({strengths})")
        return False

    if lower == "/synth":
        print("Capturing responses from all panes...")
        responses = {}
        for model in active_models:
            pane = pane_map.get(model)
            if pane:
                content = capture_pane_content(pane, lines=80)
                responses[model] = content
                label = AI_MODELS.get(model, {}).get("label", model)
                print(f"\n--- {label} (last ~80 lines) ---")
                # Show last 10 lines as preview
                recent = content.strip().splitlines()[-10:]
                for line in recent:
                    print(f"  {line}")
        print()
        return False

    if lower == "/help":
        print("Commands:")
        print("  /quit or /exit  - Exit chat")
        print("  /history        - Show message log")
        print("  /clear          - Clear log")
        print("  /models         - Show available AI models")
        print("  /synth          - Capture & show all AI pane contents")
        print("  /help           - Show this help")
        print()
        print("Use @model to target specific AIs:")
        print("  @codex analyze this code")
        print("  @claude @gemini review this approach")
        print("  (no mention = all AIs respond)")
        return False

    print(f"Unknown command: {cmd}. Type /help for available commands.")
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

    print("=" * 50)
    print("  Multi-AI Team Chat")
    print("=" * 50)
    print(f"  Models: {', '.join(active_models)}")
    print("  AI CLIs are running in interactive mode.")
    print("  Each AI maintains its own conversation history.")
    print("  Type /help for commands")
    print("=" * 50)
    print()

    while True:
        try:
            user_input = input("You > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nGoodbye!")
            break

        if not user_input:
            continue

        # Handle /commands
        if user_input.startswith("/"):
            if handle_command(user_input, log, active_models, pane_map):
                break
            continue

        # Parse @mentions
        clean_msg, targets = parse_mentions(user_input, active_models)
        if not clean_msg:
            print("(Empty message after removing mentions)")
            continue

        target_models = targets if targets else active_models
        log.add("user", clean_msg, targets)

        # Send message to each target AI pane
        sent = []
        for model_name in target_models:
            pane = pane_map.get(model_name)
            if pane:
                send_message_to_pane(pane, clean_msg)
                sent.append(model_name)

        if targets:
            labels = [AI_MODELS[m]["label"] for m in sent]
            print(f"  -> Sent to: {', '.join(labels)}")
        else:
            print(f"  -> Sent to all {len(sent)} AIs")
        print("  (Watch their panes for responses)")
        print()


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
