"""AI Worker - runs CLI commands for each AI model."""
from __future__ import annotations

import concurrent.futures
import logging
import os
import subprocess
import time
from pathlib import Path
from config import AI_MODELS, AI_RESPONSE_TIMEOUT_SEC, IS_WSL, get_shared_dir, get_wsl_binary, to_wsl_path, wsl_prefix
from tmux_manager import update_pane_status

logger = logging.getLogger(__name__)

# Retry configuration
MAX_RETRIES = 2  # Maximum additional attempts after first failure

# TUI input timing (for send_message_to_pane)
HEX_CHUNK_BYTES = 100            # bytes per hex-mode tmux call (smaller = more reliable)
HEX_CHUNK_DELAY_SEC = 0.3        # pause between hex chunks (longer = less tmux contention)
POST_TEXT_SETTLE_SEC = 2.0       # wait for TUI to finish rendering before Enter
ENTER_BASE_DELAY_SEC = 1.0       # initial wait before first Enter
ENTER_RETRY_BACKOFF_SEC = 1.0    # added per retry attempt
ENTER_VERIFY_WAIT_SEC = 1.0      # wait after Enter before checking pane
MAX_ENTER_RETRIES = 3            # max Enter attempts
MAX_TUI_MSG_CHARS = 0            # no limit — hex+xargs handles any length


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
    prompt_file = str(get_shared_dir(work_dir) / f"{model_name}_batch_prompt.txt")
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
) -> str:
    """Send AI CLI command to a tmux pane so the user can see it running.

    Output is tee'd to a file for collection.  Returns the tmux wait-for
    signal name that the caller should wait on.
    """
    args = " ".join(AI_MODELS[model_name]["args"])

    # Write prompt to a temp file (avoids shell escaping issues)
    prompt_file = str(get_shared_dir(work_dir) / f"{model_name}_prompt.txt")
    Path(prompt_file).parent.mkdir(parents=True, exist_ok=True)
    Path(prompt_file).write_text(prompt, encoding="utf-8")

    # Convert paths if on Windows
    tmux_prompt = prompt_file if IS_WSL else to_wsl_path(prompt_file)
    tmux_output = output_file if IS_WSL else to_wsl_path(output_file)

    # Build a unique tmux wait-for signal name
    signal = f"done-{model_name}-{pane_target.replace('.', '-')}"

    # Use absolute binary path to avoid PATH issues
    binary = get_wsl_binary(model_name)
    tmux_cmd = (
        f'{binary} {args} "$(cat {tmux_prompt})" 2>&1 | tee {tmux_output}'
        f' ; tmux wait-for -S {signal}'
    )

    # Send to tmux pane
    update_pane_status(pane_target, model_name, "실행중")
    escaped = tmux_cmd.replace("'", "'\\''")
    subprocess.run(
        wsl_prefix() + ["tmux", "send-keys", "-t", pane_target, escaped, "Enter"],
        timeout=5,
    )
    return signal


def wait_for_signals(
    signals: dict[str, str],
    output_files: dict[str, str],
    pane_targets: dict[str, str] | None = None,
    timeout: int = AI_RESPONSE_TIMEOUT_SEC,
) -> dict[str, AIResult]:
    """Wait for tmux wait-for signals from each pane (no polling).

    ``signals`` maps model_name -> signal_name (returned by
    ``run_ai_in_tmux_pane``).  ``output_files`` maps model_name -> output
    file path used to read the result after the signal fires.

    Returns dict of model_name -> :class:`AIResult`.
    """
    results: dict[str, AIResult] = {}

    def _wait_one(model: str, signal: str) -> tuple[str, AIResult]:
        try:
            subprocess.run(
                wsl_prefix() + ["tmux", "wait-for", signal],
                timeout=timeout,
            )
            # Signal received — read output file
            fpath = output_files.get(model, "")
            if fpath and os.path.exists(fpath):
                content = Path(fpath).read_text(encoding="utf-8", errors="replace").strip()
                if pane_targets and model in pane_targets:
                    update_pane_status(pane_targets[model], model, "완료")
                return model, AIResult(content, success=True, model_name=model)
            return model, AIResult(
                f"[error] {model}: signal received but output file missing",
                success=False, model_name=model,
                error_type="missing_output", error_detail="no output file after signal",
            )
        except subprocess.TimeoutExpired:
            fpath = output_files.get(model, "")
            partial = ""
            if fpath and os.path.exists(fpath):
                partial = Path(fpath).read_text(encoding="utf-8", errors="replace").strip()
            if pane_targets and model in pane_targets:
                update_pane_status(pane_targets[model], model, "에러")
            if partial:
                return model, AIResult(
                    partial, success=False, model_name=model,
                    error_type="timeout_partial",
                    error_detail=f"partial output after {timeout}s",
                )
            return model, AIResult(
                f"[timeout] {model}: no response within {timeout}s",
                success=False, model_name=model,
                error_type="timeout",
                error_detail=f"exceeded {timeout}s",
            )

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(signals)) as pool:
        futures = {pool.submit(_wait_one, m, s): m for m, s in signals.items()}
        for f in concurrent.futures.as_completed(futures):
            model, result = f.result()
            results[model] = result

    return results


