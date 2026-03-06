"""Round Manager - controls multi-round AI collaboration protocol."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Callable
from config import ROUNDS, AI_MODELS, get_shared_dir


class RoundManager:
    """Manages the multi-round collaboration protocol."""

    def __init__(self, task: str, work_dir: str, active_models: list[str] | None = None):
        self.task = task
        self.work_dir = Path(work_dir)
        self.shared_dir = get_shared_dir(work_dir)
        self.chat_log = self.shared_dir / "chat.jsonl"
        self.round_results: dict[int, dict[str, str]] = {}
        self.active_models = active_models or list(AI_MODELS.keys())
        self._prompt_builders: dict[str, Callable[[str, str, str], str]] = {
            "plan": self._build_plan_prompt,
            "review": self._build_review_prompt,
            "revise": self._build_revise_prompt,
            "synthesize": self._build_synthesize_prompt,
        }

    def get_round_config(self, round_index: int) -> dict | None:
        """Get configuration for a specific round."""
        if round_index < 0 or round_index >= len(ROUNDS):
            return None
        return ROUNDS[round_index]

    def build_prompt(self, round_index: int, model_name: str) -> str:
        """Build prompt for a specific model in a specific round."""
        round_cfg = self.get_round_config(round_index)
        if not round_cfg:
            return ""

        if model_name not in AI_MODELS:
            return ""

        template = round_cfg["prompt_template"]
        builder = self._prompt_builders.get(round_cfg["name"])
        if not builder:
            return ""
        return builder(model_name, model_name, template)

    def _build_plan_prompt(self, _model_name: str, name: str, template: str) -> str:
        return template.format(name=name, task=self.task)

    def _build_review_prompt(self, model_name: str, name: str, template: str) -> str:
        # Collect other models' plans from round 0
        plans = self.round_results.get(0, {})
        other_plans = self._join_model_sections(plans, exclude_model=model_name)
        return template.format(name=name, task=self.task, other_plans=other_plans)

    def _build_revise_prompt(self, model_name: str, name: str, template: str) -> str:
        my_plan = self.round_results.get(0, {}).get(model_name, "(no plan)")

        # Collect reviews from round 1 written by other models
        reviews = self.round_results.get(1, {})
        chunks = []
        for reviewer, text in reviews.items():
            if reviewer == model_name:
                continue
            reviewer_name = AI_MODELS[reviewer]["label"]
            chunks.append(f"--- Review by {reviewer_name} ---\n{text}")
        review_text = "\n\n".join(chunks)
        return template.format(name=name, task=self.task, my_plan=my_plan, reviews=review_text)

    def _build_synthesize_prompt(self, _model_name: str, _role: str, template: str) -> str:
        revised = self.round_results.get(2, {})
        all_plans = self._join_model_sections(revised, title_suffix=" (Revised Plan)")
        return template.format(task=self.task, all_revised_plans=all_plans)

    def _join_model_sections(
        self,
        items: dict[str, str],
        exclude_model: str | None = None,
        title_suffix: str = "",
    ) -> str:
        chunks = []
        for model_name, text in items.items():
            if model_name == exclude_model:
                continue
            label = AI_MODELS.get(model_name, {}).get("label", model_name)
            chunks.append(f"--- {label}{title_suffix} ---\n{text}")
        return "\n\n".join(chunks)

    def get_output_file(self, round_index: int, model_name: str) -> str:
        """Get output file path for a round/model."""
        round_name = ROUNDS[round_index]["name"]
        return str(self.shared_dir / f"round_{round_index}_{round_name}_{model_name}.txt")

    def store_round_results(self, round_index: int, results: dict[str, str]) -> None:
        """Store results for a completed round."""
        self.round_results[round_index] = results
        self._append_chat_log(round_index, results)

    def _append_chat_log(self, round_index: int, results: dict[str, str]) -> None:
        """Append round results to shared chat log."""
        round_name = ROUNDS[round_index]["name"]
        for model_name, text in results.items():
            entry = {
                "timestamp": datetime.now().isoformat(),
                "round": round_index,
                "round_name": round_name,
                "model": model_name,
                "role": AI_MODELS.get(model_name, {}).get("label", model_name),
                "content": text[:2000],  # Truncate for log
            }
            with open(self.chat_log, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def get_participating_models(self, round_index: int) -> list[str]:
        """Get which models participate in a round (filtered by availability)."""
        round_cfg = self.get_round_config(round_index)
        if not round_cfg:
            return []
        if round_cfg["name"] == "synthesize":
            # Prefer claude for synthesis; fallback to first available
            if "claude" in self.active_models:
                return ["claude"]
            return [self.active_models[0]] if self.active_models else []
        return [m for m in ["claude", "codex", "gemini"] if m in self.active_models]

    def generate_summary(self) -> str:
        """Generate a summary of the entire collaboration."""
        lines = []
        for ri, round_cfg in enumerate(ROUNDS):
            results = self.round_results.get(ri, {})
            if not results:
                continue
            lines.append(f"\n{'='*60}")
            lines.append(f"Round {ri + 1}: {round_cfg['description']}")
            lines.append(f"{'='*60}")
            for model_name, text in results.items():
                label = AI_MODELS.get(model_name, {}).get("label", model_name)
                lines.append(f"\n--- {label} ---")
                lines.append(text[:1000])
        return "\n".join(lines)
