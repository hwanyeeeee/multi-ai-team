"""Conversation log and shared context for the multi-AI chat interface."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from config import AI_MODELS, get_shared_dir


class ConversationLog:
    """Lightweight log of user messages for /history and JSONL persistence."""

    def __init__(self, work_dir: str):
        shared_dir = get_shared_dir(work_dir)
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


class EventStream:
    """Append-only JSONL event log for structured tracking."""

    EVENT_TYPES = ("action", "observation", "message", "status", "error")

    def __init__(self, work_dir: str):
        self.log_file = get_shared_dir(work_dir) / "events.jsonl"

    def log(
        self,
        event_type: str,
        model: str = "",
        detail: str = "",
        phase: str = "",
        metadata: dict | None = None,
    ) -> None:
        entry = {
            "type": event_type,
            "model": model,
            "detail": detail,
            "phase": phase,
            "timestamp": datetime.now().isoformat(),
            "metadata": metadata or {},
        }
        with open(self.log_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def recent(self, n: int = 20) -> list[dict]:
        if not self.log_file.exists():
            return []
        lines = self.log_file.read_text(encoding="utf-8").strip().splitlines()
        return [json.loads(line) for line in lines[-n:]]


class SharedContext:
    """Structured context manager for passing AI responses between agents.

    Each AI's response is stored with metadata (model, round, timestamp,
    error info) so that other AIs receive rich, structured context rather
    than raw text blobs.

    Usage::

        ctx = SharedContext(work_dir)
        ctx.add_response("claude", ai_result, round_name="plan")
        ctx.add_response("gemini", ai_result, round_name="plan")

        # Build context string for codex (excludes its own responses)
        prompt_context = ctx.build_context_for("codex")
    """

    def __init__(self, work_dir: str):
        shared_dir = get_shared_dir(work_dir)
        self.context_file = shared_dir / "shared_context.json"
        self._responses: list[dict] = []
        self._load()

    # -- Recording ------------------------------------------------------------

    def add_response(
        self,
        model_name: str,
        content: str,
        round_name: str = "",
        metadata: dict | None = None,
    ) -> None:
        """Record an AI's response with structured metadata.

        ``content`` can be a plain ``str`` or an ``AIResult``.  When an
        ``AIResult`` is passed its error fields are automatically captured.
        """
        # Detect structured AIResult (import-free duck typing)
        is_error = getattr(content, "is_error", False)
        error_info: dict | None = None
        if is_error:
            error_info = {
                "error_type": getattr(content, "error_type", None),
                "error_detail": getattr(content, "error_detail", None),
                "retry_count": getattr(content, "retry_count", 0),
                "max_retries": getattr(content, "max_retries", 0),
            }

        entry = {
            "model": model_name,
            "label": AI_MODELS.get(model_name, {}).get("label", model_name),
            "content": str(content),
            "round": round_name,
            "timestamp": datetime.now().isoformat(),
            "is_error": is_error,
            "error_info": error_info,
            "metadata": metadata or {},
        }
        self._responses.append(entry)
        self._persist()

    # -- Context building -----------------------------------------------------

    def build_context_for(
        self,
        target_model: str,
        round_name: str | None = None,
        max_chars: int = 8000,
    ) -> str:
        """Build a structured context block from other AIs' responses.

        The returned string is ready to be injected into a prompt so that
        ``target_model`` can see what its teammates said.  Its own responses
        are excluded.  Optionally filter by ``round_name``.
        """
        others = [
            r for r in self._responses
            if r["model"] != target_model
            and (round_name is None or r["round"] == round_name)
        ]
        if not others:
            return ""

        header = "=== Context from other AI team members ===\n"
        parts: list[str] = [header]
        char_count = len(header)

        for resp in others:
            # Section header
            section_hdr = f"--- {resp['label']}"
            if resp["round"]:
                section_hdr += f" (Round: {resp['round']})"
            section_hdr += f" [{resp['timestamp'][:19]}] ---\n"

            # Error badge
            if resp["is_error"] and resp["error_info"]:
                ei = resp["error_info"]
                section_hdr += (
                    f"[ERROR: {ei['error_type']} | "
                    f"detail: {ei['error_detail']} | "
                    f"retries: {ei['retry_count']}/{ei['max_retries']}]\n"
                )

            body = resp["content"]
            remaining = max_chars - char_count - len(section_hdr) - 20
            if remaining <= 0:
                break
            if len(body) > remaining:
                body = body[:remaining] + "...(truncated)"

            section = section_hdr + body + "\n"
            parts.append(section)
            char_count += len(section)

        return "\n".join(parts)

    # -- Querying -------------------------------------------------------------

    def get_responses(
        self,
        model_name: str | None = None,
        round_name: str | None = None,
    ) -> list[dict]:
        """Return stored responses, optionally filtered by model and/or round."""
        results = self._responses
        if model_name is not None:
            results = [r for r in results if r["model"] == model_name]
        if round_name is not None:
            results = [r for r in results if r["round"] == round_name]
        return results

    def get_error_summary(self) -> list[dict]:
        """Return only entries that recorded an error."""
        return [r for r in self._responses if r["is_error"]]

    def get_latest_response(self, model_name: str) -> dict | None:
        """Return the most recent response from a given model."""
        for resp in reversed(self._responses):
            if resp["model"] == model_name:
                return resp
        return None

    # -- Lifecycle ------------------------------------------------------------

    def clear(self) -> None:
        """Clear all stored context (in-memory and on disk)."""
        self._responses.clear()
        if self.context_file.exists():
            self.context_file.unlink()

    # -- Persistence ----------------------------------------------------------

    def _persist(self) -> None:
        self.context_file.write_text(
            json.dumps(self._responses, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _load(self) -> None:
        if self.context_file.exists():
            try:
                self._responses = json.loads(
                    self.context_file.read_text(encoding="utf-8")
                )
            except (json.JSONDecodeError, OSError):
                self._responses = []
