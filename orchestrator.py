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

from rich.console import Console

from config import (
    AI_MODELS,
    ORCH_PLAN_PROMPT,
    ORCH_ASSIGN_PROMPT,
    ORCH_FINAL_PROMPT,
    BATCH_OPEN_PROMPT,
    BATCH_REPLY_PROMPT,
    BATCH_CONSENSUS_PROMPT,
    BATCH_SYNTHESIS_PROMPT,
    get_shared_dir,
)
import subprocess
from config import wsl_prefix

from ai_worker import (
    run_ai_cli,
    send_message_to_pane,
    send_and_capture_all,
    wait_for_all_panes_idle,
)
from tmux_manager import display_in_pane


def _write_wsl_file(path: str, content: str) -> None:
    """Write content to a WSL file via stdin."""
    subprocess.run(
        wsl_prefix() + ["bash", "-c", f"cat > {path}"],
        input=content.encode("utf-8"),
        capture_output=True, timeout=10,
    )
from conversation import SharedContext, EventStream

console = Console()


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
        self.events = EventStream(work_dir)

    def run(self, task: str) -> str:
        """Full orchestration pipeline: plan -> assign -> execute."""
        self.events.log("status", detail=f"Task started: {task[:80]}", phase="start")

        # Phase 1: Planning
        plans = self._plan(task)
        if not plans:
            self.events.log("error", detail="No plans received", phase="plan")
            return "[Error] No plans received from any AI."

        # Phase 2: Role assignment + user confirmation
        assignments = self._assign(task, plans)
        if assignments is None:
            self.events.log("status", detail="User declined", phase="assign")
            return "[Cancelled] User declined the task plan."

        # Create todo.md checklist
        self._create_todo(task, assignments)

        # Phase 3: Execute + synthesize
        return self._execute(task, assignments)

    # -- Helpers ------------------------------------------------------------

    def _notify(self, phase: str, detail: str, style: str = "blue") -> None:
        """Print structured progress notification."""
        console.print(f"  [{style}][{phase}][/{style}] {detail}")
        self.events.log("status", detail=detail, phase=phase)

    def _create_todo(self, task: str, assignments: dict[str, str]) -> Path:
        """Create shared todo.md checklist from assignments."""
        todo_path = get_shared_dir(self.work_dir) / "todo.md"
        lines = [f"# Task: {task}\n"]
        for model, instruction in assignments.items():
            label = AI_MODELS[model]["label"]
            short = instruction[:80]
            lines.append(f"- [ ] {label}: {short}")
        lines.append("")
        todo_path.write_text("\n".join(lines), encoding="utf-8")
        self._notify("Todo", "Checklist created: todo.md", style="green")
        return todo_path

    # -- Phase 1: Planning --------------------------------------------------

    def _plan(self, task: str) -> dict[str, str]:
        """Ask each AI to draft a plan (batch, parallel)."""
        self._notify("Phase 1/3", "Planning - each AI drafting a plan...")

        def _get_plan(model: str) -> tuple[str, str]:
            prompt = ORCH_PLAN_PROMPT.format(
                name=model,
                task=task,
            )
            out_file = str(get_shared_dir(self.work_dir) / f"orch_plan_{model}.md")
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
        self._notify("Phase 2/3", "Assigning Roles...")

        # Build combined plans text
        all_plans = "\n\n".join(
            f"=== {AI_MODELS[m]['label']} ===\n{text}"
            for m, text in plans.items()
        )

        prompt = ORCH_ASSIGN_PROMPT.format(
            task=task,
            all_plans=all_plans,
            active_models=", ".join(self.active_models),
        )
        out_file = str(get_shared_dir(self.work_dir) / "orch_assign.md")
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
        self._notify("Phase 3/3", "Executing...")

        # Send instructions to each AI's interactive pane (with todo.md reference)
        sent_models = []
        for model, instruction in assignments.items():
            pane = self.pane_map.get(model)
            if pane:
                full_instruction = (
                    f"{instruction}\n\n"
                    "Refer to todo.md in the shared directory for the full task checklist."
                )
                send_message_to_pane(pane, full_instruction)
                label = AI_MODELS[model]["label"]
                self._notify("Execute", f"-> {label}: Delegated", style="cyan")
                self.events.log("action", model=model, detail="Task delegated", phase="execute")
                sent_models.append(model)

        if not sent_models:
            return "[Error] No AI panes available for execution."

        # Wait for all panes to finish
        print("    Waiting for AI responses...")
        pane_targets = {m: self.pane_map[m] for m in sent_models if m in self.pane_map}
        responses = wait_for_all_panes_idle(pane_targets, timeout=1800)

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
        out_file = str(get_shared_dir(self.work_dir) / "orch_final.md")
        return run_ai_cli("claude", prompt, self.work_dir, out_file)

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
            self._notify("Assign", "Warning: could not parse role assignments, using fallback", style="yellow")
            for model in self.active_models:
                assignments[model] = raw_text.strip()[:500]

        return assignments

