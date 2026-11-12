"""Permission endpoints: the user approves or denies dangerous Boss commands."""

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, StrictBool
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.permissions import get_permission_manager
from app.db.database import get_db
from app.db.models import PermissionRequest, PermissionStatus

router = APIRouter(tags=["permissions"])

DbSession = Annotated[Session, Depends(get_db)]


class PermissionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    command: str
    reason: str
    danger_reason: str
    status: PermissionStatus
    created_at: datetime
    resolved_at: datetime | None


class PermissionAnswer(BaseModel):
    approved: StrictBool


@router.get("/permissions/pending", response_model=list[PermissionOut])
def list_pending(db: DbSession) -> list[PermissionRequest]:
    stmt = (
        select(PermissionRequest)
        .where(PermissionRequest.status == PermissionStatus.PENDING)
        .order_by(PermissionRequest.id)
    )
    return list(db.scalars(stmt).all())


@router.post("/permissions/{permission_id}", response_model=PermissionOut)
async def answer_permission(permission_id: int, body: PermissionAnswer) -> PermissionRequest:
    # async: resolve() wakes the waiting command on the event loop.
    return await get_permission_manager().resolve(permission_id, body.approved)
