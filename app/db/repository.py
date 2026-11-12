"""Data access helpers. Every function takes an open Session and commits its own writes."""

import threading
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from app.core.crypto import encrypt_api_key, make_key_preview
from app.db.database import utc_now
from app.db.models import (
    ApiProfile,
    Command,
    CommandStatus,
    FileSnapshot,
    PermissionRequest,
    PermissionStatus,
    Prompt,
    PromptStatus,
    Run,
    RunPrompt,
    RunStatus,
    Setting,
    Snapshot,
    VerdictRecord,
)

# Settings keys.
BOSS_PROFILE_KEY = "boss_profile_id"
WORKER_PROFILE_KEY = "worker_profile_id"
PROJECT_PATH_KEY = "project_path"

# Fields whose change makes an earlier connection test meaningless.
_CONNECTION_FIELDS = {"provider", "model_name", "api_key", "base_url"}


# --- API profiles -------------------------------------------------------------


def list_profiles(db: Session) -> Sequence[ApiProfile]:
    return db.scalars(select(ApiProfile).order_by(ApiProfile.id)).all()


def get_profile(db: Session, profile_id: int) -> ApiProfile | None:
    return db.get(ApiProfile, profile_id)


def name_key(name: str) -> str:
    """Normalized form used for the case-insensitive unique name check."""
    return name.strip().casefold()


def get_profile_by_name(
    db: Session, name: str, exclude_id: int | None = None
) -> ApiProfile | None:
    """Find a profile by name, ignoring case and surrounding spaces."""
    stmt = select(ApiProfile).where(ApiProfile.name_key == name_key(name))
    if exclude_id is not None:
        stmt = stmt.where(ApiProfile.id != exclude_id)
    return db.scalars(stmt).first()


def create_profile(
    db: Session,
    *,
    name: str,
    provider: str,
    api_key: str,
    model_name: str,
    base_url: str | None = None,
) -> ApiProfile:
    profile = ApiProfile(
        name=name.strip(),
        name_key=name_key(name),
        provider=provider,
        model_name=model_name.strip(),
        base_url=base_url,
        api_key_encrypted=encrypt_api_key(api_key),
        key_preview=make_key_preview(api_key),
    )
    db.add(profile)
    db.commit()
    db.refresh(profile)
    return profile


def update_profile(db: Session, profile: ApiProfile, changes: dict[str, Any]) -> ApiProfile:
    """Apply changes. An empty or missing api_key keeps the stored key."""
    changed_connection = False

    for field in ("name", "provider", "model_name", "base_url"):
        if field not in changes:
            continue
        value = changes[field]
        if isinstance(value, str) and field != "base_url":
            value = value.strip()
        if getattr(profile, field) != value:
            setattr(profile, field, value)
            changed_connection |= field in _CONNECTION_FIELDS
            if field == "name":
                profile.name_key = name_key(value)

    api_key = changes.get("api_key")
    if api_key:
        profile.api_key_encrypted = encrypt_api_key(api_key)
        profile.key_preview = make_key_preview(api_key)
        changed_connection = True

    if changed_connection:
        profile.last_test_ok = None
        profile.last_test_message = None
        profile.last_tested_at = None

    db.commit()
    db.refresh(profile)
    return profile


def save_test_result(
    db: Session, profile: ApiProfile, ok: bool, message: str, tested_at: datetime | None = None
) -> ApiProfile:
    profile.last_test_ok = ok
    profile.last_test_message = message
    profile.last_tested_at = tested_at or utc_now()
    db.commit()
    db.refresh(profile)
    return profile


def delete_profile(db: Session, profile: ApiProfile) -> None:
    db.delete(profile)
    db.commit()


def is_profile_in_use(db: Session, profile_id: int) -> bool:
    """True if the profile is assigned as Boss or Worker."""
    roles = get_roles(db)
    return profile_id in (roles.boss_profile_id, roles.worker_profile_id)


# --- Settings -----------------------------------------------------------------


def get_setting(db: Session, key: str) -> str | None:
    row = db.get(Setting, key)
    return row.value if row else None


def _put_setting(db: Session, key: str, value: str | None) -> None:
    """Stage a setting change without committing."""
    row = db.get(Setting, key)
    if row is None:
        db.add(Setting(key=key, value=value))
    else:
        row.value = value


def set_setting(db: Session, key: str, value: str | None) -> None:
    _put_setting(db, key, value)
    db.commit()


def _as_int(value: str | None) -> int | None:
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


# --- Roles --------------------------------------------------------------------


@dataclass(frozen=True)
class Roles:
    boss_profile_id: int | None
    worker_profile_id: int | None


def get_roles(db: Session) -> Roles:
    return Roles(
        boss_profile_id=_as_int(get_setting(db, BOSS_PROFILE_KEY)),
        worker_profile_id=_as_int(get_setting(db, WORKER_PROFILE_KEY)),
    )


def set_roles(db: Session, boss_profile_id: int, worker_profile_id: int) -> Roles:
    """Save both roles in one transaction."""
    _put_setting(db, BOSS_PROFILE_KEY, str(boss_profile_id))
    _put_setting(db, WORKER_PROFILE_KEY, str(worker_profile_id))
    db.commit()
    return Roles(boss_profile_id, worker_profile_id)


