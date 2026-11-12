"""The Worker agent: writes and edits code for the Boss's tasks."""

from __future__ import annotations

from app.agents.base import Agent, AgentContext, AgentError
from app.agents.boss import READ_ONLY_TOOLS, os_description
from app.agents.schemas import FileChanges, Task, Verdict, WorkerReport
from app.agents.system_prompts import (
    worker_fix,
    worker_manual_fix,
    worker_new_task,
    worker_system_prompt,
)
from app.db.models import ApiProfile
from app.llm.client import ModelConfig
from app.tools import WORKER_TOOLS


class Worker(Agent):
    def __init__(self, profile: ApiProfile | ModelConfig, context: AgentContext, os_name: str | None = None) -> None:
        self.os_name = os_name or os_description()
        super().__init__("worker", profile, self._build_system_prompt, WORKER_TOOLS, context)

    def _build_system_prompt(self) -> str:
        return worker_system_prompt(project_path=str(self.context.project_root), os_name=self.os_name)

    async def _report(self, user_message: str) -> WorkerReport:
        result = await self.run_turn(user_message, kind="report", terminal_tools={"finish_task"})
        if result.args is None:
            raise AgentError("The Worker did not call finish_task.")
        return WorkerReport.model_validate(result.args)

    async def do_task(self, task: Task) -> WorkerReport:
        """First round of a prompt: start a fresh conversation and do the task."""
        self.reset()
        return await self._report(worker_new_task(task))

    async def fix(self, round: int, verdict: Verdict) -> WorkerReport:
        """Later rounds: fix the bugs from the Boss's rejected verdict (same conversation)."""
        self.context.round = round
        return await self._report(worker_fix(round, self.context.max_rounds, verdict))

    async def absorb_manual_fix(self, changes: FileChanges, extra: str | None = None) -> str:
        """The user fixed things by hand: read the changes, change nothing."""
        message = worker_manual_fix(changes) + (f"\n{extra}" if extra else "")
        result = await self.run_turn(
            message, kind="system", terminal_tools=set(),
            allow_no_tool_end=True, allowed_tools=READ_ONLY_TOOLS,
        )
        return result.text
