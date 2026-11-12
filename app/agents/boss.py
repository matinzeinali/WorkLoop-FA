"""The Boss agent: plans tasks, tests the project and gives verdicts."""

from __future__ import annotations

import platform
from typing import Protocol

from app.agents.base import Agent, AgentContext, AgentError
from app.agents.schemas import FileChanges, Task, Verdict, WorkerReport
from app.agents.system_prompts import (
    boss_failed_summary,
    boss_manual_fix,
    boss_new_prompt,
    boss_system_prompt,
    boss_test,
)
from app.core.events import emit_verdict
from app.db import repository as repo
from app.db.database import SessionLocal
from app.db.models import ApiProfile
from app.llm.client import ModelConfig
from app.tools import BOSS_TOOLS
from app.tools.command_tools import pick_shell

READ_ONLY_TOOLS = frozenset({"list_files", "read_file"})


class PromptLike(Protocol):
    id: int
    content: str


def os_description() -> str:
    return f"{platform.system()} {platform.release()}".strip() or "unknown"


class Boss(Agent):
    def __init__(
        self,
        profile: ApiProfile | ModelConfig,
        context: AgentContext,
        os_name: str | None = None,
        shell_name: str | None = None,
    ) -> None:
        self.os_name = os_name or os_description()
        if shell_name is None:
            try:
                shell_name = pick_shell()[0]
            except Exception:  # noqa: BLE001 - only used for the prompt text
                shell_name = "unknown"
        self.shell_name = shell_name
        super().__init__("boss", profile, self._build_system_prompt, BOSS_TOOLS, context)

    def _build_system_prompt(self) -> str:
        ctx = self.context
        return boss_system_prompt(
            project_path=str(ctx.project_root), os_name=self.os_name, shell_name=self.shell_name,
            round=ctx.round, max_rounds=ctx.max_rounds,
        )

    async def plan(self, prompt: PromptLike) -> Task:
        """Start a new prompt: read the project and assign a task."""
        self.reset()
        self.context.prompt_id = prompt.id
        result = await self.run_turn(
            boss_new_prompt(prompt.id, prompt.content), kind="task", terminal_tools={"assign_task"}
        )
        if result.args is None:
            raise AgentError("The Boss did not assign a task.")
        return Task.model_validate(result.args)

    async def test(self, round: int, worker_report: WorkerReport) -> Verdict:
        """Test the whole project after the Worker's round and give a verdict."""
        self.context.round = round
        result = await self.run_turn(
            boss_test(round, self.context.max_rounds, worker_report), kind="verdict",
            terminal_tools={"submit_verdict"},
        )
        if result.args is None:
            raise AgentError("The Boss did not submit a verdict.")
        verdict = Verdict.model_validate(result.args)
        bugs = [b.model_dump() for b in verdict.bugs]
        with SessionLocal() as db:
            repo.save_verdict(
                db, self.context.run_id, self.context.prompt_id, round, verdict.status, verdict.summary,
                bugs, list(verdict.fix_instructions),
            )
        await emit_verdict(
            self.context.prompt_id, round, verdict.status, verdict.summary, bugs, list(verdict.fix_instructions)
        )
        return verdict

    async def failure_summary(self) -> str:
        """After the last round failed: a short summary for the user, no tools."""
        result = await self.run_turn(
            boss_failed_summary(self.context.max_rounds), kind="system", terminal_tools=set(),
            allow_no_tool_end=True, allowed_tools=set(),
        )
        return result.text

    async def absorb_manual_fix(self, changes: FileChanges, extra: str | None = None) -> str:
        """The user fixed things by hand: read the changes, no task, no verdict.

        extra: added below the template (e.g. "No file changes were detected." or the user's note).
        """
        message = boss_manual_fix(changes) + (f"\n{extra}" if extra else "")
        result = await self.run_turn(
            message, kind="system", terminal_tools=set(),
            allow_no_tool_end=True, allowed_tools=READ_ONLY_TOOLS,
        )
        return result.text
