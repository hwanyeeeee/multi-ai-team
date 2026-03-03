"""AI Worker - runs CLI commands for each AI model."""
from __future__ import annotations

import logging
import os
import subprocess
import time
from pathlib import Path
from config import AI_MODELS, AI_RESPONSE_TIMEOUT_SEC, IS_WSL, get_wsl_binary, to_wsl_path, wsl_prefix
from tmux_manager import update_pane_status

logger = logging.getLogger(__name__)

# Retry configuration
MAX_RETRIES = 2  # Maximum additional attempts after first failure


class AIResult(str):
    """String subclass carrying structured metadata about an AI CLI execution.

    Fully backward-compatible: behaves as a plain ``str`` for callers that
    do ``result.startswith(...)``, slicing, formatting, etc.  Additional
    attributes expose structured error information.
    """

    def __new__(
        cls,
        output: str,
        *,
        success: bool = True,
        model_name: str = "",
        error_type: str | None = None,
        error_detail: str | None = None,
        retry_count: int = 0,
        max_retries: int = MAX_RETRIES,
    ):
        instance = super().__new__(cls, output)
        instance.success = success
        instance.model_name = model_name
        instance.error_type = error_type
        instance.error_detail = error_detail
        instance.retry_count = retry_count
        instance.max_retries = max_retries
        return instance

    @property
    def is_error(self) -> bool:
        return not self.success

    def to_dict(self) -> dict:
        """Serialize to a plain dict (useful for JSON logging)."""
        return {
            "output": str(self),
            "success": self.success,
            "model_name": self.model_name,
            "error_type": self.error_type,
            "error_detail": self.error_detail,
            "retry_count": self.retry_count,
            "max_retries": self.max_retries,
        }


def _build_cli_command(model_name: str, prompt_path: str) -> list[str]:
    """Build the shell command list for an AI CLI invocation."""
    binary = get_wsl_binary(model_name)
    args = " ".join(AI_MODELS[model_name]["args"])
    shell_cmd = f'{binary} {args} "$(cat {prompt_path})"'
    if IS_WSL:
        return ["bash", "-c", shell_cmd]
    return ["wsl", "bash", "-lc", shell_cmd]


def _execute_subprocess(
    cmd: list[str], work_dir: str, model_name: str
) -> AIResult:
    """Run a single subprocess attempt.  Returns an AIResult."""
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=AI_RESPONSE_TIMEOUT_SEC,
        cwd=work_dir,
    )
    output = result.stdout.strip()
    if not output and result.stderr:
        stderr_text = result.stderr.strip()
        return AIResult(
            f"[stderr] {stderr_text}",
            success=False,
            model_name=model_name,
            error_type="stderr",
            error_detail=stderr_text,
        )
    if not output and result.returncode != 0:
        return AIResult(
            f"[error] {model_name}: process exited with code {result.returncode}",
            success=False,
            model_name=model_name,
            error_type="exit_code",
            error_detail=f"returncode={result.returncode}",
        )
    return AIResult(output, success=True, model_name=model_name)


