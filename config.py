"""Configuration for Multi-AI Team system."""
from __future__ import annotations

import os
import platform
import subprocess
from pathlib import Path

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
        "args": ["--print"],  # batch mode (one-shot)
        "interactive_args": ["--dangerously-skip-permissions"],
        "strengths": ["complex reasoning", "architecture", "code review", "planning"],
        "label": "Claude (Reasoning/Architecture)",
    },
    "codex": {
        "binary": "codex",
        "wsl_path": None,
        "args": ["exec", "--skip-git-repo-check"],  # batch mode
        "interactive_args": ["--full-auto"],
        "strengths": ["code generation", "fast iteration", "debugging", "testing"],
        "label": "Codex (Code/Analysis)",
    },
    "gemini": {
        "binary": "gemini",
        "wsl_path": None,
        "args": ["-p"],  # batch mode
        "interactive_args": ["--yolo"],
        "strengths": ["research", "long context", "frontend", "documentation"],
        "label": "Gemini (Research/Frontend)",
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
            "You are part of a multi-AI team. Your role: {role}.\n"
            "Task: {task}\n\n"
            "Write your implementation plan. Be specific about:\n"
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
            "You are part of a multi-AI team. Your role: {role}.\n"
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
            "You are part of a multi-AI team. Your role: {role}.\n"
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
            "Synthesize these into ONE final, unified implementation plan that:\n"
            "1. Takes the best ideas from each\n"
            "2. Resolves any conflicts\n"
            "3. Assigns roles based on each AI's strengths\n"
            "4. Provides a clear execution order\n\n"
            "Output the final plan."
        ),
    },
]

# Tmux settings
TMUX_SESSION_PREFIX = "multi-ai-team"

# Timeouts
AI_RESPONSE_TIMEOUT_SEC = 120

# Chat mode settings
CHAT_HISTORY_MAX_CHARS = 8000
CHAT_SYNTHESIS_ENABLED = True


def to_wsl_path(win_path: str) -> str:
    """Convert Windows path to WSL path."""
    p = win_path.replace("\\", "/")
    if len(p) >= 2 and p[1] == ":":
        drive = p[0].lower()
        return f"/mnt/{drive}{p[2:]}"
    return p
