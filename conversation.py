"""Conversation manager for interactive multi-AI chat."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from config import AI_MODELS, CHAT_HISTORY_MAX_CHARS


class ConversationManager:
    """Manages conversation history and prompt building for chat mode."""

    def __init__(self, work_dir: str, active_models: list[str]):
        self.work_dir = Path(work_dir)
        self.shared_dir = self.work_dir / "shared"
        self.shared_dir.mkdir(parents=True, exist_ok=True)
        self.chat_log = self.shared_dir / "chat.jsonl"
        self.active_models = active_models
        self.history: list[dict] = []

    def add_user_message(self, content: str, targets: list[str] | None = None) -> None:
        """Add a user message to conversation history."""
        entry = {
            "role": "user",
            "content": content,
            "targets": targets,
            "timestamp": datetime.now().isoformat(),
        }
        self.history.append(entry)
        self._save_to_jsonl(entry)

    def add_ai_response(self, model_name: str, content: str) -> None:
        """Add an AI response to conversation history."""
        entry = {
            "role": model_name,
            "content": content,
            "timestamp": datetime.now().isoformat(),
        }
        self.history.append(entry)
        self._save_to_jsonl(entry)

    def build_prompt(self, model_name: str, user_message: str) -> str:
        """Build a full prompt with system context, history, and current message."""
        model_cfg = AI_MODELS[model_name]
        label = model_cfg["label"]
        strengths = ", ".join(model_cfg["strengths"])

        others = [
            AI_MODELS[m]["label"]
            for m in self.active_models
            if m != model_name and m in AI_MODELS
        ]

        parts = [
            f"You are {label}, part of a multi-AI team.",
            f"Your strengths: {strengths}",
            f"Other team members: {', '.join(others)}",
        ]

        history_text = self._get_history_text()
        if history_text:
            parts.append(f"\n=== Conversation History ===\n{history_text}")

        parts.append(f"\n=== Current Message ===\n[User]: {user_message}")
        parts.append(
            "\nRespond helpfully. Be concise. Focus on your strengths."
            "\nIf another AI already covered a point, build on it rather than repeating."
        )

        return "\n".join(parts)

    def build_synthesis_prompt(
        self, user_message: str, responses: dict[str, str]
    ) -> str:
        """Build a synthesis prompt for Claude to summarize all AI responses."""
        resp_text = ""
        for model_name, text in responses.items():
            label = AI_MODELS.get(model_name, {}).get("label", model_name)
            resp_text += f"--- {label} ---\n{text}\n\n"

        return (
            "You are the lead synthesizer for a multi-AI team.\n"
            f"User asked: {user_message}\n\n"
            f"Responses from team members:\n\n{resp_text.strip()}\n\n"
            "Synthesize these into a unified, concise response that:\n"
            "1. Combines the best insights from each\n"
            "2. Resolves any contradictions\n"
            "3. Presents a clear, actionable answer\n"
            "Keep it concise."
        )

    def _get_history_text(self, max_chars: int = CHAT_HISTORY_MAX_CHARS) -> str:
        """Format conversation history as text, trimming old entries to fit."""
        if not self.history:
            return ""

        lines: list[str] = []
        for entry in self.history:
            role = entry["role"]
            content = entry["content"]
            if role == "user":
                lines.append(f"[User]: {content}")
            else:
                label = AI_MODELS.get(role, {}).get("label", role)
                lines.append(f"[{label}]: {content}")

        # Join and trim from the front if too long
        full = "\n".join(lines)
        if len(full) <= max_chars:
            return full

        # Keep the most recent messages that fit
        trimmed: list[str] = []
        char_count = 0
        for line in reversed(lines):
            if char_count + len(line) + 1 > max_chars:
                break
            trimmed.append(line)
            char_count += len(line) + 1
        trimmed.reverse()

        return "...(earlier messages trimmed)...\n" + "\n".join(trimmed)

    def get_history_display(self) -> str:
        """Get formatted history for /history command display."""
        if not self.history:
            return "(No conversation history yet)"

        lines: list[str] = []
        for entry in self.history:
            ts = entry.get("timestamp", "")
            if ts:
                ts = ts.split("T")[1][:8]  # HH:MM:SS
            role = entry["role"]
            content = entry["content"]
            if role == "user":
                lines.append(f"[{ts}] You: {content}")
            else:
                label = AI_MODELS.get(role, {}).get("label", role)
                lines.append(f"[{ts}] {label}: {content[:200]}")
        return "\n".join(lines)

    def clear(self) -> None:
        """Clear conversation history."""
        self.history.clear()

    def _save_to_jsonl(self, entry: dict) -> None:
        """Append an entry to the chat.jsonl log file."""
        with open(self.chat_log, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
