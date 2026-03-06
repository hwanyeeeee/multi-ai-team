"""Configuration for Multi-AI Team system."""
from __future__ import annotations

import json
import os
import platform
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

# Detect if we are running inside WSL already
IS_WSL = (
    platform.system() == "Linux"
    and "microsoft" in (open("/proc/version").read().lower() if os.path.exists("/proc/version") else "")
)

# WSL binary search paths (absolute to avoid PATH issues with Windows spaces/parens)
_WSL_BIN_SEARCH_PATHS = [
    "/home/best1pjh/.npm-global/bin",
    "/usr/local/bin",
    "/usr/bin",
]


def wsl_prefix() -> list[str]:
    """Return ['wsl'] when on Windows, [] when already inside WSL."""
    return [] if IS_WSL else ["wsl"]

# AI CLI commands (WSL environment)
AI_MODELS = {
    "claude": {
        "binary": "claude",
        "wsl_path": None,  # Resolved at runtime by detect_available_models()
        "args": ["--print", "--dangerously-skip-permissions"],  # batch mode (one-shot)
        "interactive_args": ["--dangerously-skip-permissions"],
        "label": "Claude",
    },
    "codex": {
        "binary": "codex",
        "wsl_path": None,
        "args": ["exec", "--skip-git-repo-check", "--dangerously-bypass-approvals-and-sandbox"],  # batch mode
        "interactive_args": ["--dangerously-bypass-approvals-and-sandbox"],
        "label": "Codex",
    },
    "gemini": {
        "binary": "gemini",
        "wsl_path": None,
        "args": ["--yolo", "-p"],  # batch mode — -p must be last (takes prompt as next arg)
        "interactive_args": ["--yolo"],
        "label": "Gemini",
    },
}


def detect_available_models() -> dict[str, bool]:
    """Check which AI CLIs are available in WSL using absolute path checks."""
    available = {}
    for name in AI_MODELS:
        binary = AI_MODELS[name]["binary"]
        found = False
        for search_dir in _WSL_BIN_SEARCH_PATHS:
            full_path = f"{search_dir}/{binary}"
            try:
                r = subprocess.run(
                    wsl_prefix() + ["test", "-x", full_path],
                    capture_output=True, text=True, timeout=5,
                )
                if r.returncode == 0:
                    AI_MODELS[name]["wsl_path"] = full_path
                    found = True
                    break
            except Exception:
                pass
        available[name] = found
    return available


def get_wsl_binary(model_name: str) -> str:
    """Get the resolved WSL absolute binary path for a model."""
    path = AI_MODELS[model_name].get("wsl_path")
    if path:
        return path
    return AI_MODELS[model_name]["binary"]

# Round protocol
ROUNDS = [
    {
        "name": "plan",
        "description": "각자 계획안 작성",
        "prompt_template": (
            "You are {name}, part of a multi-AI team.\n"
            "Task: {task}\n\n"
            "Write your plan. Be specific about:\n"
            "1. Your approach\n"
            "2. Key decisions and why\n"
            "3. Potential risks\n"
            "Keep it concise (under 500 words)."
        ),
    },
    {
        "name": "review",
        "description": "다른 AI들의 계획을 리뷰",
        "prompt_template": (
            "You are {name}, part of a multi-AI team.\n"
            "Task: {task}\n\n"
            "Review these plans from your teammates:\n\n"
            "{other_plans}\n\n"
            "For each plan, provide:\n"
            "1. Score (1-10)\n"
            "2. Strengths\n"
            "3. Weaknesses\n"
            "4. Suggested improvements\n"
            "Keep it concise."
        ),
    },
    {
        "name": "revise",
        "description": "리뷰 반영하여 최종안 수정",
        "prompt_template": (
            "You are {name}, part of a multi-AI team.\n"
            "Task: {task}\n\n"
            "Your original plan:\n{my_plan}\n\n"
            "Reviews from teammates:\n{reviews}\n\n"
            "Revise your plan based on the feedback. "
            "Incorporate valid suggestions and explain what you changed and why."
        ),
    },
    {
        "name": "synthesize",
        "description": "최종 통합 (Claude only)",
        "prompt_template": (
            "You are the lead synthesizer for a multi-AI team.\n"
            "Task: {task}\n\n"
            "Final revised plans from all team members:\n\n"
            "{all_revised_plans}\n\n"
            "Synthesize these into ONE final, unified plan that:\n"
            "1. Takes the best ideas from each\n"
            "2. Resolves any conflicts\n"
            "3. Assigns clear responsibilities\n"
            "4. Provides a clear execution order\n\n"
            "Output the final plan."
        ),
    },
]

# Context management thresholds (characters)
# Approximate: 1 token ≈ 4 chars for English, ≈ 2 chars for Korean
CONTEXT_WARNING_CHARS = 80000      # ~20K tokens, warn user
CONTEXT_RESET_CHARS = 150000       # ~37K tokens, auto-reset
CONTEXT_CHECK_INTERVAL = 5         # check every N messages