# --- Project ------------------------------------------------------------------


def get_project(db: Session) -> str | None:
    return get_setting(db, PROJECT_PATH_KEY) or None


def set_project(db: Session, path: str | None) -> None:
    set_setting(db, PROJECT_PATH_KEY, path)


# --- Prompts ------------------------------------------------------------------
#
# Queue order is `position`, always dense 1..N. Only pending prompts can be
# edited, deleted or moved; every other status is locked. A new prompt can't be
# placed before a locked prompt.

# Held while reading-and-changing the queue, by the prompt API (worker threads) and by the
# orchestrator, so a prompt can't be edited or deleted at the moment it is picked to run.
QUEUE_LOCK = threading.RLock()

# Statuses that end a prompt (finished_at gets set).
_FINAL_STATUSES = {PromptStatus.DONE, PromptStatus.FAILED, PromptStatus.MANUAL_FIX}


def is_prompt_locked(prompt: Prompt) -> bool:
    return prompt.status != PromptStatus.PENDING


def list_prompts(db: Session, status: PromptStatus | None = None) -> Sequence[Prompt]:
    stmt = select(Prompt).order_by(Prompt.position, Prompt.id)
    if status is not None:
        stmt = stmt.where(Prompt.status == status)
    return db.scalars(stmt).all()


def get_prompt(db: Session, prompt_id: int) -> Prompt | None:
    return db.get(Prompt, prompt_id)


def prompt_insert_range(db: Session) -> tuple[int, int]:
    """Allowed insert_at values (inclusive): after the last locked prompt, up to N + 1."""
    count = db.scalar(select(func.count(Prompt.id))) or 0
    last_locked = db.scalar(
        select(func.max(Prompt.position)).where(Prompt.status != PromptStatus.PENDING)
    )
    return (last_locked or 0) + 1, count + 1


def _compact_positions(db: Session) -> None:
    """Renumber all prompts 1..N in their current order."""
    for index, prompt in enumerate(list_prompts(db), start=1):
        if prompt.position != index:
            prompt.position = index


def create_prompt(db: Session, content: str, insert_at: int | None = None) -> Prompt:
    """Add a pending prompt at insert_at (1-based), or at the end. Caller checks the range."""
    _, end = prompt_insert_range(db)
    position = end if insert_at is None else insert_at
    for prompt in list_prompts(db):
        if prompt.position >= position:
            prompt.position += 1
    new = Prompt(content=content, status=PromptStatus.PENDING, position=position)
    db.add(new)
    db.flush()
    _compact_positions(db)
    db.commit()
    db.refresh(new)
    return new


def update_prompt_content(db: Session, prompt: Prompt, content: str) -> Prompt:
    prompt.content = content
    db.commit()
    db.refresh(prompt)
    return prompt


def delete_prompt(db: Session, prompt: Prompt) -> None:
    db.delete(prompt)  # runs, messages, permissions and snapshots cascade
    db.flush()
    _compact_positions(db)
    db.commit()


def reorder_pending_prompts(db: Session, ordered_ids: Sequence[int]) -> Sequence[Prompt]:
    """Put pending prompts in the given order. Locked prompts keep their positions.

    Pending prompts reuse their own current position slots. ordered_ids must be
    exactly the pending ids (the API checks this).
    """
    pending = list(list_prompts(db, PromptStatus.PENDING))
    by_id = {p.id: p for p in pending}
    slots = sorted(p.position for p in pending)
    for slot, prompt_id in zip(slots, ordered_ids, strict=True):
        by_id[prompt_id].position = slot
    db.commit()
    return list_prompts(db)


def next_pending_prompt(db: Session) -> Prompt | None:
    """The prompt the queue should run next."""
    stmt = (
        select(Prompt)
        .where(Prompt.status == PromptStatus.PENDING)
        .order_by(Prompt.position, Prompt.id)
        .limit(1)
    )
    return db.scalars(stmt).first()


def set_prompt_status(
    db: Session, prompt: Prompt, status: PromptStatus, rounds_used: int | None = None
) -> Prompt:
    """Change status and keep started_at / finished_at in step. Used by the orchestrator."""
    with QUEUE_LOCK:
        now = utc_now()
        prompt.status = status
        if rounds_used is not None:
            prompt.rounds_used = rounds_used
        if status == PromptStatus.RUNNING:
            prompt.started_at = now
            prompt.finished_at = None
        elif status in _FINAL_STATUSES:
            prompt.finished_at = now
        elif status == PromptStatus.PENDING:
            prompt.started_at = None
            prompt.finished_at = None
            prompt.rounds_used = 0
        db.commit()
        db.refresh(prompt)
        return prompt


def insert_prompt_after(db: Session, after_prompt_id: int, content: str) -> Prompt:
    """New pending prompt directly after another one (used by the fix prompt after a failure)."""
    with QUEUE_LOCK:
        after = get_prompt(db, after_prompt_id)
        if after is None:
            return create_prompt(db, content)
        return create_prompt(db, content, insert_at=after.position + 1)


