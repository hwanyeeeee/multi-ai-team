"""Auto Task Orchestrator - 3-phase pipeline for multi-AI collaboration.

Usage: /task <description> in chat_loop
Phase 1: Each AI plans (batch, parallel)
Phase 2: Claude assigns roles (batch, user confirms)
Phase 3: Execute in panes, wait, synthesize
"""
from __future__ import annotations

import re
import concurrent.futures
from pathlib import Path

from config import (
    AI_MODELS,
    ORCH_PLAN_PROMPT,
    ORCH_ASSIGN_PROMPT,
    ORCH_FINAL_PROMPT,
    BATCH_OPEN_PROMPT,
    BATCH_REPLY_PROMPT,
    BATCH_CONSENSUS_PROMPT,
    BATCH_SYNTHESIS_PROMPT,
)
from ai_worker import (
    run_ai_cli,
    send_message_to_pane,
    wait_for_all_panes_idle,
)
from conversation import SharedContext


class TaskOrchestrator:
    """Orchestrate a task across multiple AI agents."""

    def __init__(
        self,
        pane_map: dict[str, str],
        work_dir: str,
        active_models: list[str],
    ):
        self.pane_map = pane_map
        self.work_dir = work_dir
        self.active_models = active_models
        self.shared_ctx = SharedContext(work_dir)

    def run(self, task: str) -> str:
        """Full orchestration pipeline: plan -> assign -> execute."""
        # Phase 1: Planning
        plans = self._plan(task)
        if not plans:
            return "[Error] No plans received from any AI."

        # Phase 2: Role assignment + user confirmation
        assignments = self._assign(task, plans)
        if assignments is None:
            return "[Cancelled] User declined the task plan."

        # Phase 3: Execute + synthesize
        return self._execute(task, assignments)

    # -- Phase 1: Planning --------------------------------------------------

    def _plan(self, task: str) -> dict[str, str]:
        """Ask each AI to draft a plan (batch, parallel)."""
        print(f"\n  [Phase 1/3] Planning - each AI drafting a plan...")

        def _get_plan(model: str) -> tuple[str, str]:
            cfg = AI_MODELS[model]
            prompt = ORCH_PLAN_PROMPT.format(
                label=cfg["label"],
                strengths=", ".join(cfg["strengths"]),
                task=task,
            )
            out_file = str(Path(self.work_dir) / "shared" / f"orch_plan_{model}.md")
            result = run_ai_cli(model, prompt, self.work_dir, out_file)
            return model, result

        plans: dict[str, str] = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            futures = {pool.submit(_get_plan, m): m for m in self.active_models}
            for future in concurrent.futures.as_completed(futures):
                model, result = future.result()
                label = AI_MODELS[model]["label"]
                is_err = getattr(result, "is_error", False)
                status = "Done" if not is_err else result[:40]
                print(f"    {label}: {status}")
                plans[model] = result
                self.shared_ctx.add_response(model, result, round_name="plan")

        return plans

    # -- Phase 2: Role assignment -------------------------------------------

    def _assign(self, task: str, plans: dict[str, str]) -> dict[str, str] | None:
        """Have Claude assign roles based on plans. Returns None if user declines."""
        print(f"\n  [Phase 2/3] Assigning Roles...")

        # Build combined plans text
        all_plans = "\n\n".join(
            f"=== {AI_MODELS[m]['label']} ===\n{text}"
            for m, text in plans.items()
        )

        prompt = ORCH_ASSIGN_PROMPT.format(
            task=task,
            all_plans=all_plans,
            model_strengths=self._format_strengths(),
            active_models=", ".join(self.active_models),
        )
        out_file = str(Path(self.work_dir) / "shared" / "orch_assign.md")
        raw = run_ai_cli("claude", prompt, self.work_dir, out_file)

        assignments = self._parse_assignments(raw)

        # Show plan to user
        print("  ┌──────────────────────────────────────────┐")
        print("  │  Task Plan                               │")
        for model, instruction in assignments.items():
            label = AI_MODELS.get(model, {}).get("label", model)
            # Truncate long instructions for display
            short = instruction[:50] + "..." if len(instruction) > 50 else instruction
            print(f"  │  {label}: {short}")
        print("  └──────────────────────────────────────────┘")

        try:
            confirm = input("  Proceed? (y/n): ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            confirm = "n"

        if confirm != "y":
            return None
        return assignments

    # -- Phase 3: Execute + synthesize --------------------------------------

    def _execute(self, task: str, assignments: dict[str, str]) -> str:
        """Send instructions to panes, wait for responses, synthesize."""
        print(f"\n  [Phase 3/3] Executing...")

        # Send instructions to each AI's interactive pane
        sent_models = []
        for model, instruction in assignments.items():
            pane = self.pane_map.get(model)
            if pane:
                send_message_to_pane(pane, instruction)
                label = AI_MODELS[model]["label"]
                print(f"    -> {label}: Delegated")
                sent_models.append(model)

        if not sent_models:
            return "[Error] No AI panes available for execution."

        # Wait for all panes to finish
        print("    Waiting for AI responses...")
        pane_targets = {m: self.pane_map[m] for m in sent_models if m in self.pane_map}
        responses = wait_for_all_panes_idle(pane_targets, timeout=120)

        # Build results text
        all_results = "\n\n".join(
            f"=== {AI_MODELS[m]['label']} ===\n{responses.get(m, '(no response)')}"
            for m in sent_models
        )

        # Build assignments text
        assign_text = "\n".join(
            f"- {AI_MODELS[m]['label']}: {assignments[m]}"
            for m in sent_models
        )

        # Synthesize with Claude
        print("    Synthesizing final result...")
        prompt = ORCH_FINAL_PROMPT.format(
            task=task,
            assignments=assign_text,
            all_results=all_results,
        )
        out_file = str(Path(self.work_dir) / "shared" / "orch_final.md")
        return run_ai_cli("claude", prompt, self.work_dir, out_file)

    # -- Helpers ------------------------------------------------------------

    def _parse_assignments(self, raw_text: str) -> dict[str, str]:
        """Parse '[model] instruction' lines from Claude's response.

        Supports multiline instructions — captures everything until the
        next [model] bracket or end-of-string.
        Model names must be \\w+ (alphanumeric + underscore).
        """
        assignments = {}
        for match in re.finditer(
            r"\[(\w+)\]\s*(.+?)(?=\n\[|\Z)", raw_text, re.DOTALL
        ):
            model = match.group(1).lower()
            instruction = match.group(2).strip()
            if model in self.active_models:
                assignments[model] = instruction

        # Fallback: if parsing failed, assign the task description directly
        if not assignments:
            for model in self.active_models:
                assignments[model] = raw_text.strip()[:500]

        return assignments

    def _format_strengths(self) -> str:
        """Format model strengths for the assignment prompt."""
        lines = []
        for model in self.active_models:
            cfg = AI_MODELS[model]
            strengths = ", ".join(cfg["strengths"])
            lines.append(f"- {cfg['label']}: {strengths}")
        return "\n".join(lines)


class BatchDiscussion:
    """Multi-round AI-to-AI discussion with auto-convergence."""

    MAX_ROUNDS = 5

    def __init__(self, work_dir: str, active_models: list[str]):
        self.work_dir = work_dir
        self.active_models = active_models
        self.history: list[dict[str, str]] = []  # [{model: response}, ...]
        self.shared_ctx = SharedContext(work_dir)

    def run(self, topic: str) -> str:
        """Run discussion until convergence or max rounds."""
        for round_num in range(1, self.MAX_ROUNDS + 1):
            print(f"\n  [Round {round_num}/{self.MAX_ROUNDS}] ", end="")
            if round_num == 1:
                print("각 AI 의견 제시 중...")
            else:
                print("서로의 의견에 반응 중...")

            responses = self._discuss_round(topic, round_num)
            self.history.append(responses)

            # Check convergence after round 2
            if round_num >= 2:
                converged, reason = self._check_consensus(topic)
                if converged:
                    print(f"\n  [Consensus] Converged")
                    break
                else:
                    print(f"  [Consensus] Not yet - {reason}")

        rounds_done = len(self.history)
        return self._synthesize(topic, rounds_done)

    def _discuss_round(self, topic: str, round_num: int) -> dict[str, str]:
        """Run one round of discussion (all AIs in parallel)."""

        def _get_response(model: str) -> tuple[str, str]:
            cfg = AI_MODELS[model]
            if round_num == 1:
                prompt = BATCH_OPEN_PROMPT.format(
                    label=cfg["label"],
                    strengths=", ".join(cfg["strengths"]),
                    topic=topic,
                )
            else:
                prompt = BATCH_REPLY_PROMPT.format(
                    label=cfg["label"],
                    strengths=", ".join(cfg["strengths"]),
                    topic=topic,
                    history=self._format_history(),
                )
            out_file = str(
                Path(self.work_dir)
                / "shared"
                / f"batch_r{round_num}_{model}.md"
            )
            result = run_ai_cli(model, prompt, self.work_dir, out_file)
            return model, result

        responses: dict[str, str] = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            futures = {pool.submit(_get_response, m): m for m in self.active_models}
            for future in concurrent.futures.as_completed(futures):
                model, result = future.result()
                label = AI_MODELS[model]["label"]
                is_err = getattr(result, "is_error", False)
                if not is_err:
                    word_count = len(result.split())
                    print(f"    {label}: Done ({word_count} words)")
                    print(f"    ┌─ {label} ─")
                    for line in str(result).splitlines():
                        print(f"    │ {line}")
                    print(f"    └{'─' * 40}")
                else:
                    print(f"    {label}: {result[:40]}")
                responses[model] = result
                self.shared_ctx.add_response(
                    model, result, round_name=f"batch_r{round_num}",
                )

        return responses

    def _check_consensus(self, topic: str) -> tuple[bool, str]:
        """Ask Claude to judge whether discussion has converged."""
        prompt = BATCH_CONSENSUS_PROMPT.format(
            topic=topic,
            history=self._format_history(),
        )
        out_file = str(
            Path(self.work_dir) / "shared" / "batch_consensus.md"
        )
        result = run_ai_cli("claude", prompt, self.work_dir, out_file)

        if "CONVERGED" in result and "NOT_CONVERGED" not in result:
            return True, ""

        # Extract reason after "NOT_CONVERGED:"
        reason = result.replace("NOT_CONVERGED:", "").strip()
        if not reason:
            reason = "no details"
        return False, reason

    def _synthesize(self, topic: str, rounds: int) -> str:
        """Have Claude synthesize the full discussion."""
        prompt = BATCH_SYNTHESIS_PROMPT.format(
            topic=topic,
            rounds=rounds,
            history=self._format_history(),
        )
        out_file = str(
            Path(self.work_dir) / "shared" / "batch_synthesis.md"
        )
        return run_ai_cli("claude", prompt, self.work_dir, out_file)

    def _format_history(self) -> str:
        """Format all rounds into readable text."""
        parts = []
        for i, round_responses in enumerate(self.history, 1):
            for model, response in round_responses.items():
                label = AI_MODELS[model]["label"]
                parts.append(f"[Round {i}] {label}:\n{response}")
        return "\n\n".join(parts)
