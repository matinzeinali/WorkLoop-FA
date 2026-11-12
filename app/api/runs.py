"""Run endpoints: current state, start and stop."""

import asyncio
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import APIRouter, status
from pydantic import BaseModel, StringConstraints

from app.core.crypto import CryptoError
from app.core.errors import ApiError, ErrorCode
from app.core.orchestrator import NotNeedsUserError, get_orchestrator
from app.core.queue import get_run_state, get_run_state_manager, is_run_active
from app.db import repository as repo
from app.db.database import SessionLocal
from app.db.models import ApiProfile, PromptStatus
from app.llm.client import ModelConfig, TestResult, config_from_profile, test_connection

router = APIRouter(tags=["runs"])

RunStateName = Literal["idle", "running", "waiting_permission", "needs_user", "stopping"]


class RunStateOut(BaseModel):
    state: RunStateName
    run_id: int | None
    prompt_id: int | None
    round: int | None
    max_rounds: int


class RunActionOut(BaseModel):
    run_id: int | None
    state: RunStateName


class FixPromptIn(BaseModel):
    text: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=20_000)]


class ManualFixIn(BaseModel):
    note: Annotated[str, StringConstraints(strip_whitespace=True, max_length=5000)] | None = None


class PromptBrief(BaseModel):
    model_config = {"from_attributes": True}

    id: int
    content: str
    status: PromptStatus
    position: int
    rounds_used: int
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime
    updated_at: datetime


class FixPromptOut(BaseModel):
    prompt: PromptBrief
    run_state: RunStateOut


class ChangesOut(BaseModel):
    added: list[str]
    removed: list[str]
    modified: list[str]


class ManualFixOut(BaseModel):
    changes: ChangesOut
    run_state: RunStateOut


def _not_needs_user() -> ApiError:
    return ApiError(
        ErrorCode.NOT_NEEDS_USER, "This is only possible while the run waits for you after a failed prompt.", 409,
        {"state": get_run_state()["state"]},
    )


def _run_active_error() -> ApiError:
    return ApiError(ErrorCode.RUN_ACTIVE, "A run is already in progress.", 409, {"run_id": get_run_state()["run_id"]})


async def _test_profile(profile: ApiProfile, model: ModelConfig | None) -> TestResult:
    if model is None:
        return TestResult(False, "Stored API key could not be decrypted. Enter the key again.", 0, profile.model_name)
    return await test_connection(model.provider, model.api_key, model.model_name, model.base_url)


def _model(profile: ApiProfile) -> ModelConfig | None:
    try:
        return config_from_profile(profile)
    except CryptoError:
        return None


@router.get("/run", response_model=RunStateOut)
def get_run() -> dict[str, Any]:
    """Same object as the run_state event data, for clients that reconnect."""
    return dict(get_run_state())


@router.post("/run/start", response_model=RunActionOut, status_code=status.HTTP_202_ACCEPTED)
async def start_run() -> dict[str, Any]:
    orchestrator = get_orchestrator()
    state = get_run_state_manager()
    # The lock covers the whole sequence, so a double click can't start two runs.
    async with orchestrator.start_lock:
        if is_run_active():
            raise _run_active_error()

        with SessionLocal() as db:
            roles = repo.get_roles(db)
            boss = repo.get_profile(db, roles.boss_profile_id) if roles.boss_profile_id else None
            worker = repo.get_profile(db, roles.worker_profile_id) if roles.worker_profile_id else None
            if boss is None or worker is None:
                missing = [name for name, p in (("boss", boss), ("worker", worker)) if p is None]
                raise ApiError(
                    ErrorCode.ROLES_NOT_SET, "Choose a profile for both the Boss and the Worker first.", 409,
                    {"missing": missing},
                )
            project = repo.get_project(db)
            if not project:
                raise ApiError(ErrorCode.PROJECT_NOT_SET, "Choose a project folder first.", 409)
            if not Path(project).is_dir():
                raise ApiError(
                    ErrorCode.PROJECT_NOT_FOUND, f"The project folder no longer exists: {project}", 404,
                    {"path": project},
                )
            if repo.next_pending_prompt(db) is None:
                raise ApiError(ErrorCode.NO_PENDING_PROMPTS, "Add a prompt to the queue first.", 409)
            boss_model, worker_model = _model(boss), _model(worker)

        state.starting = True  # blocks role / project changes during the connection tests
        try:
            same = boss.id == worker.id
            if same:
                boss_result = await _test_profile(boss, boss_model)
                worker_result = boss_result
            else:
                boss_result, worker_result = await asyncio.gather(
                    _test_profile(boss, boss_model), _test_profile(worker, worker_model)
                )
            with SessionLocal() as db:
                for profile, result in ((boss, boss_result), (worker, worker_result)):
                    fresh = repo.get_profile(db, profile.id)
                    if fresh is not None:
                        repo.save_test_result(db, fresh, result.ok, result.message)
                    if same:
                        break
            if not (boss_result.ok and worker_result.ok):
                raise ApiError(
                    ErrorCode.API_TEST_FAILED,
                    "Could not connect with the Boss or Worker profile.",
                    502,
                    {
                        "boss": {"ok": boss_result.ok, "message": boss_result.message},
                        "worker": {"ok": worker_result.ok, "message": worker_result.message},
                    },
                )
            assert boss_model is not None and worker_model is not None
            run_id = await orchestrator.start(
                boss.id, worker.id, boss_model, worker_model, Path(project), boss.name, worker.name
            )
        finally:
            state.starting = False
    return {"run_id": run_id, "state": get_run_state()["state"]}


@router.post("/run/stop", response_model=RunActionOut)
async def stop_run() -> dict[str, Any]:
    current = get_run_state()
    if not is_run_active():
        return {"run_id": current["run_id"], "state": current["state"]}
    after = await get_orchestrator().stop()
    return {"run_id": current["run_id"], "state": after["state"]}


@router.post("/run/fix-prompt", response_model=FixPromptOut)
async def fix_prompt(body: FixPromptIn) -> dict[str, Any]:
    """After a failure: queue a new prompt right after the failed one and continue the run."""
    try:
        prompt = await get_orchestrator().fix_prompt(body.text)
    except NotNeedsUserError:
        raise _not_needs_user() from None
    return {"prompt": prompt, "run_state": dict(get_run_state())}


@router.post("/run/manual-fix", response_model=ManualFixOut, status_code=status.HTTP_202_ACCEPTED)
async def manual_fix(body: ManualFixIn | None = None) -> dict[str, Any]:
    """After a failure: the user fixed it by hand. The agents read the changes in the background."""
    try:
        changes = await get_orchestrator().manual_fix(body.note if body else None)
    except NotNeedsUserError:
        raise _not_needs_user() from None
    return {"changes": changes.model_dump(), "run_state": dict(get_run_state())}
