from __future__ import annotations

import json

from round_manager import RoundManager


def test_store_round_results_persists_progress_and_chat_log(tmp_path) -> None:
    rm = RoundManager("Task", str(tmp_path), active_models=["claude"])

    rm.store_round_results(0, {"claude": "first-pass"})
    rm.store_round_results(1, {"claude": "review-pass"})

    assert rm.round_results[0]["claude"] == "first-pass"
    assert rm.round_results[1]["claude"] == "review-pass"

    lines = rm.chat_log.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    second = json.loads(lines[1])
    assert first["round"] == 0
    assert second["round"] == 1
    assert first["content"] == "first-pass"


def test_get_participating_models_for_synthesize_prefers_claude(tmp_path) -> None:
    rm = RoundManager("Task", str(tmp_path), active_models=["gemini", "claude", "codex"])
    assert rm.get_participating_models(3) == ["claude"]


def test_get_participating_models_for_synthesize_falls_back_first_active(tmp_path) -> None:
    rm = RoundManager("Task", str(tmp_path), active_models=["gemini", "codex"])
    assert rm.get_participating_models(3) == ["gemini"]
