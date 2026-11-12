"""The work loop: runs the prompt queue with the Boss and the Worker (single global instance).

Per prompt:
    round 1:      boss.plan -> worker.do_task -> boss.test
    round N > 1:  worker.fix -> boss.test
    approved -> prompt done, next prompt.
    rejected in the last round -> prompt failed, Boss writes a summary, the run pauses in
    needs_user (the same run continues later with resume()).

After a failure the user picks one of:
    fix_prompt(text)  a new prompt right after the failed one; the run continues with it.
    manual_fix()      the user edited files by hand; both agents read the changes
                      (diff against the snapshot taken at the failure), then the run continues.

The loop runs as one background asyncio.Task. stop() cancels it and cleans up within
STOP_TIMEOUT seconds even if a provider or a process hangs. Errors inside the loop end
the run with an error event; they never reach the server.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from app.agents.base import AgentContext, AgentError
from app.agents.boss import Boss
from app.agents.schemas import FileChanges, Verdict
from app.agents.worker import Worker
from app.config import get_settings
from app.core.errors import ErrorCode
from app.core.events import (
    emit_error,
    emit_manual_fix_applied,
    emit_needs_user,
    emit_prompt_done,
    emit_prompt_status,
    emit_queue_empty,
    emit_round_started,
)
from app.core.permissions import PermissionManager, get_permission_manager
from app.core.queue import RunStateManager, get_run_state_manager
from app.core.snapshot import diff_snapshot, discard_snapshot, take_snapshot
from app.db import repository as repo
from app.db.database import SessionLocal
from app.db.models import ManualFix, Prompt, PromptStatus, RunStatus
from app.llm.client import ModelConfig, ProviderError
from app.tools.command_tools import kill_all_commands

logger = logging.getLogger(__name__)

STOP_TIMEOUT = 10.0  # stop() always returns within this many seconds
KILL_TIMEOUT = 3.0  # time allowed for killing running commands

NeedsUserHook = Callable[[int], Awaitable[None]]
NO_CHANGES_NOTE = "No file changes were detected."


class NotNeedsUserError(Exception):
    """fix_prompt / manual_fix called while the run is not paused in needs_user."""


@dataclass
class RunConfig:
    run_id: int
    boss_profile_id: int
    worker_profile_id: int
    boss_model: ModelConfig
    worker_model: ModelConfig
    project_root: Path


class Orchestrator:
    def __init__(
        self, state: RunStateManager | None = None, permissions: PermissionManager | None = None
    ) -> None:
        self.state = state or get_run_state_manager()
        self.permissions = permissions or get_permission_manager()
        self.permissions.set_waiting_callback(self.state.on_permission_waiting)
        self.start_lock = asyncio.Lock()  # held by POST /run/start for its whole check sequence
        self._stop_lock = asyncio.Lock()
        self._close_lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None
        self.config: RunConfig | None = None
        # Agents of the prompt being processed.
        self.boss: Boss | None = None
        self.worker: Worker | None = None
        # The failed prompt waiting for the user (needs_user); step 8 uses these.
        self.failed_prompt_id: int | None = None
        self.failed_boss: Boss | None = None
        self.failed_worker: Worker | None = None
        self.last_verdict: Verdict | None = None
        self.snapshot_id: int | None = None  # taken when the run paused in needs_user
        self.on_needs_user: NeedsUserHook = self._take_snapshot
        self._phase = ""

    # --- public API ----------------------------------------------------------------

    @property
    def task(self) -> asyncio.Task[None] | None:
        return self._task

    async def start(
        self,
        boss_profile_id: int,
        worker_profile_id: int,
        boss_model: ModelConfig,
        worker_model: ModelConfig,
        project_root: Path,
        boss_name: str | None = None,
        worker_name: str | None = None,
    ) -> int:
        """Create a run and start the loop. The caller has done the checks (see POST /run/start)."""
        with SessionLocal() as db:
            run = repo.create_run(
                db, boss_profile_id, worker_profile_id, str(project_root), boss_name, worker_name
            )
            run_id = run.id
        self.config = RunConfig(run_id, boss_profile_id, worker_profile_id, boss_model, worker_model, project_root)
        self._clear_failed()
        await self.state.set_state("running", run_id=run_id, prompt_id=None, round=None)
        self._task = asyncio.create_task(self._guarded(self._queue_loop), name=f"workloop-run-{run_id}")
        logger.info("Run %s started (project %s)", run_id, project_root)
        return run_id

    async def resume(self) -> bool:
        """Continue a run paused in needs_user with the next pending prompt. False if not paused."""
        async with self._stop_lock:
            return await self._resume_locked()

    async def _resume_locked(self) -> bool:
        if self.state.state != "needs_user" or self.config is None:
            return False
        await self._continue_run(self._queue_loop)
        return True

    async def _continue_run(self, body: Callable[[], Awaitable[None]], prompt_id: int | None = None) -> None:
        cfg = self._require_config()
        with SessionLocal() as db:
            repo.set_run_status(db, cfg.run_id, RunStatus.RUNNING)
        await self.state.set_state("running", prompt_id=prompt_id, round=None)
        self._task = asyncio.create_task(self._guarded(body), name=f"workloop-run-{cfg.run_id}")
        logger.info("Run %s continues", cfg.run_id)

    async def fix_prompt(self, text: str) -> Prompt:
        """Option 1 after a failure: queue a new prompt right after the failed one and continue."""
        async with self._stop_lock:
            if self.state.state != "needs_user" or self.config is None or self.failed_prompt_id is None:
                raise NotNeedsUserError
            with SessionLocal() as db:
                prompt = repo.insert_prompt_after(db, self.failed_prompt_id, text)
            await emit_prompt_status(prompt.id, "pending", 0)
            await self._discard_snapshot()
            self._clear_failed()
            await self._resume_locked()
            return prompt

    async def manual_fix(self, note: str | None = None) -> FileChanges:
        """Option 2 after a failure: the user fixed it by hand. Agents read the changes, then continue."""
        async with self._stop_lock:
            if self.state.state != "needs_user" or self.config is None or self.failed_prompt_id is None:
                raise NotNeedsUserError
            cfg = self.config
            pid = self.failed_prompt_id
            if self.snapshot_id is not None:
                changes = await diff_snapshot(self.snapshot_id, cfg.project_root)
            else:
                logger.warning("Run %s: no snapshot for prompt %s; reporting no changes", cfg.run_id, pid)
                changes = FileChanges()
            with SessionLocal() as db:
                prompt = repo.get_prompt(db, pid)
                rounds = prompt.rounds_used if prompt else 0
                if prompt is not None:
                    repo.set_prompt_status(db, prompt, PromptStatus.MANUAL_FIX)
                db.add(ManualFix(
                    run_id=cfg.run_id, prompt_id=pid, added=changes.added, removed=changes.removed,
                    modified=changes.modified, note=note,
                ))
                db.commit()
            await emit_prompt_status(pid, "manual_fix", rounds)
            await emit_manual_fix_applied(pid, changes.added, changes.removed, changes.modified)
            await self._discard_snapshot()
            boss, worker = self.failed_boss, self.failed_worker
            no_changes = not (changes.added or changes.removed or changes.modified)
            extra_lines = [line for line in (
                NO_CHANGES_NOTE if no_changes else None,
                f"User note: {note}" if note else None,
            ) if line]
            extra = "\n".join(extra_lines) or None

            async def absorb_then_continue() -> None:
                self._phase = "boss.absorb_manual_fix"
                if boss is not None:
                    await boss.absorb_manual_fix(changes, extra)
                self._phase = "worker.absorb_manual_fix"
                if worker is not None:
                    await worker.absorb_manual_fix(changes, extra)
                self._clear_failed()
                await self._queue_loop()

            await self._continue_run(absorb_then_continue, prompt_id=pid)
            return changes

    async def stop(self) -> dict[str, Any]:
        """Stop the run: cancel the loop, kill commands, cancel permissions, close the run.

        Safe to call twice at the same time; returns within STOP_TIMEOUT seconds.
        """
        async with self._stop_lock:
            if not self.state.is_active():
                return dict(self.state.snapshot())
            loop = asyncio.get_running_loop()
            deadline = loop.time() + STOP_TIMEOUT
            run_id = self.state.run_id
            logger.info("Stopping run %s", run_id)
            await self.state.set_state("stopping")

            try:
                task = self._task
                if task is not None and not task.done():
                    task.cancel()
                # Unblock whatever the loop is waiting on, so it can unwind quickly. Each step is
                # independent: one failing must not leave the run stuck in "stopping".
                await self._safely(self._kill_commands(), "kill commands")
                await self._safely(self.permissions.cancel_all(), "cancel permissions")
                if task is not None and not task.done():
                    remaining = max(0.5, deadline - loop.time() - 1.5)
                    done, _ = await asyncio.wait({task}, timeout=remaining)
                    if not done:
                        logger.warning("Run %s: loop did not stop in time; forcing cleanup", run_id)
                await self._safely(self._revert_running_prompts(), "revert running prompts")
                await self._safely(self._discard_snapshot(), "discard snapshot")
            finally:
                await self._close_run(RunStatus.STOPPED)
                self._clear_failed()
            logger.info("Run %s stopped", run_id)
            return dict(self.state.snapshot())

    @staticmethod
    async def _safely(step: Awaitable[Any], what: str) -> None:
        """Run a cleanup step; log and continue if it fails."""
        try:
            await step
        except Exception:
            logger.exception("Cleanup step failed: %s", what)

    # --- the loop ------------------------------------------------------------------

    async def _queue_loop(self) -> None:
        cfg = self._require_config()
        while True:
            if self.config is not cfg:
                return  # this run was closed (stop / error); never claim prompts for another run
            with SessionLocal() as db:
                prompt = repo.claim_next_pending_prompt(db, cfg.run_id)
            if prompt is None:
                await emit_queue_empty()
                await self._close_run(RunStatus.COMPLETED)
                return
            outcome = await self._process_prompt(prompt)
            if outcome == "needs_user":
                return

    async def _guarded(self, body: Callable[[], Awaitable[None]]) -> None:
        """Run a loop body; any error ends the run cleanly instead of reaching the server.

        The task remembers its run id: if it only notices a cancel after stop() already closed
        the run (and maybe a new run started), it must not touch the new run.
        """
        run_id = self.config.run_id if self.config else None
        try:
            await body()
        except asyncio.CancelledError:
            raise  # stop() does the cleanup
        except Exception as exc:
            if isinstance(exc, ProviderError):
                code, message, details = ErrorCode.PROVIDER_ERROR, exc.message, {"kind": exc.kind}
            elif isinstance(exc, AgentError):
                code, message, details = ErrorCode.INTERNAL_ERROR, str(exc), {}
            else:
                logger.exception("Unexpected error in the work loop")
                code, message, details = (
                    ErrorCode.INTERNAL_ERROR, f"Unexpected error: {type(exc).__name__}: {exc}", {}
                )
            if self.config is None or self.config.run_id != run_id:
                logger.warning("Run %s: error after the run was closed: %s", run_id, message)
                return
            try:
                await self._fail(code, message, details)
            except Exception:
                logger.exception("Error handling failed; forcing the run closed")
                await self._close_run(RunStatus.ERROR, message)

    async def _process_prompt(self, prompt: Prompt) -> Literal["done", "needs_user"]:
        cfg = self._require_config()
        max_rounds = get_settings().max_rounds
        pid = prompt.id
        logger.info("Run %s: prompt %s started", cfg.run_id, pid)
        await emit_prompt_status(pid, "running", 0)
        await self.state.set_state(prompt_id=pid, round=None)

        boss = Boss(cfg.boss_model, self._context(pid, max_rounds))
        worker = Worker(cfg.worker_model, self._context(pid, max_rounds))
        self.boss, self.worker = boss, worker

        self._phase = "boss.plan"
        task = await boss.plan(prompt)
        await self._start_round(pid, 1, max_rounds)
        self._phase = "worker.do_task"
        report = await worker.do_task(task)

        verdict: Verdict | None = None
        for round_no in range(1, max_rounds + 1):
            if round_no > 1:
                assert verdict is not None
                await self._start_round(pid, round_no, max_rounds)
                self._phase = "worker.fix"
                report = await worker.fix(round_no, verdict)
            self._phase = "boss.test"
            verdict = await boss.test(round_no, report)
            if verdict.status == "approved":
                await self._finish_prompt(pid, PromptStatus.DONE, round_no)
                await emit_prompt_done(pid, round_no)
                logger.info("Run %s: prompt %s done in %d round(s)", cfg.run_id, pid, round_no)
                return "done"

        # Rejected in the last round.
        assert verdict is not None
        await self._finish_prompt(pid, PromptStatus.FAILED, max_rounds)
        self.failed_prompt_id, self.failed_boss, self.failed_worker = pid, boss, worker
        self.last_verdict = verdict
        self._phase = "boss.failure_summary"
        try:
            summary = await boss.failure_summary()
        except (ProviderError, AgentError) as exc:
            summary = f"The task failed after {max_rounds} rounds. (The Boss could not write a summary: {exc})"
        # The hook (snapshot) runs before needs_user is announced: once the user sees the
        # choice they may click "manual fix" at once, and the diff needs the snapshot.
        self._phase = "snapshot"
        try:
            await self.on_needs_user(pid)
        except Exception:
            logger.exception("on_needs_user hook failed")
        with SessionLocal() as db:
            repo.set_run_status(db, cfg.run_id, RunStatus.NEEDS_USER)
        await self.state.set_state("needs_user", prompt_id=pid, round=max_rounds)
        await emit_needs_user(pid, max_rounds, summary, [b.model_dump() for b in verdict.bugs])
        logger.info("Run %s: prompt %s failed after %d rounds; waiting for the user", cfg.run_id, pid, max_rounds)
        return "needs_user"

    # --- helpers -------------------------------------------------------------------

    def _require_config(self) -> RunConfig:
        if self.config is None:
            raise RuntimeError("No run configured")
        return self.config

    def _context(self, prompt_id: int, max_rounds: int) -> AgentContext:
        cfg = self._require_config()
        return AgentContext(
            project_root=cfg.project_root, prompt_id=prompt_id, run_id=cfg.run_id, round=1,
            max_rounds=max_rounds, permissions=self.permissions,
        )

    async def _start_round(self, prompt_id: int, round_no: int, max_rounds: int) -> None:
        with SessionLocal() as db:
            repo.set_prompt_rounds(db, prompt_id, round_no)
        await self.state.set_state(prompt_id=prompt_id, round=round_no)
        await emit_round_started(prompt_id, round_no, max_rounds)

    async def _finish_prompt(self, prompt_id: int, status: PromptStatus, rounds_used: int) -> None:
        cfg = self._require_config()
        with SessionLocal() as db:
            prompt = repo.get_prompt(db, prompt_id)
            if prompt is not None:
                repo.set_prompt_status(db, prompt, status, rounds_used=rounds_used)
            repo.add_run_result(
                db, cfg.run_id, done=int(status == PromptStatus.DONE), failed=int(status == PromptStatus.FAILED)
            )
        await emit_prompt_status(prompt_id, status.value, rounds_used)  # type: ignore[arg-type]

    async def _kill_commands(self) -> None:
        try:
            await asyncio.wait_for(kill_all_commands(), KILL_TIMEOUT)
        except TimeoutError:
            logger.warning("Killing commands took longer than %s s", KILL_TIMEOUT)

    async def _revert_running_prompts(self) -> None:
        with SessionLocal() as db:
            reverted = repo.revert_running_prompts(db)
        for prompt_id in reverted:
            await emit_prompt_status(prompt_id, "pending", 0)

    async def _take_snapshot(self, prompt_id: int) -> None:
        """Default on_needs_user hook: remember the project's files at the failure."""
        cfg = self._require_config()
        await self._discard_snapshot()
        self.snapshot_id = await take_snapshot(cfg.project_root, cfg.run_id, prompt_id)

    async def _discard_snapshot(self) -> None:
        if self.snapshot_id is not None:
            snapshot_id, self.snapshot_id = self.snapshot_id, None
            try:
                await discard_snapshot(snapshot_id)
            except Exception:
                logger.exception("Could not delete snapshot %s", snapshot_id)

    async def _fail(self, code: ErrorCode, message: str, details: dict[str, Any]) -> None:
        """Fatal error inside the loop: report it and close the run as error."""
        if self.state.state == "stopping":
            return  # stop() is already cleaning up
        cfg = self.config
        prompt_id = self.state.prompt_id
        logger.error("Run %s failed in %s: %s", cfg.run_id if cfg else None, self._phase, message)
        try:
            await self._safely(emit_error(
                code.value, message,
                {"run_id": cfg.run_id if cfg else None, "prompt_id": prompt_id, "phase": self._phase, **details},
            ), "emit error")
            await self._safely(self._kill_commands(), "kill commands")
            await self._safely(self.permissions.cancel_all(), "cancel permissions")
            await self._safely(self._revert_running_prompts(), "revert running prompts")
            await self._safely(self._discard_snapshot(), "discard snapshot")
        finally:
            await self._close_run(RunStatus.ERROR, message)
            self._clear_failed()

    async def _close_run(self, status: RunStatus, error_message: str | None = None) -> None:
        async with self._close_lock:
            run_id = self.config.run_id if self.config else self.state.run_id
            try:
                if run_id is not None:
                    with SessionLocal() as db:
                        repo.finish_run(db, run_id, status, error_message)
                    logger.info("Run %s closed: %s", run_id, status.value)
            except Exception:
                logger.exception("Could not save the end of run %s", run_id)
            finally:
                # Always return to idle, even if the DB write failed: a stuck state blocks everything.
                self.config = None
                self.boss = self.worker = None
                self._phase = ""
                await self.state.reset()

    def _clear_failed(self) -> None:
        self.failed_prompt_id = None
        self.failed_boss = self.failed_worker = None
        self.last_verdict = None


_orchestrator: Orchestrator | None = None


def get_orchestrator() -> Orchestrator:
    global _orchestrator
    if _orchestrator is None:
        _orchestrator = Orchestrator()
    return _orchestrator
