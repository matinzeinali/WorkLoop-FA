"""WebSocket endpoint at /ws: one-way live updates from the event bus.

Per connection, three tasks run until the first one ends:
- writer:    subscriber queue -> socket
- reader:    reads client messages (pong/ping only; everything else is ignored)
- heartbeat: sends ping every ws_ping_interval, closes if nothing arrives within ws_pong_timeout
"""

import asyncio
import ipaddress
import json
import logging
from urllib.parse import urlparse

from fastapi import APIRouter, WebSocket
from starlette.websockets import WebSocketState

from app.config import get_settings
from app.core.events import (
    CloseRequest,
    EventBus,
    Subscriber,
    get_event_bus,
    make_ping_message,
    make_pong_message,
)
from app.core.queue import get_run_state

logger = logging.getLogger(__name__)

router = APIRouter()

CLOSE_NORMAL = 1000
CLOSE_POLICY_VIOLATION = 1008
CLOSE_HEARTBEAT_TIMEOUT = 4000  # app-specific: no reply to ping


def _is_loopback(host: str | None) -> bool:
    if not host:
        return False
    if host.lower() == "localhost":
        return True
    try:
        ip = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_loopback


def is_allowed_origin(origin: str | None) -> bool:
    """A missing Origin is allowed (CLI tools). "null" (Electron file://) only with allow_null_origin.

    Otherwise the host must be loopback.
    """
    if origin is None:
        return True
    if origin == "null":
        return get_settings().allow_null_origin
    return _is_loopback(urlparse(origin).hostname)


class _Connection:
    def __init__(self, websocket: WebSocket, bus: EventBus) -> None:
        settings = get_settings()
        self.ws = websocket
        self.bus = bus
        self.ping_interval = settings.ws_ping_interval
        self.pong_timeout = settings.ws_pong_timeout
        self.server_version = settings.version
        self.sub: Subscriber | None = None
        self._send_lock = asyncio.Lock()
        self._activity = asyncio.Event()
        self.close_reason = "client disconnected"

    @property
    def _open(self) -> bool:
        return (
            self.ws.application_state == WebSocketState.CONNECTED
            and self.ws.client_state == WebSocketState.CONNECTED
        )

    async def _send(self, text: str) -> None:
        async with self._send_lock:
            await self.ws.send_text(text)

    async def _close(self, code: int, reason: str) -> None:
        self.close_reason = reason
        async with self._send_lock:
            if self._open:
                try:
                    await self.ws.close(code=code, reason=reason)
                except Exception:  # noqa: BLE001 - socket already gone
                    pass

    async def _writer(self, sub: Subscriber) -> None:
        while True:
            item = await sub.get()
            if isinstance(item, CloseRequest):
                await self._close(item.code, item.reason)
                return
            await self._send(item)

    async def _reader(self) -> None:
        while True:
            message = await self.ws.receive()
            if message["type"] == "websocket.disconnect":
                return
            self._activity.set()  # any message counts as a heartbeat reply
            text = message.get("text")
            if not text:
                continue  # binary frames are ignored
            try:
                payload = json.loads(text)
            except ValueError:
                continue
            if isinstance(payload, dict) and payload.get("type") == "ping":
                await self._send(make_pong_message())
            # "pong" and anything else need no action.

    async def _heartbeat(self) -> None:
        while True:
            await asyncio.sleep(self.ping_interval)
            self._activity.clear()
            await self._send(make_ping_message())
            try:
                await asyncio.wait_for(self._activity.wait(), self.pong_timeout)
            except TimeoutError:
                await self._close(CLOSE_HEARTBEAT_TIMEOUT, "Heartbeat timeout")
                return

    async def run(self) -> None:
        sub = self.sub = self.bus.subscribe()
        client = f"{self.ws.client.host}:{self.ws.client.port}" if self.ws.client else "unknown"
        logger.info(
            "WebSocket client %s connected from %s (%d open)", sub.id, client, self.bus.subscriber_count
        )
        tasks: list[asyncio.Task[None]] = []
        try:
            await self._send(self.bus.make_hello_message(get_run_state(), self.server_version))
            tasks = [
                asyncio.create_task(self._writer(sub), name=f"ws-{sub.id}-writer"),
                asyncio.create_task(self._reader(), name=f"ws-{sub.id}-reader"),
                asyncio.create_task(self._heartbeat(), name=f"ws-{sub.id}-heartbeat"),
            ]
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                if not task.cancelled() and (exc := task.exception()) is not None:
                    logger.debug("WebSocket client %s task %s ended: %r", sub.id, task.get_name(), exc)
        except Exception as exc:  # noqa: BLE001 - e.g. client gone before hello
            logger.debug("WebSocket client %s failed: %r", sub.id, exc)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self.bus.unsubscribe(sub)
            await self._close(CLOSE_NORMAL, self.close_reason)
            logger.info(
                "WebSocket client %s disconnected (%s, %d open)",
                sub.id,
                self.close_reason,
                self.bus.subscriber_count,
            )


@router.websocket("/ws")
async def events_socket(websocket: WebSocket) -> None:
    origin = websocket.headers.get("origin")
    client_host = websocket.client.host if websocket.client else None
    if not is_allowed_origin(origin) or not _is_loopback(client_host):
        logger.warning("Rejected WebSocket from %s (origin %r)", client_host, origin)
        await websocket.close(code=CLOSE_POLICY_VIOLATION)  # before accept -> HTTP 403
        return
    await websocket.accept()
    await _Connection(websocket, get_event_bus()).run()
