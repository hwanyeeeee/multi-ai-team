"""Round Manager - controls multi-round AI collaboration protocol."""
from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from config import ROUNDS, AI_MODELS


class RoundManager:
    """Manages the multi-round collaboration protocol."""

    def __init__(self, task: str, work_dir: str, active_models: list[str] | None = None):
        self.task = task
        self.work_dir = Path(work_dir)
        self.shared_dir = self.work_dir / "shared"
        self.shared_dir.mkdir(parents=True, exist_ok=True)
        self.chat_log = self.shared_dir / "chat.jsonl"
        self.round_results: dict[str, dict[str, str]] = {}
        self.active_models = active_models or list(AI_MODELS.keys())

    def get_round_config(self, round_index: int) -> dict:
        """Get configuration for a specific round."""
        if round_index >= len(ROUNDS):
            return None
        return ROUNDS[round_index]

    def build_prompt(self, round_index: int, model_name: str) -> str:
        """Build prompt for a specific model in a specific round."""
        round_cfg = self.get_round_config(round_index)
        if not round_cfg:
            return ""

        role = AI_MODELS[model_name]["label"]
        template = round_cfg["prompt_template"]

        if round_cfg["name"] == "plan":
            return template.format(role=role, task=self.task)

        elif round_cfg["name"] == "review":
            # Collect other models' plans from round 0
            plans = self.round_results.get(0, {})
            other_plans = ""
            for m, text in plans.items():
                if m != model_name:
                    label = AI_MODELS[m]["label"]
                    other_plans += f"--- {label} ---\n{text}\n\n"
            return template.format(
                role=role, task=self.task, other_plans=other_plans.strip()
            )

        elif round_cfg["name"] == "revise":
            my_plan = self.round_results.get(0, {}).get(model_name, "(no plan)")
            # Collect reviews about this model from round 1
            reviews = self.round_results.get(1, {})
            review_text = ""
            for m, text in reviews.items():
                if m != model_name:
                    label = AI_MODELS[m]["label"]
                    review_text += f"--- Review by {label} ---\n{text}\n\n"
            return template.format(
                role=role,
                task=self.task,
                my_plan=my_plan,
                reviews=review_text.strip(),
            )

        elif round_cfg["name"] == "synthesize":
            revised = self.round_results.get(2, {})
            all_plans = ""
            for m, text in revised.items():
                label = AI_MODELS[m]["label"]
                all_plans += f"--- {label} (Revised Plan) ---\n{text}\n\n"
            return template.format(task=self.task, all_revised_plans=all_plans.strip())

        return ""

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
