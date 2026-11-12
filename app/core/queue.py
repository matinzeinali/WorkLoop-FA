"""Run state: what the work loop is doing right now (single global instance).

States:
    idle                no run
    running             the orchestrator is working on a prompt
    waiting_permission  a dangerous command is waiting for the user's answer
    needs_user          a prompt failed after max_rounds; the run is paused for the user
    stopping            stop() is cleaning up

Every change is emitted as a run_state event. GET /api/run and the WebSocket
hello return the same object.
"""

from __future__ import annotations

import asyncio
from typing import Any, Final

from app.config import get_settings
from app.core.errors import ApiError, ErrorCode
from app.core.events import RunStateData, RunStateName, emit_run_state

ACTIVE_STATES: Final = frozenset({"running", "waiting_permission", "needs_user", "stopping"})

_UNSET: Any = object()


class RunStateManager:
    def __init__(self) -> None:
        self.state: RunStateName = "idle"
        self.run_id: int | None = None
        self.prompt_id: int | None = None
        self.round: int | None = None
        # True between "start accepted" and "run created" (connection tests), so roles and
        # project can't change while a run is starting.
        self.starting = False
        self._lock = asyncio.Lock()

    @property
    def max_rounds(self) -> int:
        return get_settings().max_rounds

    def snapshot(self) -> RunStateData:
        return {
            "state": self.state,
            "run_id": self.run_id,
            "prompt_id": self.prompt_id,
            "round": self.round,
            "max_rounds": self.max_rounds,
        }

    def is_active(self) -> bool:
        return self.state in ACTIVE_STATES

    async def set_state(
        self,
        state: RunStateName | None = None,
        *,
        run_id: int | None = _UNSET,
        prompt_id: int | None = _UNSET,
        round: int | None = _UNSET,
    ) -> RunStateData:
        """Change any of the fields; emits run_state only when something changed."""
        async with self._lock:
            before = self.snapshot()
            if state is not None:
                self.state = state
            if run_id is not _UNSET:
                self.run_id = run_id
            if prompt_id is not _UNSET:
                self.prompt_id = prompt_id
            if round is not _UNSET:
                self.round = round
            after = self.snapshot()
            if after != before:
                await emit_run_state(
                    after["state"], after["run_id"], after["prompt_id"], after["round"], after["max_rounds"]
                )
            return after

    async def reset(self) -> RunStateData:
        return await self.set_state("idle", run_id=None, prompt_id=None, round=None)

    async def on_permission_waiting(self, waiting: bool) -> None:
        """PermissionManager callback: waiting_permission while a dangerous command waits."""
        if waiting and self.state == "running":
            await self.set_state("waiting_permission")
        elif not waiting and self.state == "waiting_permission":
            await self.set_state("running")


_state = RunStateManager()


def get_run_state_manager() -> RunStateManager:
    return _state


def get_run_state() -> RunStateData:
    """Current run state, used by GET /api/run and the WebSocket hello."""
    return _state.snapshot()


def is_run_active() -> bool:
    return _state.is_active()


def ensure_no_active_run() -> None:
    """Raise 409 RUN_ACTIVE if a run is in progress or starting. Usable as a FastAPI dependency."""
    if _state.is_active() or _state.starting:
        raise ApiError(
            ErrorCode.RUN_ACTIVE,
            "A run is in progress. Stop it or wait for it to finish first.",
            409,
        )