CONTEXT_RESET_SUMMARY_PROMPT = (
    "Below is a conversation history from {name}. "
    "Summarize the key context, decisions, and current task state in under 200 words. "
    "This summary will be used to restore context after a session reset. "
    "Focus on: what task is being worked on, what was decided, what remains to do.\n\n"
    "{conversation}"
)

# Tmux settings
TMUX_SESSION_PREFIX = "multi-ai-team"

# Timeouts
AI_RESPONSE_TIMEOUT_SEC = 1800

# Chat mode settings
CHAT_HISTORY_MAX_CHARS = 8000
CHAT_SYNTHESIS_ENABLED = True

# Orchestration prompt templates (/task auto-delegation)
ORCH_PLAN_PROMPT = (
    "You are {name}, part of a multi-AI team.\n\n"
    "Task: {task}\n\n"
    "Write a brief plan for how YOU would contribute to this task.\n"
    "Be specific and actionable. Keep it under 200 words.\n"
    "Respond in the same language as the task."
)

ORCH_ASSIGN_PROMPT = (
    "You are the task coordinator for a multi-AI team.\n"
    "Task: {task}\n\n"
    "Plans from each AI:\n{all_plans}\n\n"
    "Based on these plans, assign a specific task to each AI.\n"
    "Output in this exact format (one line per AI, parseable):\n"
    "[claude] Specific instruction for Claude\n"
    "[codex] Specific instruction for Codex\n"
    "[gemini] Specific instruction for Gemini\n\n"
    "Each instruction should be a clear, actionable directive.\n"
    "Only assign tasks to available AIs: {active_models}\n"
    "Respond in the same language as the task."
)

ORCH_FINAL_PROMPT = (
    "Task: {task}\n\n"
    "Role assignments:\n{assignments}\n\n"
    "Execution results from each AI:\n{all_results}\n\n"
    "Synthesize into a comprehensive final output.\n"
    "Combine all work into a coherent result.\n"
    "Note any gaps or issues.\n"
    "Respond in the same language as the task."
)

# Batch discussion prompts (/batch AI-to-AI debate)
BATCH_OPEN_PROMPT = (
    "You are {name}, part of a multi-AI team discussion.\n\n"
    "Topic: {topic}\n\n"
    "Share your perspective on this topic.\n"
    "Be specific and opinionated. Keep it under 200 words.\n"
    "Respond in the same language as the topic."
)

BATCH_REPLY_PROMPT = (
    "You are {name}, part of a multi-AI team discussion.\n\n"
    "Topic: {topic}\n\n"
    "Previous discussion:\n{history}\n\n"
    "Before responding, analyze the other AIs' arguments:\n"
    "1. What are the strongest points from each AI?\n"
    "2. Where do you agree or disagree, and why?\n"
    "3. What perspectives are missing?\n\n"
    "Then provide your response, building on this analysis.\n"
    "Be specific and constructive. Keep it under 250 words.\n"
    "Respond in the same language as the topic."
)

# Team context sent to each AI when starting interactive mode.
# IMPORTANT: Must be a single line — tmux send-keys treats \n as Enter,
# which would submit partial messages in TUI-based CLIs.
INTERACTIVE_TEAM_CONTEXT = (
    "[Team Context] You are {name}, part of a 3-AI collaboration team ({teammates}). "
    "A human operator sends messages from the chat pane and may address you with @{name}. "
    "Other AIs cannot see your responses directly — the operator relays context or uses /synth. "
    "IMPORTANT: Do NOT take any action on your own. Wait for the operator to give you a task. "
    "Do NOT read, explore, or modify any files until explicitly asked. "
    "Be concise and actionable. Respond in the same language as the operator. "
    "Acknowledge briefly that you are ready, then WAIT."
)

BATCH_CONSENSUS_PROMPT = (
    "Topic: {topic}\n\n"
    "Discussion so far:\n{history}\n\n"
    "Have the participants reached consensus on the key points?\n"
    "Answer ONLY 'CONVERGED' or 'NOT_CONVERGED: <brief reason>'."
)

BATCH_SYNTHESIS_PROMPT = (
    "Topic: {topic}\n\n"
    "Full discussion ({rounds} rounds):\n{history}\n\n"
    "Synthesize the discussion into a final summary.\n"
    "Include: key agreements, remaining disagreements, and actionable conclusions.\n"
    "Respond in the same language as the topic."
)


# ---------------------------------------------------------------------------
# Session directory management
# ---------------------------------------------------------------------------
_current_session_dir: Path | None = None