def clear_pane(pane_target: str) -> None:
    """Clear a tmux pane before showing new output."""
    subprocess.run(
        wsl_prefix() + ["tmux", "send-keys", "-t", pane_target, "clear", "Enter"],
        capture_output=True,
        timeout=5,
    )


def start_interactive(pane_target: str, model_name: str, initial_prompt: str = "") -> None:
    """Start an AI CLI in interactive mode in a tmux pane.

    The CLI stays running and maintains its own conversation history.
    If ``initial_prompt`` is provided, it is written to a temp file
    and passed as the first message via ``$(cat file)`` — this avoids
    all TUI bracket-paste issues.
    """
    binary = get_wsl_binary(model_name)
    interactive_args = " ".join(AI_MODELS[model_name].get("interactive_args", []))

    if initial_prompt:
        # Write prompt to temp file (avoids shell escaping issues entirely)
        prompt_file = f"/tmp/_team_init_{model_name}.txt"
        subprocess.run(
            wsl_prefix() + ["bash", "-c", f"cat > {prompt_file}"],
            input=initial_prompt.encode("utf-8"),
            capture_output=True, timeout=5,
        )
        cmd = f'{binary} {interactive_args} "$(cat {prompt_file})"'
    else:
        cmd = f"{binary} {interactive_args}"

    subprocess.run(
        wsl_prefix() + ["tmux", "send-keys", "-t", pane_target, cmd.strip(), "Enter"],
        capture_output=True,
        timeout=5,
    )


def restart_interactive(pane_target: str, model_name: str, initial_prompt: str = "") -> None:
    """Restart an AI CLI in a tmux pane (kill current + start fresh)."""
    # Send exit/quit command to gracefully close
    subprocess.run(
        wsl_prefix() + ["tmux", "send-keys", "-t", pane_target, "/exit", "Enter"],
        capture_output=True, timeout=5,
    )
    time.sleep(2)
    # Clear pane
    clear_pane(pane_target)
    time.sleep(0.5)
    # Start fresh (with optional initial prompt)
    start_interactive(pane_target, model_name, initial_prompt=initial_prompt)


def send_message_to_pane(pane_target: str, message: str) -> None:
    """Send a chat message to an AI running in interactive mode.

    Uses ``tmux send-keys -H`` (hex mode) to bypass bracket-paste for
    the text, then sends a named ``Enter`` key separately.  Hex ``0d``
    does NOT work as Enter in TUI apps (ink/React), so the named key
    is required.
    """
    clean = message.replace("\n", " ").replace("\r", " ")

    # Send text via hex (no Enter) — no length limit, hex+xargs handles any size
    _send_hex_text_fast(pane_target, clean)

    # Let TUI render the text before sending Enter
    time.sleep(POST_TEXT_SETTLE_SEC)

    # Send named Enter key (works reliably for all TUI CLIs)
    subprocess.run(
        wsl_prefix() + ["tmux", "send-keys", "-t", pane_target, "Enter"],
        capture_output=True, timeout=5,
    )


