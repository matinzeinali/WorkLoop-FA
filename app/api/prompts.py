"""Prompt queue endpoints.

Prompts run in `position` order (dense 1..N). Adding is always allowed, also during a run.
Only pending prompts can be edited, deleted or reordered; other statuses are locked.
"""

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response, status
from pydantic import BaseModel, Field, StrictInt, StringConstraints
from sqlalchemy.orm import Session

from app.core.errors import ApiError, ErrorCode
from app.db import repository as repo
from app.db.database import get_db
from app.db.models import Prompt, PromptStatus

router = APIRouter(tags=["prompts"])

DbSession = Annotated[Session, Depends(get_db)]

PROMPT_MAX_LENGTH = 20_000

Content = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=PROMPT_MAX_LENGTH)
]


class PromptCreate(BaseModel):
    content: Content
    insert_at: Annotated[StrictInt, Field(ge=1)] | None = None  # 1-based; missing = end of queue


class PromptUpdate(BaseModel):
    content: Content


class PromptOrder(BaseModel):
    prompt_ids: list[StrictInt]


class PromptOut(BaseModel):
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


def _get_or_404(db: Session, prompt_id: int) -> Prompt:
    prompt = repo.get_prompt(db, prompt_id)
    if prompt is None:
        raise ApiError.not_found("Prompt", prompt_id)
    return prompt


def _ensure_pending(prompt: Prompt, action: str) -> None:
    if repo.is_prompt_locked(prompt):
        raise ApiError(
            ErrorCode.PROMPT_LOCKED,
            f"Only pending prompts can be {action}. This one is {prompt.status.value}.",
            409,
            {"id": prompt.id, "status": prompt.status.value},
        )


@router.get("/prompts", response_model=list[PromptOut])
def list_prompts(
    db: DbSession,
    status_filter: Annotated[PromptStatus | None, Query(alias="status")] = None,
) -> list[Prompt]:
    return list(repo.list_prompts(db, status_filter))


@router.post("/prompts", response_model=PromptOut, status_code=status.HTTP_201_CREATED)
def create_prompt(body: PromptCreate, db: DbSession) -> Prompt:
    with repo.QUEUE_LOCK:
        if body.insert_at is not None:
            first, last = repo.prompt_insert_range(db)
            if not first <= body.insert_at <= last:
                reason = (
                    "cannot be before a prompt that already ran or is running"
                    if body.insert_at < first
                    else "is past the end of the queue"
                )
                raise ApiError.validation(
                    "insert_at", f"insert_at {reason}; allowed range is {first}..{last}."
                )
        return repo.create_prompt(db, body.content, body.insert_at)


# Declared before /prompts/{prompt_id} so "order" is not parsed as an id.
@router.put("/prompts/order", response_model=list[PromptOut])
def reorder_prompts(body: PromptOrder, db: DbSession) -> list[Prompt]:
    """Body lists every pending prompt id exactly once, in the new order."""
    with repo.QUEUE_LOCK:
        pending_ids = {p.id for p in repo.list_prompts(db, PromptStatus.PENDING)}
        given = body.prompt_ids
        duplicates = sorted({i for i in given if given.count(i) > 1})
        missing = sorted(pending_ids - set(given))
        extra = sorted(set(given) - pending_ids)  # unknown or locked ids
        if duplicates or missing or extra:
            raise ApiError(
                ErrorCode.VALIDATION_ERROR,
                "prompt_ids must list every pending prompt exactly once.",
                422,
                {"missing": missing, "extra": extra, "duplicates": duplicates},
            )
        return list(repo.reorder_pending_prompts(db, given))


@router.get("/prompts/{prompt_id}", response_model=PromptOut)
def get_prompt(prompt_id: int, db: DbSession) -> Prompt:
    return _get_or_404(db, prompt_id)


@router.put("/prompts/{prompt_id}", response_model=PromptOut)
def update_prompt(prompt_id: int, body: PromptUpdate, db: DbSession) -> Prompt:
    with repo.QUEUE_LOCK:
        prompt = _get_or_404(db, prompt_id)
        db.refresh(prompt)  # the orchestrator may have just claimed it
        _ensure_pending(prompt, "edited")
        return repo.update_prompt_content(db, prompt, body.content)


@router.delete("/prompts/{prompt_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_prompt(prompt_id: int, db: DbSession) -> Response:
    with repo.QUEUE_LOCK:
        prompt = _get_or_404(db, prompt_id)
        db.refresh(prompt)  # the orchestrator may have just claimed it
        _ensure_pending(prompt, "deleted")
        repo.delete_prompt(db, prompt)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
