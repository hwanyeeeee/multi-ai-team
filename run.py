#!/usr/bin/env python3
"""
Multi-AI Team Orchestrator
==========================
3개 AI (Claude, Codex, Gemini)가 tmux 분할 화면에서
자유롭게 대화하며 협업하는 시스템.

사용법:
    python run.py                   # 대화형 tmux 모드 (기본)
    python run.py --no-tmux         # 배치 모드 (tmux 없이)
    python run.py --no-tmux "task"  # 단일 작업 배치 모드

필요 환경:
    - WSL에 tmux 설치
    - claude, codex, gemini CLI 설치
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

# Add parent to path for imports
sys.path.insert(0, str(Path(__file__).parent))

from config import (
    AI_MODELS,
    ROUNDS,
    TMUX_SESSION_PREFIX,
    AI_RESPONSE_TIMEOUT_SEC,
    INTERACTIVE_TEAM_CONTEXT,
    create_session_dir,
    get_shared_dir,
    update_session_meta,
    detect_available_models,
    validate_config,
    wsl_prefix,
)
from round_manager import RoundManager
from ai_worker import run_ai_cli

# Globally track which models are available
AVAILABLE_MODELS = {}  # type: dict[str, bool]


def check_prerequisites() -> list[str]:
    """Check if required CLIs are available."""
    global AVAILABLE_MODELS
    import subprocess
    issues = []

    AVAILABLE_MODELS = detect_available_models()
    for name, found in AVAILABLE_MODELS.items():
        if not found:
            issues.append(f"{name} CLI not found in WSL")

    # Check tmux
    try:
        result = subprocess.run(
            wsl_prefix() + ["tmux", "-V"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode != 0:
            issues.append("tmux not found in WSL")
    except Exception:
        issues.append("Cannot check tmux")

    return issues


def get_active_models() -> list[str]:
    """Return list of models that are actually available."""
    if not AVAILABLE_MODELS:
        return list(AI_MODELS.keys())
    return [m for m, ok in AVAILABLE_MODELS.items() if ok]


def run_batch_mode(task: str, work_dir: str) -> str:
    """Run without tmux - sequential batch execution."""
    # Create a timestamped session directory for this batch run
    create_session_dir(work_dir)
    update_session_meta(models=get_active_models(), topic=task[:50])
    rm = RoundManager(task, work_dir, active_models=get_active_models())

    print(f"\n{'='*60}")
    print(f"  Multi-AI Team - Batch Mode")
    print(f"  Task: {task[:80]}...")
    print(f"{'='*60}\n")

    for round_idx, round_cfg in enumerate(ROUNDS):
        models = rm.get_participating_models(round_idx)
        print(f"\n--- Round {round_idx + 1}: {round_cfg['description']} ---")
        print(f"    Participants: {', '.join(models)}")

        results = {}
        for model_name in models:
            prompt = rm.build_prompt(round_idx, model_name)
            output_file = rm.get_output_file(round_idx, model_name)

            label = AI_MODELS[model_name]["label"]
            print(f"    [{label}] Working...", end="", flush=True)

            output = run_ai_cli(model_name, prompt, work_dir, output_file)
            results[model_name] = output
            print(f" Done ({len(output)} chars)")

        rm.store_round_results(round_idx, results)

    summary = rm.generate_summary()

    # Save final report
    report_file = get_shared_dir(work_dir) / "final_report.md"
    report_file.write_text(summary, encoding="utf-8")
    print(f"\nFinal report saved to: {report_file}")

    return summary


def run_tmux_chat(work_dir: str) -> None:
    """Launch interactive tmux chat mode (default).

    Starts each AI CLI in interactive mode (persistent session),
    then launches the chat loop in the input pane.
    """
    from ai_worker import start_interactive
    from tmux_manager import (
        create_team_session,
        display_in_pane,
        start_chat_in_pane,
        attach_session,
    )

    session_name = f"{TMUX_SESSION_PREFIX}-{int(time.time()) % 10000}"
    active = get_active_models()

    # Create a timestamped session directory for all shared files
    session_dir = create_session_dir(work_dir)
    update_session_meta(models=active)

    print(f"\n{'='*60}")
    print(f"  Multi-AI Team - Interactive Chat")
    print(f"  Session: {session_name}")
    print(f"  Models: {', '.join(active)}")
    print(f"{'='*60}\n")

    # Create tmux session with 4 panes
    print("Creating tmux session...")
    pane_map = create_team_session(session_name)

    # Start AI CLIs in interactive mode with team context as initial prompt.
    # Passing context via CLI argument (file-based) avoids all TUI
    # bracket-paste issues that plague tmux send-keys for long text.
    print("Starting AI CLIs in interactive mode...")
    for role in ("claude", "codex", "gemini"):
        pane = pane_map.get(role)
        if pane and role in active:
            model_cfg = AI_MODELS[role]
            # Build teammate list (everyone except this AI)
            teammates = []
            for other in active:
                if other == role:
                    continue
                other_cfg = AI_MODELS[other]
                teammates.append(
                    f"{other_cfg['label']} ({', '.join(other_cfg['strengths'])})"
                )
            context_msg = INTERACTIVE_TEAM_CONTEXT.format(
                label=model_cfg["label"],
                strengths=", ".join(model_cfg["strengths"]),
                teammates=" / ".join(teammates) if teammates else "(solo mode)",
                name=role,
            )
            start_interactive(pane, role, initial_prompt=context_msg)
            print(f"  {model_cfg['label']} - started")
        elif pane:
            display_in_pane(pane, f"=== {role} (not available) ===")

    # Wait for CLIs to initialize and process initial context
    time.sleep(3)

    # Launch chat_loop.py in the input pane
    print("Starting chat interface...")
    start_chat_in_pane(session_name, pane_map["input"], work_dir, active, str(session_dir))

    time.sleep(0.5)

    # Attach to tmux session (replaces current process on Unix)
    print(f"Attaching to tmux session: {session_name}")
    print(f"  (To reattach later: wsl tmux attach -t {session_name})")
    attach_session(session_name)


def _init_and_check(skip_check: bool, no_tmux: bool) -> None:
    """Run prerequisite checks. Exits on fatal issues."""
    try:
        validate_config()
    except ValueError as e:
        print(f"\nERROR: Invalid configuration.\n{e}")
        sys.exit(1)

    if skip_check:
        return
    issues = check_prerequisites()
    active = get_active_models()
    if issues:
        print("Prerequisites check:")
        for issue in issues:
            print(f"  WARNING: {issue}")
        if not no_tmux and any("tmux" in i for i in issues):
            print("\ntmux not available. Use --no-tmux for batch mode.")
            sys.exit(1)
    if not active:
        print("\nERROR: No AI CLIs available. Install at least one.")
        sys.exit(1)
    if len(active) < 3:
        print(f"\nRunning with available models only: {', '.join(active)}")
        print("(Missing models will be skipped in rounds)\n")


def run_interactive(work_dir: str) -> None:
    """Interactive chat mode - enter tasks in a loop."""
    _init_and_check(skip_check=False, no_tmux=True)

    print(f"\n{'='*60}")
    print(f"  Multi-AI Team - Interactive Mode")
    print(f"  Available: {', '.join(get_active_models())}")
    print(f"  Type 'quit' or 'exit' to end.")
    print(f"{'='*60}\n")

    while True:
        try:
            task = input("You > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nBye!")
            break

        if not task:
            continue
        if task.lower() in ("quit", "exit", "q"):
            print("Bye!")
            break

        print()
        summary = run_batch_mode(task, work_dir)
        print(f"\n{'─'*60}")
        print(summary)
        print(f"{'─'*60}\n")


def main():
    parser = argparse.ArgumentParser(
        description="Multi-AI Team: Claude + Codex + Gemini collaboration"
    )
    parser.add_argument("task", nargs="?", default=None,
                        help="Task for batch mode (ignored in default chat mode)")
    parser.add_argument(
        "--no-tmux",
        action="store_true",
        help="Run in batch mode without tmux",
    )
    parser.add_argument(
        "--work-dir",
        default=str(Path(__file__).parent),
        help="Working directory for shared files",
    )
    parser.add_argument(
        "--skip-check",
        action="store_true",
        help="Skip prerequisite checks",
    )
    args = parser.parse_args()

    _init_and_check(args.skip_check, args.no_tmux)

    if args.no_tmux:
        # Batch mode: single task or interactive loop
        if args.task:
            run_batch_mode(args.task, args.work_dir)
        else:
            run_interactive(args.work_dir)
    else:
        # Default: interactive tmux chat mode
        run_tmux_chat(args.work_dir)

    print("\nDone!")


if __name__ == "__main__":
    main()
