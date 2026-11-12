"""Permission requests for dangerous Boss commands.

run_command() calls request() and waits. The user answers through
POST /api/permissions/{id}, which calls resolve(). Stopping a run calls
cancel_all(), which unblocks every waiter with "denied".
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from sqlalchemy import select

from app.core.errors import ApiError, ErrorCode
from app.core.events import emit_permission_request, emit_permission_resolved
from app.db.database import SessionLocal, utc_now
from app.db.models import PermissionRequest, PermissionStatus

logger = logging.getLogger(__name__)

# Called with True when the first request starts waiting and False when none are left.
# Step 7 connects this to the run state (waiting_permission).
WaitingCallback = Callable[[bool], Awaitable[None]]


async def _no_op(_waiting: bool) -> None:
    return None


@dataclass
class _Waiter:
    event: asyncio.Event = field(default_factory=asyncio.Event)
    approved: bool = False


class PermissionManager:
    def __init__(self) -> None:
        self._waiters: dict[int, _Waiter] = {}
        self._on_waiting: WaitingCallback = _no_op

    def set_waiting_callback(self, callback: WaitingCallback | None) -> None:
        self._on_waiting = callback or _no_op
    async def request(self, command: str, reason: str, danger_reason: str, run_id: int | None = None) -> bool:
        """Ask the user. Returns True if approved; False if denied or cancelled."""
        with SessionLocal() as db:
            row = PermissionRequest(
                run_id=run_id, command=command, reason=reason, danger_reason=danger_reason,
                status=PermissionStatus.PENDING,
            )
            db.add(row)
            db.commit()
            permission_id = row.id

        waiter = _Waiter()
        self._waiters[permission_id] = waiter
        logger.info("Permission %s requested (waiting for the user)", permission_id)
        try:
            await emit_permission_request(permission_id, command, reason, danger_reason)
            if len(self._waiters) == 1:
                await self._on_waiting(True)
            await waiter.event.wait()
            return waiter.approved
        except asyncio.CancelledError:
            # The task asking for permission was stopped: the request can't be answered any more.
            self._mark(permission_id, PermissionStatus.CANCELLED)
            await emit_permission_resolved(permission_id, False)
            raise
        finally:
            self._waiters.pop(permission_id, None)
            if not self._waiters:
                await self._on_waiting(False)

    async def resolve(self, permission_id: int, approved: bool) -> PermissionRequest:
        """Answer a pending request. Raises ApiError NOT_FOUND / PERMISSION_RESOLVED."""
        with SessionLocal() as db:
            row = db.get(PermissionRequest, permission_id)
            if row is None:
                raise ApiError.not_found("Permission request", permission_id)
            if row.status != PermissionStatus.PENDING:
                raise ApiError(
                    ErrorCode.PERMISSION_RESOLVED,
                    f"Permission request {permission_id} is already {row.status.value}.",
                    409,
                    {"id": permission_id, "status": row.status.value},
                )
            row.status = PermissionStatus.APPROVED if approved else PermissionStatus.DENIED
            row.resolved_at = utc_now()
            db.commit()
            db.refresh(row)
            db.expunge(row)

        logger.info("Permission %s %s by the user", permission_id, "approved" if approved else "denied")
        # Wake the waiting command before awaiting anything: a stop arriving during the emit
        # must not find a request that is approved in the DB but never delivered.
        waiter = self._waiters.get(permission_id)
        if waiter is not None:
            waiter.approved = approved
            waiter.event.set()
        await emit_permission_resolved(permission_id, approved)
        return row

    async def cancel_all(self) -> int:
        """Cancel every pending request (used when a run stops). Waiters get False."""
        with SessionLocal() as db:
            rows = db.scalars(
                select(PermissionRequest).where(PermissionRequest.status == PermissionStatus.PENDING)
            ).all()
            ids = [r.id for r in rows]
            now = utc_now()
            for r in rows:
                r.status = PermissionStatus.CANCELLED
                r.resolved_at = now
            db.commit()
        for permission_id in ids:  # wake every waiter first, then tell the UI
            waiter = self._waiters.get(permission_id)
            if waiter is not None:
                waiter.approved = False
                waiter.event.set()
        for permission_id in ids:
            await emit_permission_resolved(permission_id, False)
        if ids:
            logger.info("Cancelled %d pending permission request(s)", len(ids))
        return len(ids)

    @staticmethod
    def _mark(permission_id: int, status: PermissionStatus) -> None:
        with SessionLocal() as db:
            row = db.get(PermissionRequest, permission_id)
            if row is not None and row.status == PermissionStatus.PENDING:
                row.status = status
                row.resolved_at = utc_now()
                db.commit()


_manager = PermissionManager()


def get_permission_manager() -> PermissionManager:
    return _manager