def _send_hex_text_fast(pane_target: str, text: str) -> None:
    """Send text as hex bytes to a pane via temp file + xargs (no Enter).

    Writes hex string to a temp file via stdin (avoids Windows 32KB
    command-line limit for long messages), then pipes it through
    ``xargs tmux send-keys -H`` for a single fast delivery.

    Note: hex ``0d`` (CR) does NOT work as Enter in TUI apps
    (ink/React).  Use ``tmux send-keys Enter`` separately.
    """
    raw = text.encode("utf-8")
    hex_str = " ".join(f"{b:02x}" for b in raw)

    # Write hex to temp file via stdin (not command-line args)
    tmp_file = "/tmp/_team_hex_single.txt"
    subprocess.run(
        wsl_prefix() + ["bash", "-c", f"cat > {tmp_file}"],
        input=hex_str.encode("ascii"),
        capture_output=True, timeout=10,
    )
    # Send via xargs (reads from file, handles arg-length splitting)
    subprocess.run(
        wsl_prefix() + ["bash", "-c",
                        f"cat {tmp_file} | xargs tmux send-keys -t {pane_target} -H"],
        capture_output=True, timeout=30,
    )


def _send_enter_with_retry(pane_target: str) -> None:
    """Send Enter to a pane with retry logic.

    Uses normalized comparison to avoid false retries caused by
    TUI noise (spinners, ANSI codes, cursor movement).
    Alternates between hex Enter (0x0d) and tmux 'Enter' key
    for maximum compatibility across different CLI TUIs.

    First checks if Enter was already accepted (e.g. from a prior
    parallel send), and skips if content already changed.
    """
    # Use more lines (20) to detect changes — some TUIs have static footer areas
    before = _normalize_for_compare(capture_pane_content(pane_target, lines=20))

    for attempt in range(MAX_ENTER_RETRIES):
        # Check if content already changed (Enter was accepted by prior send)
        current = _normalize_for_compare(capture_pane_content(pane_target, lines=20))
        if current != before:
            return  # Already accepted

        if attempt > 0:
            time.sleep(ENTER_RETRY_BACKOFF_SEC)

        if attempt % 2 == 0:
            # tmux named key Enter (works for Gemini / standard TUI)
            subprocess.run(
                wsl_prefix() + ["tmux", "send-keys", "-t", pane_target, "Enter"],
                capture_output=True, timeout=5,
            )
        else:
            # Hex mode Enter (works for Claude Code / ink TUI)
            subprocess.run(
                wsl_prefix() + ["tmux", "send-keys", "-t", pane_target, "-H", "0d"],
                capture_output=True, timeout=5,
            )

        time.sleep(ENTER_VERIFY_WAIT_SEC)
        after = _normalize_for_compare(capture_pane_content(pane_target, lines=20))
        if after != before:
            return
        logger.warning(
            "_send_enter_with_retry: attempt %d/%d — pane %s unchanged",
            attempt + 1, MAX_ENTER_RETRIES, pane_target,
        )


