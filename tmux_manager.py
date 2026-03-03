"""Tmux pane management for Multi-AI Team."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import shlex
from pathlib import Path

from config import IS_WSL, to_wsl_path, wsl_prefix


def run_tmux(cmd: list[str] | str, check: bool = True) -> str:
    """Run a tmux command, with wsl prefix when on Windows.

    ``cmd`` can be a pre-split list (preferred) or a string that will be
    parsed with ``shlex.split`` so that quoted arguments are handled
    correctly.
    """
    if isinstance(cmd, str):
        parts = shlex.split(cmd)
    else:
        parts = list(cmd)
    full_cmd = wsl_prefix() + ["tmux"] + parts
    result = subprocess.run(
        full_cmd, capture_output=True, text=True, timeout=10
    )
    if check and result.returncode != 0:
        raise RuntimeError(f"tmux error: {result.stderr.strip()}")
    return result.stdout.strip()


def session_exists(session_name: str) -> bool:
    """Check if tmux session exists."""
    try:
        result = subprocess.run(
            wsl_prefix() + ["tmux", "has-session", "-t", session_name],
            capture_output=True, text=True, timeout=5,
        )
        return result.returncode == 0
    except Exception:
        return False


def create_team_session(session_name: str) -> dict[str, str]:
    """
    Create tmux session with 4 panes:
    ┌──────────────┬──────────────┐
    │  Claude      │  Codex       │
    │  (pane 0)    │  (pane 1)    │
    ├──────────────┼──────────────┤
    │  Gemini      │  Log/Chat    │
    │  (pane 2)    │  (pane 3)    │
    └──────────────┴──────────────┘

    Returns dict mapping role -> pane_id
    """
    if session_exists(session_name):
        run_tmux(f"kill-session -t {shlex.quote(session_name)}", check=False)

    # Create session (detached)
    run_tmux(f"new-session -d -s {shlex.quote(session_name)} -x 200 -y 50")

    # Split into 4 panes
    run_tmux(f"split-window -h -t {shlex.quote(session_name)}")    # pane 1 (right)
    run_tmux(f"split-window -v -t {shlex.quote(session_name)}.1")  # pane 2 (bottom-right)
    run_tmux(f"select-pane -t {shlex.quote(session_name)}.0")
    run_tmux(f"split-window -v -t {shlex.quote(session_name)}.0")  # pane 2 becomes bottom-left

    time.sleep(0.3)

    pane_map = {
        "claude": f"{session_name}.0",
        "codex": f"{session_name}.1",
        "gemini": f"{session_name}.2",
        "input": f"{session_name}.3",
    }

    # Set pane titles (use list form to avoid quoting issues with spaces)
    labels = {
        "claude": "Claude (Reasoning)",
        "codex": "Codex (Code)",
        "gemini": "Gemini (Research)",
        "input": "User Input",
    }
    for role, pane in pane_map.items():
        run_tmux(["select-pane", "-t", pane, "-T", labels[role]], check=False)

    # Enable pane border status
    run_tmux(["set-option", "-t", session_name, "pane-border-status", "top"], check=False)
    run_tmux(["set-option", "-t", session_name, "pane-border-format", " #{pane_title} "], check=False)

    return pane_map


def update_pane_status(pane_target: str, role: str, status: str) -> None:
    """Update tmux pane title with role and status.

    Status should be one of: 대기중, 실행중, 완료, 에러
    """
    labels = {
        "claude": "Claude (Reasoning)",
        "codex": "Codex (Code)",
        "gemini": "Gemini (Research)",
        "input": "User Input",
    }
    label = labels.get(role, role)

    # Use colors/symbols for status if desired
    status_map = {
        "대기중": "⏳ 대기중",
        "실행중": "🚀 실행중",
        "완료": "✅ 완료",
        "에러": "❌ 에러",
    }
    display_status = status_map.get(status, status)

    title = f"{label} [{display_status}]"
    run_tmux(["select-pane", "-t", pane_target, "-T", title], check=False)


def send_to_pane(pane_target: str, text: str) -> None:
    """Send text to a tmux pane (visible in the pane)."""
    run_tmux(["send-keys", "-t", pane_target, text, "Enter"])


def display_in_pane(pane_target: str, message: str) -> None:
    """Display a status message in pane using echo."""
    escaped = message.replace("'", "'\\''")
    run_tmux(["send-keys", "-t", pane_target, f"echo '{escaped}'", "Enter"])


def capture_pane(pane_target: str, lines: int = 200) -> str:
    """Capture current pane content."""
    return run_tmux(f"capture-pane -t {pane_target} -p -S -{lines}")


def kill_session(session_name: str) -> None:
    """Kill tmux session."""
    run_tmux(f"kill-session -t {shlex.quote(session_name)}", check=False)


def start_chat_in_pane(
    session_name: str,
    pane_target: str,
    work_dir: str,
    active_models: list[str],
    session_dir: str = "",
) -> None:
    """Launch chat_loop.py in the input pane."""
    script = str((Path(__file__).parent / "chat_loop.py").resolve())
    models_json = json.dumps(active_models)

    if IS_WSL:
        wsl_script = script
        wsl_work_dir = work_dir
        wsl_session_dir = session_dir
    else:
        wsl_script = to_wsl_path(script)
        wsl_work_dir = to_wsl_path(work_dir)
        wsl_session_dir = to_wsl_path(session_dir) if session_dir else ""

    cmd = (
        f"python3 {wsl_script}"
        f" --session {shlex.quote(session_name)}"
        f" --work-dir {shlex.quote(wsl_work_dir)}"
        f" --models {shlex.quote(models_json)}"
    )
    if wsl_session_dir:
        cmd += f" --session-dir {shlex.quote(wsl_session_dir)}"
    run_tmux(["send-keys", "-t", pane_target, cmd, "Enter"])


def attach_session(session_name: str) -> None:
    """Attach to a tmux session, replacing the current process.

    If already inside tmux, uses switch-client instead of attach to
    avoid the 'sessions should be nested' error.
    """
    already_in_tmux = bool(os.environ.get("TMUX"))
    if already_in_tmux:
        # switch-client works from inside an existing tmux session
        full_cmd = wsl_prefix() + ["tmux", "switch-client", "-t", session_name]
    else:
        full_cmd = wsl_prefix() + ["tmux", "attach-session", "-t", session_name]

    if IS_WSL or sys.platform != "win32":
        os.execvp(full_cmd[0], full_cmd)
    else:
        subprocess.run(full_cmd)


