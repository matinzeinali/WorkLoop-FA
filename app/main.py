"""FastAPI entry point: app setup, startup/shutdown, routers and health check."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api import history, permissions, profiles, project, prompts, roles, runs, ws
from app.config import get_settings
from app.core.crypto import ensure_key
from app.core.errors import ErrorCode, error_response, register_error_handlers
from app.core.events import get_event_bus
from app.core.orchestrator import get_orchestrator
from app.core.permissions import get_permission_manager
from app.db import repository as repo
from app.db.database import SessionLocal, create_tables, utc_now
from app.llm.client import ProviderError
from app.tools.command_tools import kill_all_commands

settings = get_settings()

LOCAL_ORIGIN_REGEX = r"^https?://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?$"

logger = logging.getLogger("app")


def _setup_logging() -> None:
    """Send app.* logs to stderr. Uvicorn only configures its own loggers."""
    if logger.handlers:
        return
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG if settings.debug else logging.INFO)
    logger.propagate = False


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    _setup_logging()
    ensure_key()
    create_tables()
    # Crash recovery: nothing can be running when the server starts.
    with SessionLocal() as db:
        recovered = repo.recover_after_restart(db)
    if any(recovered.values()):
        logger.info(
            "Recovered after restart: %d prompt(s) back to pending, %d run(s) stopped, "
            "%d permission request(s) cancelled, %d command(s) marked killed, %d snapshot(s) removed",
            recovered["prompts"], recovered["runs"], recovered["permissions"], recovered["commands"],
            recovered["snapshots"],
        )
    get_orchestrator()  # connects the permission callback to the run state
    yield
    await get_orchestrator().stop()
    await kill_all_commands()
    await get_permission_manager().cancel_all()
    bus = get_event_bus()
    if bus.subscriber_count:
        logger.info("Closing %d WebSocket client(s)", bus.subscriber_count)
        bus.close_all()
        await bus.wait_until_empty(timeout=3)


app = FastAPI(title=settings.app_name, version=settings.version, lifespan=lifespan)
register_error_handlers(app)

# The frontend runs on the same machine: allow localhost / 127.0.0.1 / [::1] on any port. No auth.
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=(
        f"(?:{LOCAL_ORIGIN_REGEX}|^null$)" if settings.allow_null_origin else LOCAL_ORIGIN_REGEX
    ),
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(ProviderError)
async def _provider_error(_: Request, exc: ProviderError) -> JSONResponse:
    return error_response(502, ErrorCode.PROVIDER_ERROR, exc.message, {"kind": exc.kind})


api = APIRouter(prefix="/api")


@api.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "time": utc_now().isoformat().replace("+00:00", "Z")}


api.include_router(profiles.router)
api.include_router(roles.router)
api.include_router(project.router)
api.include_router(prompts.router)
api.include_router(runs.router)
api.include_router(permissions.router)
api.include_router(history.router)
app.include_router(api)
app.include_router(ws.router)  # /ws, not under /api


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host=settings.host, port=settings.port, reload=settings.debug)
