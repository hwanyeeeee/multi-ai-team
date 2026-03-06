from __future__ import annotations

from round_manager import RoundManager


def test_build_prompt_plan_round(tmp_path) -> None:
    rm = RoundManager("Implement API", str(tmp_path), active_models=["claude", "codex"])
    prompt = rm.build_prompt(0, "claude")

    assert "Task: Implement API" in prompt
    assert "You are claude" in prompt


def test_build_prompt_review_round_excludes_self_plan(tmp_path) -> None:
    rm = RoundManager("Task", str(tmp_path), active_models=["claude", "codex", "gemini"])
    rm.store_round_results(0, {"claude": "plan-c", "codex": "plan-x", "gemini": "plan-g"})

    prompt = rm.build_prompt(1, "claude")
    assert "plan-x" in prompt
    assert "plan-g" in prompt
    assert "plan-c" not in prompt


def test_build_prompt_revise_round_uses_own_plan_and_peer_reviews(tmp_path) -> None:
    rm = RoundManager("Task", str(tmp_path), active_models=["claude", "codex", "gemini"])
    rm.store_round_results(0, {"claude": "my-plan", "codex": "x-plan", "gemini": "g-plan"})
    rm.store_round_results(1, {"claude": "self-review", "codex": "review-1", "gemini": "review-2"})

    prompt = rm.build_prompt(2, "claude")
    assert "my-plan" in prompt
    assert "review-1" in prompt
    assert "review-2" in prompt
    assert "self-review" not in prompt


def test_build_prompt_synthesize_round_collects_revised_plans(tmp_path) -> None:
    rm = RoundManager("Task", str(tmp_path), active_models=["claude", "codex", "gemini"])
    rm.store_round_results(2, {"claude": "c-revised", "codex": "x-revised"})

    prompt = rm.build_prompt(3, "claude")
    assert "c-revised" in prompt
    assert "x-revised" in prompt
    assert "(Revised Plan)" in prompt
