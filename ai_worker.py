"""AI Worker - runs CLI commands for each AI model."""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from config import AI_MODELS, AI_RESPONSE_TIMEOUT_SEC, IS_WSL, get_wsl_binary, to_wsl_path, wsl_prefix


def run_ai_cli(model_name: str, prompt: str, work_dir: str, output_file: str) -> str:
    """
    Run an AI CLI command and capture output to file.
    Returns the output text.
    """
    binary = get_wsl_binary(model_name)
    args = " ".join(AI_MODELS[model_name]["args"])

    # Write prompt to file to avoid shell escaping issues
    prompt_file = str(Path(work_dir) / "shared" / f"{model_name}_batch_prompt.txt")
    Path(prompt_file).parent.mkdir(parents=True, exist_ok=True)
    Path(prompt_file).write_text(prompt, encoding="utf-8")

    prompt_path = prompt_file if IS_WSL else to_wsl_path(prompt_file)
    # Use absolute binary path to avoid PATH issues in WSL
    shell_cmd = f'{binary} {args} "$(cat {prompt_path})"'
    if IS_WSL:
        cmd = ["bash", "-c", shell_cmd]
    else:
        cmd = ["wsl", "bash", "-lc", shell_cmd]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=AI_RESPONSE_TIMEOUT_SEC,
            cwd=work_dir,
        )
        output = result.stdout.strip()
        if not output and result.stderr:
            output = f"[stderr] {result.stderr.strip()}"
    except subprocess.TimeoutExpired:
        output = f"[timeout] {model_name} did not respond within {AI_RESPONSE_TIMEOUT_SEC}s"
    except Exception as e:
        output = f"[error] {model_name}: {e}"

    # Save to file
    Path(output_file).parent.mkdir(parents=True, exist_ok=True)
    Path(output_file).write_text(output, encoding="utf-8")

    return output


def run_ai_in_tmux_pane(
    pane_target: str,
    model_name: str,
    prompt: str,
    output_file: str,
    work_dir: str,
) -> None:
    """
    Send AI CLI command to a tmux pane so the user can see it running.
    Output is tee'd to a file for collection.
    """
    args = " ".join(AI_MODELS[model_name]["args"])

    # Write prompt to a temp file (avoids shell escaping issues)
    prompt_file = str(Path(work_dir) / "shared" / f"{model_name}_prompt.txt")
    Path(prompt_file).parent.mkdir(parents=True, exist_ok=True)
    Path(prompt_file).write_text(prompt, encoding="utf-8")

    # Convert paths if on Windows
    tmux_prompt = prompt_file if IS_WSL else to_wsl_path(prompt_file)
    tmux_output = output_file if IS_WSL else to_wsl_path(output_file)

    # Use absolute binary path to avoid PATH issues
    binary = get_wsl_binary(model_name)
    tmux_cmd = (
        f'{binary} {args} "$(cat {tmux_prompt})" 2>&1 | tee {tmux_output}'
        f' && echo "===DONE===" >> {tmux_output}'
    )

    # Send to tmux pane
    escaped = tmux_cmd.replace("'", "'\\''")
    subprocess.run(
        wsl_prefix() + ["tmux", "send-keys", "-t", pane_target, escaped, "Enter"],
        timeout=5,
    )


def wait_for_completion(output_files: dict[str, str], timeout: int = AI_RESPONSE_TIMEOUT_SEC) -> dict[str, str]:
    """
    Wait for all AI workers to complete by checking for ===DONE=== marker.
    Returns dict of model_name -> output text.
    """
    results = {}
    start = time.time()

    pending = set(output_files.keys())

    while pending and (time.time() - start) < timeout:
        for model_name in list(pending):
            fpath = output_files[model_name]
            if os.path.exists(fpath):
                content = Path(fpath).read_text(encoding="utf-8", errors="replace")
                if "===DONE===" in content:
                    results[model_name] = content.replace("===DONE===", "").strip()
                    pending.discard(model_name)
        if pending:
            time.sleep(2)

    # Handle timeouts
    for model_name in pending:
        fpath = output_files[model_name]
        if os.path.exists(fpath):
            results[model_name] = Path(fpath).read_text(encoding="utf-8", errors="replace").strip()
        else:
            results[model_name] = f"[timeout] No response from {model_name}"

    return results


def clear_pane(pane_target: str) -> None:
    """Clear a tmux pane before showing new output."""
    subprocess.run(
        wsl_prefix() + ["tmux", "send-keys", "-t", pane_target, "clear", "Enter"],
        capture_output=True,
        timeout=5,
    )