def run_ai_cli(model_name: str, prompt: str, work_dir: str, output_file: str) -> AIResult:
    """Run an AI CLI command and capture output to file.

    On failure, retries up to ``MAX_RETRIES`` times before returning a
    structured :class:`AIResult` containing the failure cause and retry count.
    """
    binary = get_wsl_binary(model_name)

    # Write prompt to file to avoid shell escaping issues
    prompt_file = str(Path(work_dir) / "shared" / f"{model_name}_batch_prompt.txt")
    Path(prompt_file).parent.mkdir(parents=True, exist_ok=True)
    Path(prompt_file).write_text(prompt, encoding="utf-8")

    prompt_path = prompt_file if IS_WSL else to_wsl_path(prompt_file)
    cmd = _build_cli_command(model_name, prompt_path)

    last_error: AIResult | None = None

    for attempt in range(1 + MAX_RETRIES):  # 1 initial + up to MAX_RETRIES retries
        try:
            ai_result = _execute_subprocess(cmd, work_dir, model_name)
            if ai_result.success:
                # Attach retry metadata even on success
                ai_result = AIResult(
                    str(ai_result),
                    success=True,
                    model_name=model_name,
                    retry_count=attempt,
                )
                break
            # Non-fatal subprocess failure (stderr / bad exit code) — retry
            last_error = ai_result
            logger.warning(
                "%s attempt %d/%d failed: %s",
                model_name, attempt + 1, 1 + MAX_RETRIES, ai_result.error_detail,
            )

        except subprocess.TimeoutExpired:
            last_error = AIResult(
                f"[timeout] {model_name}: no response within {AI_RESPONSE_TIMEOUT_SEC}s "
                f"(attempt {attempt + 1}/{1 + MAX_RETRIES})",
                success=False,
                model_name=model_name,
                error_type="timeout",
                error_detail=f"exceeded {AI_RESPONSE_TIMEOUT_SEC}s",
                retry_count=attempt,
                max_retries=MAX_RETRIES,
            )
            logger.warning(
                "%s attempt %d/%d timed out after %ds",
                model_name, attempt + 1, 1 + MAX_RETRIES, AI_RESPONSE_TIMEOUT_SEC,
            )

        except Exception as e:
            last_error = AIResult(
                f"[error] {model_name}: {e} "
                f"(attempt {attempt + 1}/{1 + MAX_RETRIES})",
                success=False,
                model_name=model_name,
                error_type="exception",
                error_detail=str(e),
                retry_count=attempt,
                max_retries=MAX_RETRIES,
            )
            logger.warning(
                "%s attempt %d/%d exception: %s",
                model_name, attempt + 1, 1 + MAX_RETRIES, e,
            )
    else:
        # All attempts exhausted — finalize the last error with full retry count
        ai_result = AIResult(
            str(last_error),
            success=False,
            model_name=model_name,
            error_type=last_error.error_type if last_error else "unknown",
            error_detail=last_error.error_detail if last_error else "all retries failed",
            retry_count=MAX_RETRIES,
            max_retries=MAX_RETRIES,
        )

    # Save to file
    Path(output_file).parent.mkdir(parents=True, exist_ok=True)
    Path(output_file).write_text(str(ai_result), encoding="utf-8")

    return ai_result


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
    update_pane_status(pane_target, model_name, "실행중")
    escaped = tmux_cmd.replace("'", "'\\''")
    subprocess.run(
        wsl_prefix() + ["tmux", "send-keys", "-t", pane_target, escaped, "Enter"],
        timeout=5,
    )


def wait_for_completion(output_files: dict[str, str], timeout: int = AI_RESPONSE_TIMEOUT_SEC) -> dict[str, AIResult]:
    """Wait for all AI workers to complete by checking for ===DONE=== marker.

    Returns dict of model_name -> :class:`AIResult`.
    """
    results: dict[str, AIResult] = {}
    start = time.time()

    pending = set(output_files.keys())

    while pending and (time.time() - start) < timeout:
        for model_name in list(pending):
            fpath = output_files[model_name]
            if os.path.exists(fpath):
                content = Path(fpath).read_text(encoding="utf-8", errors="replace")
                if "===DONE===" in content:
                    clean = content.replace("===DONE===", "").strip()
                    results[model_name] = AIResult(
                        clean, success=True, model_name=model_name,
                    )
                    pending.discard(model_name)
        if pending:
            time.sleep(2)

    # Handle timeouts with structured error objects
    elapsed = int(time.time() - start)
    for model_name in pending:
        fpath = output_files[model_name]
        if os.path.exists(fpath):
            partial = Path(fpath).read_text(encoding="utf-8", errors="replace").strip()
            results[model_name] = AIResult(
                partial,
                success=False,
                model_name=model_name,
                error_type="timeout_partial",
                error_detail=f"partial output after {elapsed}s (limit {timeout}s)",
            )
        else:
            results[model_name] = AIResult(
                f"[timeout] {model_name}: no response after {elapsed}s (limit {timeout}s)",
                success=False,
                model_name=model_name,
                error_type="timeout",
                error_detail=f"no output file after {elapsed}s",
            )

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
        update_pane_status(pane, name, "실행중")

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
                    update_pane_status(pane, name, "실행중")
            else:
                # Phase 2: waiting for content to stabilize
                if content != last_contents[name]:
                    last_contents[name] = content
                    last_changes[name] = time.time()
                elif (time.time() - last_changes[name]) >= stable_secs:
                    done.add(name)
                    update_pane_status(pane, name, "완료")

        if len(done) < len(pane_targets):
            time.sleep(2)

    # For AIs that started but didn't stabilize, use last captured content
    # For AIs that never started, use initial content
    results = {}
    for name in pane_targets:
        if name not in done:
            update_pane_status(pane_targets[name], name, "에러")
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

    output_file = str(Path(work_dir) / "shared" / "synthesis.md")
    return run_ai_cli("claude", prompt, work_dir, output_file)