def claim_next_pending_prompt(db: Session, run_id: int | None = None) -> Prompt | None:
    """Atomically mark the first pending prompt as running and return it (None if the queue is empty).

    With run_id, the prompt is also linked to that run (run_prompts) for the history.
    """
    with QUEUE_LOCK:
        while True:
            prompt = next_pending_prompt(db)
            if prompt is None:
                return None
            result = db.execute(
                update(Prompt)
                .where(Prompt.id == prompt.id, Prompt.status == PromptStatus.PENDING)
                .values(status=PromptStatus.RUNNING, rounds_used=0, started_at=utc_now(), finished_at=None)
            )
            db.commit()
            if getattr(result, "rowcount", 0) == 1:
                if run_id is not None:
                    linked = db.scalar(
                        select(RunPrompt.id).where(RunPrompt.run_id == run_id, RunPrompt.prompt_id == prompt.id)
                    )
                    if linked is None:
                        db.add(RunPrompt(run_id=run_id, prompt_id=prompt.id))
                        db.commit()
                db.refresh(prompt)
                return prompt
            db.expire_all()  # changed meanwhile; try the next one


def set_prompt_rounds(db: Session, prompt_id: int, rounds_used: int) -> None:
    with QUEUE_LOCK:
        db.execute(update(Prompt).where(Prompt.id == prompt_id).values(rounds_used=rounds_used))
        db.commit()


def revert_running_prompts(db: Session) -> list[int]:
    """Put every running prompt back to pending (stop, error, restart). Returns their ids."""
    with QUEUE_LOCK:
        ids = list(db.scalars(select(Prompt.id).where(Prompt.status == PromptStatus.RUNNING)).all())
        if ids:
            db.execute(
                update(Prompt)
                .where(Prompt.id.in_(ids))
                .values(status=PromptStatus.PENDING, rounds_used=0, started_at=None, finished_at=None)
            )
            db.commit()
        return ids


# --- Runs -----------------------------------------------------------------------


def create_run(
    db: Session,
    boss_profile_id: int,
    worker_profile_id: int,
    project_path: str,
    boss_name: str | None = None,
    worker_name: str | None = None,
) -> Run:
    run = Run(
        status=RunStatus.RUNNING, boss_profile_id=boss_profile_id, worker_profile_id=worker_profile_id,
        project_path=project_path, boss_name=boss_name, worker_name=worker_name,
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    return run
def set_run_status(db: Session, run_id: int, status: RunStatus) -> None:
    db.execute(update(Run).where(Run.id == run_id).values(status=status))
    db.commit()


def add_run_result(db: Session, run_id: int, done: int = 0, failed: int = 0) -> None:
    db.execute(
        update(Run)
        .where(Run.id == run_id)
        .values(prompts_done=Run.prompts_done + done, prompts_failed=Run.prompts_failed + failed)
    )
    db.commit()


def finish_run(db: Session, run_id: int, status: RunStatus, error_message: str | None = None) -> bool:
    """Close a run that is still open. False if it was already closed."""
    result = db.execute(
        update(Run)
        .where(Run.id == run_id, Run.status.in_((RunStatus.RUNNING, RunStatus.NEEDS_USER)))
        .values(status=status, finished_at=utc_now(), error_message=error_message)
    )
    db.commit()
    return getattr(result, "rowcount", 0) == 1


def save_verdict(
    db: Session,
    run_id: int | None,
    prompt_id: int,
    round_no: int,
    status: str,
    summary: str,
    bugs: list[dict[str, Any]],
    fix_instructions: list[str],
) -> None:
    db.add(VerdictRecord(
        run_id=run_id, prompt_id=prompt_id, round=round_no, status=status, summary=summary,
        bugs=bugs, fix_instructions=fix_instructions,
    ))
    db.commit()


def recover_after_restart(db: Session) -> dict[str, int]:
    """Crash recovery at startup: nothing can be running when the server starts."""
    prompts = revert_running_prompts(db)
    runs = db.execute(
        update(Run)
        .where(Run.status.in_((RunStatus.RUNNING, RunStatus.NEEDS_USER)))
        .values(status=RunStatus.STOPPED, finished_at=utc_now(), error_message="Server restarted")
    )
    permissions = db.execute(
        update(PermissionRequest)
        .where(PermissionRequest.status == PermissionStatus.PENDING)
        .values(status=PermissionStatus.CANCELLED, resolved_at=utc_now())
    )
    # Commands that were running died with the server process.
    commands = db.execute(
        update(Command)
        .where(Command.status == CommandStatus.RUNNING)
        .values(status=CommandStatus.FINISHED, killed=True, finished_at=utc_now())
    )
    # Snapshots only matter while a run waits in needs_user; every run is closed now.
    db.execute(delete(FileSnapshot))
    snapshots = db.execute(delete(Snapshot))
    db.commit()
    return {
        "prompts": len(prompts),
        "runs": getattr(runs, "rowcount", 0),
        "permissions": getattr(permissions, "rowcount", 0),
        "commands": getattr(commands, "rowcount", 0),
        "snapshots": getattr(snapshots, "rowcount", 0),
    }
