"""History endpoints: past runs and everything that happened in them."""

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.errors import ApiError
from app.db.database import get_db
from app.db.models import (
    ApiProfile,
    Command,
    LlmRole,
    ManualFix,
    Message,
    PermissionRequest,
    Prompt,
    Run,
    RunPrompt,
    VerdictRecord,
)

router = APIRouter(tags=["history"])

DbSession = Annotated[Session, Depends(get_db)]


class ProfileRef(BaseModel):
    id: int | None  # null when the profile was deleted later
    name: str | None


class RunItem(BaseModel):
    id: int
    status: str
    started_at: datetime
    finished_at: datetime | None
    project_path: str
    boss: ProfileRef | None
    worker: ProfileRef | None
    prompts_done: int
    prompts_failed: int
    error_message: str | None


class RunList(BaseModel):
    items: list[RunItem]
    total: int
    limit: int
    offset: int


class PromptItem(BaseModel):
    id: int
    text: str
    status: str
    rounds_used: int
    started_at: datetime | None
    finished_at: datetime | None


class MessageItem(BaseModel):
    id: int
    prompt_id: int | None
    round: int | None
    agent: str
    role: str
    kind: str
    content: str
    tool_calls: list[dict[str, Any]] | None
    tool_call_id: str | None
    tool_name: str | None
    created_at: datetime


class CommandItem(BaseModel):
    id: int
    message_id: int | None
    command: str
    cwd: str
    reason: str
    status: str
    exit_code: int | None
    timed_out: bool
    killed: bool
    duration_ms: int | None
    stdout: str | None
    stderr: str | None
    created_at: datetime


class PermissionItem(BaseModel):
    id: int
    command: str
    reason: str
    danger_reason: str
    status: str
    created_at: datetime
    resolved_at: datetime | None


class VerdictItem(BaseModel):
    prompt_id: int
    round: int
    status: str
    summary: str
    bugs: list[dict[str, Any]]
    fix_instructions: list[str]
    created_at: datetime


class ManualFixItem(BaseModel):
    prompt_id: int
    added: list[str]
    removed: list[str]
    modified: list[str]
    note: str | None
    created_at: datetime


class RunDetail(BaseModel):
    run: RunItem
    prompts: list[PromptItem]
    messages: list[MessageItem]
    commands: list[CommandItem]
    permissions: list[PermissionItem]
    verdicts: list[VerdictItem]
    manual_fixes: list[ManualFixItem]


def _profile_ref(profile_id: int | None, stored_name: str | None, names: dict[int, str]) -> dict[str, Any] | None:
    """Current name if the profile still exists, else the name saved on the run (id null)."""
    if profile_id is not None and profile_id in names:
        return {"id": profile_id, "name": names[profile_id]}
    if stored_name:
        return {"id": None, "name": stored_name}
    return None


def _run_item(run: Run, names: dict[int, str]) -> dict[str, Any]:
    return {
        "id": run.id,
        "status": run.status.value,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "project_path": run.project_path,
        "boss": _profile_ref(run.boss_profile_id, run.boss_name, names),
        "worker": _profile_ref(run.worker_profile_id, run.worker_name, names),
        "prompts_done": run.prompts_done,
        "prompts_failed": run.prompts_failed,
        "error_message": run.error_message,
    }


def _profile_names(db: Session, runs: list[Run]) -> dict[int, str]:
    ids = {i for r in runs for i in (r.boss_profile_id, r.worker_profile_id) if i is not None}
    if not ids:
        return {}
    return dict(db.execute(select(ApiProfile.id, ApiProfile.name).where(ApiProfile.id.in_(ids))).tuples().all())


@router.get("/runs", response_model=RunList)
def list_runs(
    db: DbSession,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    """Runs, newest first."""
    total = db.scalar(select(func.count(Run.id))) or 0
    runs = list(db.scalars(select(Run).order_by(Run.id.desc()).limit(limit).offset(offset)).all())
    names = _profile_names(db, runs)
    return {"items": [_run_item(r, names) for r in runs], "total": total, "limit": limit, "offset": offset}


@router.get("/runs/{run_id}", response_model=RunDetail)
def get_run_detail(
    run_id: int,
    db: DbSession,
    include_tool_messages: Annotated[bool, Query()] = True,
) -> dict[str, Any]:
    """Everything that happened in one run, each list in time order."""
    run = db.get(Run, run_id)
    if run is None:
        raise ApiError.not_found("Run", run_id)

    prompts = db.scalars(
        select(Prompt).join(RunPrompt, RunPrompt.prompt_id == Prompt.id)
        .where(RunPrompt.run_id == run_id).order_by(RunPrompt.id)
    ).all()
    message_stmt = select(Message).where(Message.run_id == run_id).order_by(Message.id)
    if not include_tool_messages:
        message_stmt = message_stmt.where(Message.role != LlmRole.TOOL)
    messages = db.scalars(message_stmt).all()
    commands = db.scalars(select(Command).where(Command.run_id == run_id).order_by(Command.id)).all()
    permissions = db.scalars(
        select(PermissionRequest).where(PermissionRequest.run_id == run_id).order_by(PermissionRequest.id)
    ).all()
    verdicts = db.scalars(
        select(VerdictRecord).where(VerdictRecord.run_id == run_id).order_by(VerdictRecord.id)
    ).all()
    fixes = db.scalars(select(ManualFix).where(ManualFix.run_id == run_id).order_by(ManualFix.id)).all()

    return {
        "run": _run_item(run, _profile_names(db, [run])),
        "prompts": [
            {"id": p.id, "text": p.content, "status": p.status.value, "rounds_used": p.rounds_used,
             "started_at": p.started_at, "finished_at": p.finished_at}
            for p in prompts
        ],
        "messages": [
            {"id": m.id, "prompt_id": m.prompt_id, "round": m.round, "agent": m.agent.value, "role": m.role.value,
             "kind": m.kind.value, "content": m.content, "tool_calls": m.tool_calls, "tool_call_id": m.tool_call_id,
             "tool_name": m.tool_name, "created_at": m.created_at}
            for m in messages
        ],
        "commands": [
            {"id": c.id, "message_id": c.message_id, "command": c.command, "cwd": c.cwd, "reason": c.reason,
             "status": c.status.value, "exit_code": c.exit_code, "timed_out": c.timed_out, "killed": c.killed,
             "duration_ms": c.duration_ms, "stdout": c.stdout_tail, "stderr": c.stderr_tail,
             "created_at": c.started_at}
            for c in commands
        ],
        "permissions": [
            {"id": p.id, "command": p.command, "reason": p.reason, "danger_reason": p.danger_reason,
             "status": p.status.value, "created_at": p.created_at, "resolved_at": p.resolved_at}
            for p in permissions
        ],
        "verdicts": [
            {"prompt_id": v.prompt_id, "round": v.round, "status": v.status, "summary": v.summary,
             "bugs": v.bugs, "fix_instructions": v.fix_instructions, "created_at": v.created_at}
            for v in verdicts
        ],
        "manual_fixes": [
            {"prompt_id": f.prompt_id, "added": f.added, "removed": f.removed, "modified": f.modified,
             "note": f.note, "created_at": f.created_at}
            for f in fixes
        ],
    }
