from __future__ import annotations

import pytest

from config import AI_MODELS, ROUNDS, validate_config


def test_validate_config_accepts_default() -> None:
    validate_config()


def test_validate_config_rejects_missing_model_keys() -> None:
    broken_models = {
        "codex": {
            "binary": "codex",
            "args": [],
            "interactive_args": [],
            # label intentionally missing
        }
    }
    with pytest.raises(ValueError, match="missing keys"):
        validate_config(ai_models=broken_models, rounds=ROUNDS)


def test_validate_config_rejects_missing_round_placeholders() -> None:
    broken_rounds = list(ROUNDS)
    broken_rounds[0] = {
        "name": "plan",
        "description": "broken",
        "prompt_template": "Task only: {task}",
    }
    with pytest.raises(ValueError, match="missing '\\{name\\}'"):
        validate_config(ai_models=AI_MODELS, rounds=broken_rounds)
