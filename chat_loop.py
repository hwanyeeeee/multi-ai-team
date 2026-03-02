#!/usr/bin/env python3
"""Interactive chat loop for the multi-AI team tmux interface.

This script runs inside the tmux input pane (pane 3) and manages
user input, @mention routing, and response display.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from config import AI_MODELS, AI_RESPONSE_TIMEOUT_SEC, CHAT_SYNTHESIS_ENABLED
from conversation import ConversationManager
from ai_worker import run_ai_in_tmux_pane, wait_for_completion, clear_pane


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


def display_response_summary(model_name: str, response: str, max_lines: int = 15) -> None:
    """Print a short summary of an AI response in the input pane."""
    label = AI_MODELS.get(model_name, {}).get("label", model_name)
    lines = response.strip().splitlines()
    preview = lines[:max_lines]
    print(f"\n--- {label} ---")
    for line in preview:
        print(f"  {line}")
    if len(lines) > max_lines:
        print(f"  ... ({len(lines) - max_lines} more lines, see pane)")
    print()


def handle_command(cmd: str, conv: ConversationManager, active_models: list[str]) -> bool:
    """Handle special /commands. Returns True if the loop should exit."""
    lower = cmd.lower().strip()

    if lower in ("/quit", "/exit"):
        print("Goodbye!")
        return True

    if lower == "/history":
        print(conv.get_history_display())
        return False

    if lower == "/clear":
        conv.clear()
        print("Conversation cleared.")
        return False

    if lower == "/models":
        print("Active models:")
        for m in active_models:
            label = AI_MODELS.get(m, {}).get("label", m)
            strengths = ", ".join(AI_MODELS.get(m, {}).get("strengths", []))
            print(f"  @{m} - {label} ({strengths})")
        return False

    if lower == "/help":
        print("Commands:")
        print("  /quit or /exit  - Exit chat")
        print("  /history        - Show conversation history")
        print("  /clear          - Clear conversation")
        print("  /models         - Show available AI models")
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
    """Main interactive chat loop."""
    conv = ConversationManager(work_dir, active_models)
    shared_dir = Path(work_dir) / "shared"
    shared_dir.mkdir(parents=True, exist_ok=True)
    msg_counter = 0

    print("=" * 50)
    print("  Multi-AI Team Chat")
    print("=" * 50)
    print(f"  Models: {', '.join(active_models)}")
    print("  Type /help for commands")
    print("  Use @model to target specific AIs")
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
            if handle_command(user_input, conv, active_models):
                break
            continue

        # Parse @mentions
        clean_msg, targets = parse_mentions(user_input, active_models)
        if not clean_msg:
            print("(Empty message after removing mentions)")
            continue

        target_models = targets if targets else active_models
        conv.add_user_message(clean_msg, targets)
        msg_counter += 1

        if targets:
            target_labels = [AI_MODELS[m]["label"] for m in target_models]
            print(f"  -> Sending to: {', '.join(target_labels)}")
        else:
            print(f"  -> Sending to all ({len(target_models)} models)")

        # Clear all target AI panes, then send prompts
        target_panes = {}
        for model_name in target_models:
            pane = pane_map.get(model_name)
            if pane:
                clear_pane(pane)
                target_panes[model_name] = pane
        time.sleep(0.2)

        output_files: dict[str, str] = {}
        for model_name, pane in target_panes.items():
            prompt = conv.build_prompt(model_name, clean_msg)
            out_file = str(shared_dir / f"chat_{msg_counter}_{model_name}.txt")
            try:
                os.remove(out_file)
            except FileNotFoundError:
                pass
            output_files[model_name] = out_file
            run_ai_in_tmux_pane(
                pane_target=pane,
                model_name=model_name,
                prompt=prompt,
                output_file=out_file,
                work_dir=work_dir,
            )

        # Wait for responses
        print("  Waiting for responses...", end="", flush=True)
        results = wait_for_completion(output_files, timeout=AI_RESPONSE_TIMEOUT_SEC)
        print(" Done!")

        # Store responses and show summaries
        for model_name, response in results.items():
            conv.add_ai_response(model_name, response)
            display_response_summary(model_name, response)

        # Auto-synthesis when all 3 AIs respond and synthesis is enabled
        if (
            CHAT_SYNTHESIS_ENABLED
            and targets is None
            and len(results) >= 2
            and "claude" in results
        ):
            print("  [Synthesizing responses...]")
            synth_prompt = conv.build_synthesis_prompt(clean_msg, results)
            synth_file = str(shared_dir / f"chat_{msg_counter}_synthesis.txt")
            try:
                os.remove(synth_file)
            except FileNotFoundError:
                pass

            claude_pane = pane_map.get("claude")
            if claude_pane:
                clear_pane(claude_pane)
                time.sleep(0.2)
                run_ai_in_tmux_pane(
                    pane_target=claude_pane,
                    model_name="claude",
                    prompt=synth_prompt,
                    output_file=synth_file,
                    work_dir=work_dir,
                )
                synth_result = wait_for_completion(
                    {"claude": synth_file}, timeout=AI_RESPONSE_TIMEOUT_SEC
                )
                if "claude" in synth_result:
                    print("\n=== Synthesis ===")
                    for line in synth_result["claude"].strip().splitlines()[:20]:
                        print(f"  {line}")
                    print()


def main():
    parser = argparse.ArgumentParser(description="Multi-AI Team Chat Loop")
    parser.add_argument("--session", required=True, help="tmux session name")
    parser.add_argument("--work-dir", required=True, help="Working directory")
    parser.add_argument("--models", required=True, help="Active models as JSON list")
    args = parser.parse_args()

    active_models = json.loads(args.models)

    # Build pane map from session name
    pane_map = {
        "claude": f"{args.session}.0",
        "codex": f"{args.session}.1",
        "gemini": f"{args.session}.2",
    }

    run_chat_loop(args.session, pane_map, args.work_dir, active_models)


if __name__ == "__main__":
    main()