def start_interactive(pane_target: str, model_name: str) -> None:
    """Start an AI CLI in interactive mode in a tmux pane.

    The CLI stays running and maintains its own conversation history.
    """
    binary = get_wsl_binary(model_name)
    interactive_args = " ".join(AI_MODELS[model_name].get("interactive_args", []))
    cmd = f"{binary} {interactive_args}".strip()
    subprocess.run(
        wsl_prefix() + ["tmux", "send-keys", "-t", pane_target, cmd, "Enter"],
        capture_output=True,
        timeout=5,
    )


def send_message_to_pane(pane_target: str, message: str) -> None:
    """Send a chat message to an AI running in interactive mode.

    Sends text literally with -l flag, pauses briefly so the TUI can
    process the input, then sends Enter separately.
    """
    # Send text literally (no key name interpretation)
    subprocess.run(
        wsl_prefix() + ["tmux", "send-keys", "-t", pane_target, "-l", message],
        capture_output=True,
        timeout=5,
    )
    # Brief pause so TUI apps (especially Codex) register the text
    time.sleep(0.2)
    # Send Enter key separately to submit
    subprocess.run(
        wsl_prefix() + ["tmux", "send-keys", "-t", pane_target, "Enter"],
        capture_output=True,
        timeout=5,
    )


def capture_pane_content(pane_target: str, lines: int = 100) -> str:
    """Capture visible content from a tmux pane."""
    result = subprocess.run(
        wsl_prefix() + ["tmux", "capture-pane", "-t", pane_target, "-p", f"-S", f"-{lines}"],
        capture_output=True,
        text=True,
        timeout=5,
    )
    return result.stdout.strip()


def wait_for_all_panes_idle(
    pane_targets: dict[str, str],
    stable_secs: int = 5,
    timeout: int = 90,
) -> dict[str, str]:
    """Wait for multiple tmux panes to stop changing, then capture content.

    Two-phase approach:
    1. Wait for each pane's content to CHANGE (AI started responding).
    2. Then wait for it to STABILIZE (AI finished responding).

    This prevents premature idle detection when an AI hasn't started yet.
    """
    start = time.time()
    initial_contents: dict[str, str] = {}
    last_contents: dict[str, str] = {}
    last_changes: dict[str, float] = {}
    started: set[str] = set()  # AIs that have started responding
    done: set[str] = set()     # AIs that have finished responding

    # Snapshot current state BEFORE AI starts responding
    for name, pane in pane_targets.items():
        content = capture_pane_content(pane, lines=50)
        initial_contents[name] = content
        last_contents[name] = content
        last_changes[name] = start

    time.sleep(1)

    while (time.time() - start) < timeout and len(done) < len(pane_targets):
        for name, pane in pane_targets.items():
            if name in done:
                continue
            content = capture_pane_content(pane, lines=50)

            if name not in started:
                # Phase 1: waiting for content to change from initial snapshot
                if content != initial_contents[name]:
                    started.add(name)
                    last_contents[name] = content
                    last_changes[name] = time.time()
            else:
                # Phase 2: waiting for content to stabilize
                if content != last_contents[name]:
                    last_contents[name] = content
                    last_changes[name] = time.time()
                elif (time.time() - last_changes[name]) >= stable_secs:
                    done.add(name)

        if len(done) < len(pane_targets):
            time.sleep(2)

    # For AIs that started but didn't stabilize, use last captured content
    # For AIs that never started, use initial content
    results = {}
    for name in pane_targets:
        if name in started:
            results[name] = last_contents[name]
        else:
            results[name] = initial_contents[name]

    return results


def synthesize_responses(
    responses: dict[str, str],
    user_message: str,
    work_dir: str,
) -> str:
    """Use Claude in batch mode to synthesize multiple AI responses."""
    parts = []
    for model, content in responses.items():
        label = AI_MODELS.get(model, {}).get("label", model)
        parts.append(f"=== {label} ===\n{content}")

    all_responses = "\n\n".join(parts)

    prompt = (
        f"User question: {user_message}\n\n"
        f"Below are responses from AI team members (captured from their terminals):\n\n"
        f"{all_responses}\n\n"
        "Synthesize the responses to the user's question above into a concise summary.\n"
        "Include key points from each AI, note any conflicts, and give a unified recommendation.\n"
        "Keep it under 300 words. Respond in the same language as the user question."
    )

    output_file = str(Path(work_dir) / "shared" / "synthesis.txt")
    return run_ai_cli("claude", prompt, work_dir, output_file)