class BatchDiscussion:
    """Multi-round AI-to-AI discussion with auto-convergence."""

    MAX_ROUNDS = 5

    def __init__(self, work_dir: str, active_models: list[str], pane_map: dict[str, str] | None = None):
        self.work_dir = work_dir
        self.active_models = active_models
        self.pane_map = pane_map or {}
        self.history: list[dict[str, str]] = []  # [{model: response}, ...]
        self.shared_ctx = SharedContext(work_dir)
        self.events = EventStream(work_dir)

    def run(self, topic: str) -> str:
        """Run discussion until convergence or max rounds."""
        self.events.log("status", detail=f"Batch started: {topic[:80]}", phase="batch_start")
        for round_num in range(1, self.MAX_ROUNDS + 1):
            if round_num == 1:
                console.print(f"\n  [blue][Round {round_num}/{self.MAX_ROUNDS}][/blue] 각 AI 의견 제시 중...")
            else:
                console.print(f"\n  [blue][Round {round_num}/{self.MAX_ROUNDS}][/blue] 서로의 의견에 반응 중...")

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
            if round_num == 1:
                prompt = BATCH_OPEN_PROMPT.format(
                    name=model,
                    topic=topic,
                )
            else:
                prompt = BATCH_REPLY_PROMPT.format(
                    name=model,
                    topic=topic,
                    history=self._format_history(),
                )
            out_file = str(
                get_shared_dir(self.work_dir)
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
                # Show progress in the AI's tmux pane
                if self.pane_map and model in self.pane_map:
                    chars = len(result)
                    display_in_pane(
                        self.pane_map[model],
                        f"[Batch R{round_num}] Done ({chars} chars)",
                    )

        return responses

    def _check_consensus(self, topic: str) -> tuple[bool, str]:
        """Ask Claude to judge whether discussion has converged."""
        prompt = BATCH_CONSENSUS_PROMPT.format(
            topic=topic,
            history=self._format_history(),
        )
        out_file = str(
            get_shared_dir(self.work_dir) / "batch_consensus.md"
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
            get_shared_dir(self.work_dir) / "batch_synthesis.md"
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


class LiveBatchDiscussion:
    """Multi-round AI-to-AI discussion using live tmux sessions.

    Unlike BatchDiscussion which spawns one-shot CLI processes,
    this class sends messages to the already-running interactive
    sessions, preserving each AI's conversation context.
    """

    MAX_ROUNDS = 5

    def __init__(
        self,
        work_dir: str,
        active_models: list[str],
        pane_map: dict[str, str],
    ):
        self.work_dir = work_dir
        self.active_models = active_models
        self.pane_map = pane_map
        self.history: list[dict[str, str]] = []
        self.shared_ctx = SharedContext(work_dir)
        self.events = EventStream(work_dir)

    def run(self, topic: str) -> str:
        """Run live discussion until convergence or max rounds."""
        self.events.log("status", detail=f"Live batch started: {topic[:80]}", phase="live_batch_start")

        for round_num in range(1, self.MAX_ROUNDS + 1):
            if round_num == 1:
                console.print(f"\n  [blue][Live Round {round_num}/{self.MAX_ROUNDS}][/blue] 각 AI에게 의견 요청 중...")
            else:
                console.print(f"\n  [blue][Live Round {round_num}/{self.MAX_ROUNDS}][/blue] 다른 AI 의견 전달 후 반응 대기 중...")

            responses = self._discuss_round_live(topic, round_num)
            self.history.append(responses)

            # Show responses in input pane
            for model, response in responses.items():
                label = AI_MODELS[model]["label"]
                word_count = len(response.split())
                console.print(f"    {label}: {word_count} words")
                console.print(f"    ┌─ {label} ─")
                for line in response.splitlines()[:15]:
                    console.print(f"    │ {line}")
                if len(response.splitlines()) > 15:
                    console.print(f"    │ ... ({len(response.splitlines()) - 15} more lines)")
                console.print(f"    └{'─' * 40}")

            # Check convergence after round 2
            if round_num >= 2:
                converged, reason = self._check_consensus(topic)
                if converged:
                    console.print(f"\n  [green][Consensus][/green] Converged")
                    break
                else:
                    console.print(f"  [yellow][Consensus][/yellow] Not yet - {reason}")

        rounds_done = len(self.history)
        return self._synthesize(topic, rounds_done)

    def _discuss_round_live(self, topic: str, round_num: int) -> dict[str, str]:
        """Run one round using live sessions (send_and_capture_all).

        Long content (topic, other AIs' responses) is written to temp files
        so AIs read it via their file tools — no TUI input length issues.
        The AI's live session context is fully maintained.
        """
        pane_targets = {
            m: self.pane_map[m]
            for m in self.active_models
            if m in self.pane_map
        }

        messages: dict[str, str] = {}
        if round_num == 1:
            # Write full topic to file — AIs read it directly
            _write_wsl_file("/tmp/_team_topic.txt", topic)
            for model in pane_targets:
                messages[model] = (
                    f"[Team Discussion - Round 1] "
                    f"Read the file /tmp/_team_topic.txt which contains a paper section for review. "
                    f"As an expert reviewer, share your perspective on the content. "
                    f"Be specific and opinionated. Keep it under 200 words. "
                    f"Respond in the same language as the content in the file."
                )
        else:
            # Round 2+: write each AI's full response to a file per recipient
            prev = self.history[-1]
            topic_reminder = topic[:300] + "..." if len(topic) > 300 else topic
            for model in pane_targets:
                context = "\n\n".join(
                    f"=== {AI_MODELS[m]['label']} ===\n{resp}"
                    for m, resp in prev.items()
                    if m != model
                )
                context_file = f"/tmp/_team_round{round_num}_for_{model}.txt"
                _write_wsl_file(context_file, context)
                messages[model] = (
                    f"[Team Discussion - Round {round_num}] "
                    f"Topic reminder: {topic_reminder} . "
                    f"Read /tmp/_team_round{round_num}_for_{model}.txt which contains "
                    f"other AIs' full responses from the previous round. "
                    f"Analyze their arguments, state where you agree or disagree, "
                    f"and provide your updated response. Keep it under 250 words. "
                    f"Respond in the same language as the topic."
                )

        responses = send_and_capture_all(pane_targets, messages)

        # Store in shared context
        for model, response in responses.items():
            self.shared_ctx.add_response(
                model, response, round_name=f"live_batch_r{round_num}",
            )

        return responses

    def _check_consensus(self, topic: str) -> tuple[bool, str]:
        """Ask Claude (batch mode) to judge convergence — meta task stays batch."""
        prompt = BATCH_CONSENSUS_PROMPT.format(
            topic=topic,
            history=self._format_history(),
        )
        out_file = str(get_shared_dir(self.work_dir) / "live_batch_consensus.md")
        result = run_ai_cli("claude", prompt, self.work_dir, out_file)

        if "CONVERGED" in result and "NOT_CONVERGED" not in result:
            return True, ""
        reason = result.replace("NOT_CONVERGED:", "").strip()
        return False, reason if reason else "no details"

    def _synthesize(self, topic: str, rounds: int) -> str:
        """Synthesize via Claude batch mode — meta task stays batch."""
        prompt = BATCH_SYNTHESIS_PROMPT.format(
            topic=topic,
            rounds=rounds,
            history=self._format_history(),
        )
        out_file = str(get_shared_dir(self.work_dir) / "live_batch_synthesis.md")
        return run_ai_cli("claude", prompt, self.work_dir, out_file)

    def _format_history(self) -> str:
        """Format all rounds into readable text."""
        parts = []
        for i, round_responses in enumerate(self.history, 1):
            for model, response in round_responses.items():
                label = AI_MODELS[model]["label"]
                parts.append(f"[Round {i}] {label}:\n{response}")
        return "\n\n".join(parts)


class LiveTaskOrchestrator:
    """Task orchestrator using live tmux sessions for planning phase.

    Phase 1 (planning): Live sessions — each AI plans in its own session
    Phase 2 (assignment): Batch mode — Claude assigns roles (meta task)
    Phase 3 (execution): Live sessions — already was live, now with better capture
    """

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
        self.events = EventStream(work_dir)

    def run(self, task: str) -> str:
        """Full orchestration: live plan -> batch assign -> live execute."""
        self.events.log("status", detail=f"Live task started: {task[:80]}", phase="start")

        # Phase 1: Live planning
        plans = self._plan_live(task)
        if not plans:
            return "[Error] No plans received from any AI."

        # Phase 2: Role assignment (batch — meta task)
        assignments = self._assign(task, plans)
        if assignments is None:
            return "[Cancelled] User declined the task plan."

        self._create_todo(task, assignments)

        # Phase 3: Live execution + synthesis
        return self._execute_live(task, assignments)

    def _notify(self, phase: str, detail: str, style: str = "blue") -> None:
        console.print(f"  [{style}][{phase}][/{style}] {detail}")
        self.events.log("status", detail=detail, phase=phase)

    def _create_todo(self, task: str, assignments: dict[str, str]) -> None:
        todo_path = get_shared_dir(self.work_dir) / "todo.md"
        lines = [f"# Task: {task}\n"]
        for model, instruction in assignments.items():
            label = AI_MODELS[model]["label"]
            lines.append(f"- [ ] {label}: {instruction[:80]}")
        lines.append("")
        todo_path.write_text("\n".join(lines), encoding="utf-8")
        self._notify("Todo", "Checklist created: todo.md", style="green")

    def _plan_live(self, task: str) -> dict[str, str]:
        """Phase 1: Ask each AI to plan in their live session."""
        self._notify("Phase 1/3", "Planning (live sessions)...")

        pane_targets = {
            m: self.pane_map[m]
            for m in self.active_models
            if m in self.pane_map
        }
        messages = {
            m: (
                f"[Task Planning] Task: {task} . "
                f"Write a brief plan for how YOU would contribute to this task. "
                f"Be specific and actionable. Keep it under 200 words. "
                f"Respond in the same language as the task."
            )
            for m in pane_targets
        }

        plans = send_and_capture_all(pane_targets, messages)

        for model, plan in plans.items():
            label = AI_MODELS[model]["label"]
            console.print(f"    {label}: Plan received ({len(plan.split())} words)")
            self.shared_ctx.add_response(model, plan, round_name="live_plan")

        return plans

    def _assign(self, task: str, plans: dict[str, str]) -> dict[str, str] | None:
        """Phase 2: Claude assigns roles (batch mode — meta task)."""
        self._notify("Phase 2/3", "Assigning Roles (batch)...")

        all_plans = "\n\n".join(
            f"=== {AI_MODELS[m]['label']} ===\n{text}"
            for m, text in plans.items()
        )
        prompt = ORCH_ASSIGN_PROMPT.format(
            task=task,
            all_plans=all_plans,
            active_models=", ".join(self.active_models),
        )
        out_file = str(get_shared_dir(self.work_dir) / "live_orch_assign.md")
        raw = run_ai_cli("claude", prompt, self.work_dir, out_file)

        assignments = self._parse_assignments(raw)

        # Show plan to user
        console.print("  ┌──────────────────────────────────────────┐")
        console.print("  │  Task Plan                               │")
        for model, instruction in assignments.items():
            label = AI_MODELS.get(model, {}).get("label", model)
            short = instruction[:50] + "..." if len(instruction) > 50 else instruction
            console.print(f"  │  {label}: {short}")
        console.print("  └──────────────────────────────────────────┘")

        try:
            confirm = input("  Proceed? (y/n): ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            confirm = "n"

        return assignments if confirm == "y" else None

    def _execute_live(self, task: str, assignments: dict[str, str]) -> str:
        """Phase 3: Execute in live sessions, capture responses, synthesize."""
        self._notify("Phase 3/3", "Executing (live sessions)...")

        pane_targets = {}
        messages = {}
        for model, instruction in assignments.items():
            pane = self.pane_map.get(model)
            if pane:
                pane_targets[model] = pane
                messages[model] = (
                    f"{instruction} "
                    f"Refer to todo.md in the shared directory for the full task checklist."
                )
                self._notify("Execute", f"-> {AI_MODELS[model]['label']}: Delegated", style="cyan")

        if not pane_targets:
            return "[Error] No AI panes available for execution."

        console.print("    Waiting for AI responses (live)...")
        responses = send_and_capture_all(pane_targets, messages, timeout=1800)

        # Build results for synthesis
        all_results = "\n\n".join(
            f"=== {AI_MODELS[m]['label']} ===\n{responses.get(m, '(no response)')}"
            for m in pane_targets
        )
        assign_text = "\n".join(
            f"- {AI_MODELS[m]['label']}: {assignments[m]}"
            for m in pane_targets
        )

        # Synthesize with Claude (batch — meta task)
        console.print("    Synthesizing final result...")
        prompt = ORCH_FINAL_PROMPT.format(
            task=task,
            assignments=assign_text,
            all_results=all_results,
        )
        out_file = str(get_shared_dir(self.work_dir) / "live_orch_final.md")
        return run_ai_cli("claude", prompt, self.work_dir, out_file)

    def _parse_assignments(self, raw_text: str) -> dict[str, str]:
        assignments = {}
        for match in re.finditer(
            r"\[(\w+)\]\s*(.+?)(?=\n\[|\Z)", raw_text, re.DOTALL
        ):
            model = match.group(1).lower()
            instruction = match.group(2).strip()
            if model in self.active_models:
                assignments[model] = instruction

        if not assignments:
            self._notify("Assign", "Warning: could not parse role assignments, using fallback", style="yellow")
            for model in self.active_models:
                assignments[model] = raw_text.strip()[:500]

        return assignments
