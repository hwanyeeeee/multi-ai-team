"""Simple conversation log for the multi-AI chat interface."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from config import AI_MODELS


class ConversationLog:
    """Lightweight log of user messages for /history and JSONL persistence."""

    def __init__(self, work_dir: str):
        shared_dir = Path(work_dir) / "shared"
        shared_dir.mkdir(parents=True, exist_ok=True)
        self.log_file = shared_dir / "chat.jsonl"
        self.entries: list[dict] = []

    def add(self, role: str, content: str, targets: list[str] | None = None) -> None:
        """Record a message."""
        entry = {
            "role": role,
            "content": content,
            "targets": targets,
            "timestamp": datetime.now().isoformat(),
        }
        self.entries.append(entry)
        with open(self.log_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def display(self) -> str:
        """Format log for /history command."""
        if not self.entries:
            return "(No messages yet)"
        lines: list[str] = []
        for e in self.entries:
            ts = e.get("timestamp", "")[:19].split("T")[-1]
            role = e["role"]
            content = e["content"]
            if role == "user":
                targets = e.get("targets")
                suffix = ""
                if targets:
                    suffix = f" [-> {', '.join(targets)}]"
                lines.append(f"[{ts}] You: {content}{suffix}")
            else:
                label = AI_MODELS.get(role, {}).get("label", role)
                lines.append(f"[{ts}] {label}: {content[:200]}")
        return "\n".join(lines)

    def clear(self) -> None:
        """Clear in-memory log."""
        self.entries.clear()