def send_message_to_panes_parallel(pane_messages: list[tuple[str, str]]) -> None:
    """Send messages to multiple panes simultaneously.

    Uses hex mode via bash scripts for TUI compatibility (no bracket-paste).
    All panes receive text concurrently via background subshells (&).
    Each pane's hex is written to a temp file and piped through xargs
    to avoid argument-length limits.

    Phase 1: Write hex files for each pane
    Phase 2: Send hex to all panes concurrently (bash & + xargs)
    Phase 3: Settle wait for TUI rendering
    Phase 4: Send Enter to all panes concurrently + verify
    """
    if not pane_messages:
        return

    # Phase 1: Write hex data to temp files (text only, no Enter)
    for i, (pane_target, message) in enumerate(pane_messages):
        clean = message.replace("\n", " ").replace("\r", " ")
        raw = clean.encode("utf-8")
        hex_str = " ".join(f"{b:02x}" for b in raw)
        tmp_file = f"/tmp/_team_hex_{i}.txt"
        subprocess.run(
            wsl_prefix() + ["bash", "-c", f"cat > {tmp_file}"],
            input=hex_str.encode("ascii"),
            capture_output=True, timeout=5,
        )

    # Phase 2: Send hex text to all panes concurrently
    script_parts = []
    for i, (pane_target, _) in enumerate(pane_messages):
        tmp_file = f"/tmp/_team_hex_{i}.txt"
        script_parts.append(
            f'( cat {tmp_file} | xargs tmux send-keys -t {pane_target} -H ) &'
        )
    script_parts.append("wait")
    subprocess.run(
        wsl_prefix() + ["bash", "-c", "\n".join(script_parts)],
        capture_output=True, timeout=60,
    )

    # Phase 3: Wait for TUI to render text before sending Enter
    time.sleep(POST_TEXT_SETTLE_SEC)

    # Phase 4: Send named Enter to all panes concurrently (single shot, no retry)
    # Named Enter works reliably for all TUI CLIs (hex 0d does NOT)
    enter_parts = []
    for pane_target, _ in pane_messages:
        enter_parts.append(f'tmux send-keys -t {pane_target} Enter &')
    enter_parts.append("wait")
    subprocess.run(
        wsl_prefix() + ["bash", "-c", "\n".join(enter_parts)],
        capture_output=True, timeout=10,
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


import re as _re

# Regex to strip ANSI escape sequences
_ANSI_RE = _re.compile(r"\x1b\[[0-9;]*[A-Za-z]|\x1b\].*?\x07|\x1b[()][AB012]")
# Common TUI spinner characters
_SPINNER_CHARS = set("⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏◐◑◒◓/-\\●○◉◎▪▫⣾⣽⣻⢿⡿⣟⣯⣷⠁⠂⠄⡀⢀⠠⠐⠈⣀⣤⣶⣿")


def _normalize_for_compare(text: str) -> str:
    """Normalize pane content for stable comparison.

    Strips ANSI escapes, spinner characters, and trailing whitespace
    so that TUI cosmetic changes don't prevent idle detection.
    """
    text = _ANSI_RE.sub("", text)
    text = "".join(c for c in text if c not in _SPINNER_CHARS)
    lines = [line.rstrip() for line in text.splitlines()]
    # Drop empty trailing lines
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


def wait_for_all_panes_idle(
    pane_targets: dict[str, str],
    stable_secs: int = 5,
    timeout: int = 1800,
    min_poll: float = 2.0,
    max_poll: float = 8.0,
) -> dict[str, str]:
    """Wait for multiple tmux panes to stop changing, then capture content.

    Two-phase approach:
    1. Wait for each pane's content to CHANGE (AI started responding).
    2. Then wait for it to STABILIZE (AI finished responding).

    Polling interval increases gradually from ``min_poll`` to ``max_poll``
    (exponential backoff, factor 1.5) to reduce subprocess overhead.
    """
    start = time.time()
    initial_contents: dict[str, str] = {}
    last_contents: dict[str, str] = {}
    last_changes: dict[str, float] = {}
    started: set[str] = set()  # AIs that have started responding
    done: set[str] = set()     # AIs that have finished responding
    poll_interval = min_poll

    # Snapshot current state BEFORE AI starts responding
    for name, pane in pane_targets.items():
        content = _normalize_for_compare(capture_pane_content(pane, lines=50))
        initial_contents[name] = content
        last_contents[name] = content
        last_changes[name] = start
        update_pane_status(pane, name, "실행중")

    time.sleep(1)

    while (time.time() - start) < timeout and len(done) < len(pane_targets):
        changed_this_cycle = False
        for name, pane in pane_targets.items():
            if name in done:
                continue
            content = _normalize_for_compare(capture_pane_content(pane, lines=50))

            if name not in started:
                # Phase 1: waiting for content to change from initial snapshot
                if content != initial_contents[name]:
                    started.add(name)
                    last_contents[name] = content
                    last_changes[name] = time.time()
                    update_pane_status(pane, name, "실행중")
                    changed_this_cycle = True
            else:
                # Phase 2: waiting for content to stabilize
                if content != last_contents[name]:
                    last_contents[name] = content
                    last_changes[name] = time.time()
                    changed_this_cycle = True
                elif (time.time() - last_changes[name]) >= stable_secs:
                    done.add(name)
                    update_pane_status(pane, name, "완료")

        if len(done) < len(pane_targets):
            time.sleep(poll_interval)
            if changed_this_cycle:
                # Reset to fast polling when activity detected
                poll_interval = min_poll
            else:
                # Gradually increase interval when idle
                poll_interval = min(poll_interval * 1.5, max_poll)
        else:
            break  # All done — return immediately

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


def extract_new_content(before: str, after: str) -> str:
    """Extract only the new content added after 'before' snapshot.

    TUI apps (Claude Code, Codex, Gemini CLI) have static footer/status
    bars at the bottom that don't change between snapshots.  New content
    (input + AI response) is inserted ABOVE the footer.

    Algorithm:
    1. Normalize both snapshots (strip ANSI, spinners, whitespace).
    2. Strip common suffix lines (footer/status bar).
    3. Anchor-match on the remaining content to find new lines.
    4. Fallback to prefix-based diff if anchor fails.
    """
    norm_before = _normalize_for_compare(before)
    norm_after = _normalize_for_compare(after)

    before_lines = norm_before.splitlines()
    after_lines = norm_after.splitlines()

    if not before_lines:
        return norm_after.strip()

    # Step 1: Strip common suffix (TUI footer/status bar)
    common_suffix = 0
    min_len = min(len(before_lines), len(after_lines))
    for i in range(1, min_len + 1):
        if before_lines[-i] == after_lines[-i]:
            common_suffix = i
        else:
            break

    if common_suffix > 0:
        before_trimmed = before_lines[:-common_suffix]
        after_trimmed = after_lines[:-common_suffix]
    else:
        before_trimmed = before_lines
        after_trimmed = after_lines

    if not before_trimmed:
        return "\n".join(after_trimmed).strip()

    # Step 2: Anchor-match on trimmed content (footer removed)
    for anchor_size in range(min(5, len(before_trimmed)), 1, -1):
        anchor = before_trimmed[-anchor_size:]
        for i in range(len(after_trimmed) - anchor_size, -1, -1):
            if after_trimmed[i:i + anchor_size] == anchor:
                new_lines = after_trimmed[i + anchor_size:]
                result = "\n".join(new_lines).strip()
                if result:
                    return result

    # Step 3: Fallback — find common prefix, return the rest
    common_prefix = 0
    min_len = min(len(before_trimmed), len(after_trimmed))
    for i in range(min_len):
        if before_trimmed[i] == after_trimmed[i]:
            common_prefix = i + 1
        else:
            break

    new_lines = after_trimmed[common_prefix:]
    result = "\n".join(new_lines).strip()
    if result:
        return result

    # Final fallback: return full trimmed after
    return "\n".join(after_trimmed).strip()


def send_and_capture(
    pane_target: str,
    message: str,
    capture_lines: int = 500,
    stable_secs: int = 8,
    timeout: int = 300,
) -> str:
    """Send message to a live session and capture only the new response.

    1. Snapshot before
    2. Send message via hex mode
    3. Wait for pane to stabilize
    4. Snapshot after
    5. Return diff (new content only)
    """
    before = capture_pane_content(pane_target, lines=capture_lines)
    send_message_to_pane(pane_target, message)

    # Single-pane idle wait
    start = time.time()
    last_content = capture_pane_content(pane_target, lines=capture_lines)
    last_change = time.time()
    poll = 1.0

    while (time.time() - start) < timeout:
        time.sleep(poll)
        current = capture_pane_content(pane_target, lines=capture_lines)
        if current != last_content:
            last_content = current
            last_change = time.time()
            poll = 1.0  # reset to fast polling
        elif (time.time() - last_change) >= stable_secs:
            break  # stabilized
        else:
            poll = min(poll * 1.5, 5.0)

    after = capture_pane_content(pane_target, lines=capture_lines)
    return extract_new_content(before, after)


def send_and_capture_all(
    pane_targets: dict[str, str],
    messages: dict[str, str],
    capture_lines: int = 500,
    stable_secs: int = 8,
    timeout: int = 300,
) -> dict[str, str]:
    """Send messages to multiple live sessions in parallel and capture responses.

    1. Snapshot all panes (before)
    2. Send messages to all panes
    3. Wait for all panes to stabilize
    4. Snapshot all panes (after)
    5. Return diff per pane
    """
    # 1. Before snapshots
    befores: dict[str, str] = {}
    for model, pane in pane_targets.items():
        befores[model] = capture_pane_content(pane, lines=capture_lines)

    # 2. Send all messages in parallel
    pane_sends = [(pane, messages[model]) for model, pane in pane_targets.items() if model in messages]
    for model, pane in pane_targets.items():
        if model in messages:
            update_pane_status(pane, model, "실행중")
    send_message_to_panes_parallel(pane_sends)

    # 3. Wait for all to stabilize
    wait_for_all_panes_idle(pane_targets, stable_secs=stable_secs, timeout=timeout)

    # 4. After snapshots & diff
    results: dict[str, str] = {}
    for model, pane in pane_targets.items():
        after = capture_pane_content(pane, lines=capture_lines)
        results[model] = extract_new_content(befores[model], after)
        update_pane_status(pane, model, "완료")

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

    output_file = str(get_shared_dir(work_dir) / "synthesis.md")
    return run_ai_cli("claude", prompt, work_dir, output_file)


