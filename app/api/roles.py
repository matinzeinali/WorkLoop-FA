"""Boss / Worker role assignment endpoints."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, StrictInt
from sqlalchemy.orm import Session

from app.api.profiles import ProfileOut
from app.core.errors import ApiError, ErrorCode
from app.core.queue import ensure_no_active_run
from app.db import repository as repo
from app.db.database import get_db
from app.db.models import ApiProfile

router = APIRouter(tags=["roles"])

DbSession = Annotated[Session, Depends(get_db)]


class RolesUpdate(BaseModel):
    boss_profile_id: StrictInt
    worker_profile_id: StrictInt


class RolesOut(BaseModel):
    boss: ProfileOut | None
    worker: ProfileOut | None
    is_complete: bool


def _roles_response(db: Session) -> dict[str, Any]:
    roles = repo.get_roles(db)
    boss = repo.get_profile(db, roles.boss_profile_id) if roles.boss_profile_id else None
    worker = repo.get_profile(db, roles.worker_profile_id) if roles.worker_profile_id else None
    return {"boss": boss, "worker": worker, "is_complete": boss is not None and worker is not None}


@router.get("/roles", response_model=RolesOut)
def get_roles(db: DbSession) -> dict[str, Any]:
    return _roles_response(db)


@router.put("/roles", response_model=RolesOut, dependencies=[Depends(ensure_no_active_run)])
def set_roles(body: RolesUpdate, db: DbSession) -> dict[str, Any]:
    missing: list[dict[str, Any]] = []
    for field, profile_id in (
        ("boss_profile_id", body.boss_profile_id),
        ("worker_profile_id", body.worker_profile_id),
    ):
        profile: ApiProfile | None = repo.get_profile(db, profile_id)
        if profile is None:
            missing.append({"field": field, "id": profile_id})

    if missing:
        names = " and ".join("Boss" if m["field"] == "boss_profile_id" else "Worker" for m in missing)
        raise ApiError(
            ErrorCode.NOT_FOUND, f"{names} profile not found.", 404, {"missing": missing}
        )

    repo.set_roles(db, body.boss_profile_id, body.worker_profile_id)
    return _roles_response(db)