def create_session_dir(work_dir: str) -> Path:
    """Create a timestamped session directory under shared/.

    Called once at session start.  All subsequent ``get_shared_dir()`` calls
    will return this directory instead of the bare ``shared/``.
    """
    global _current_session_dir
    ts = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    session_dir = Path(work_dir) / "shared" / ts
    session_dir.mkdir(parents=True, exist_ok=True)
    _current_session_dir = session_dir
    # Write initial session.json
    (session_dir / "session.json").write_text(
        json.dumps(
            {"started": datetime.now().isoformat(), "models": [], "topic": ""},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return session_dir


def set_session_dir(session_dir: str) -> None:
    """Restore a previously created session directory (for subprocess use).

    Called by chat_loop.py which runs in a separate process and cannot
    inherit the global ``_current_session_dir`` set by run.py.
    """
    global _current_session_dir
    p = Path(session_dir)
    p.mkdir(parents=True, exist_ok=True)
    _current_session_dir = p


def get_shared_dir(work_dir: str) -> Path:
    """Return the current session directory, falling back to bare shared/."""
    if _current_session_dir is not None:
        return _current_session_dir
    d = Path(work_dir) / "shared"
    d.mkdir(parents=True, exist_ok=True)
    return d


def update_session_meta(
    topic: str = "",
    models: list[str] | None = None,
) -> None:
    """Update session.json with topic and/or model list."""
    if _current_session_dir is None:
        return
    meta_file = _current_session_dir / "session.json"
    if not meta_file.exists():
        return
    meta = json.loads(meta_file.read_text(encoding="utf-8"))
    if topic and not meta.get("topic"):
        meta["topic"] = topic
    if models is not None:
        meta["models"] = models
    meta_file.write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def validate_config(
    ai_models: Mapping[str, Mapping[str, Any]] | None = None,
    rounds: Sequence[Mapping[str, Any]] | None = None,
) -> None:
    """Validate critical runtime configuration.

    Raises:
        ValueError: If any required config value is missing or malformed.
    """
    cfg_models = ai_models if ai_models is not None else AI_MODELS
    cfg_rounds = rounds if rounds is not None else ROUNDS

    errors: list[str] = []

    if not cfg_models:
        errors.append("AI_MODELS must not be empty.")
    else:
        required_model_keys = {"binary", "args", "interactive_args", "label"}
        for model_name, model_cfg in cfg_models.items():
            missing = required_model_keys - set(model_cfg.keys())
            if missing:
                errors.append(
                    f"AI_MODELS['{model_name}'] missing keys: {sorted(missing)}"
                )
            if not isinstance(model_cfg.get("binary"), str) or not model_cfg.get("binary"):
                errors.append(f"AI_MODELS['{model_name}']['binary'] must be a non-empty string.")
            if not isinstance(model_cfg.get("args"), list):
                errors.append(f"AI_MODELS['{model_name}']['args'] must be a list.")
            if not isinstance(model_cfg.get("interactive_args"), list):
                errors.append(f"AI_MODELS['{model_name}']['interactive_args'] must be a list.")
            if not isinstance(model_cfg.get("label"), str) or not model_cfg.get("label"):
                errors.append(f"AI_MODELS['{model_name}']['label'] must be a non-empty string.")

    expected_rounds = {
        "plan": {"name", "task"},
        "review": {"name", "task", "other_plans"},
        "revise": {"name", "task", "my_plan", "reviews"},
        "synthesize": {"task", "all_revised_plans"},
    }

    if not cfg_rounds:
        errors.append("ROUNDS must not be empty.")
    else:
        seen_rounds: set[str] = set()
        for idx, round_cfg in enumerate(cfg_rounds):
            round_name = round_cfg.get("name")
            if not isinstance(round_name, str) or not round_name:
                errors.append(f"ROUNDS[{idx}] has invalid 'name'.")
                continue

            seen_rounds.add(round_name)
            if not isinstance(round_cfg.get("description"), str) or not round_cfg.get("description"):
                errors.append(f"ROUNDS[{idx}]('{round_name}') has invalid 'description'.")
            template = round_cfg.get("prompt_template")
            if not isinstance(template, str) or not template:
                errors.append(f"ROUNDS[{idx}]('{round_name}') has invalid 'prompt_template'.")
                continue

            for placeholder in expected_rounds.get(round_name, set()):
                if f"{{{placeholder}}}" not in template:
                    errors.append(
                        f"ROUNDS[{idx}]('{round_name}') prompt_template missing '{{{placeholder}}}'."
                    )

        missing_rounds = [name for name in expected_rounds if name not in seen_rounds]
        if missing_rounds:
            errors.append(f"ROUNDS missing required round(s): {missing_rounds}")

    if errors:
        raise ValueError("Invalid configuration:\n- " + "\n- ".join(errors))


def to_wsl_path(win_path: str) -> str:
    """Convert Windows path to WSL path."""
    p = win_path.replace("\\", "/")
    if len(p) >= 2 and p[1] == ":":
        drive = p[0].lower()
        return f"/mnt/{drive}{p[2:]}"
    return p
