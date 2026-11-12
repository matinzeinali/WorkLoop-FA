"""In-process event bus for live updates (server -> frontend).

Later steps only call the typed emit_* helpers below; they never touch WebSockets.
Each WebSocket client gets its own Subscriber with a bounded queue. A client
that falls behind is dropped instead of slowing everyone else down.

Envelope sent to clients:
    {"type": "<event_type>", "seq": 42, "ts": "2026-11-02T10:00:00.123Z", "data": {...}}

`seq` is a global counter that grows by 1 for each published event, so a client
can detect gaps. The `hello` message carries the seq of the last event already
published (the next event will be seq + 1). ping/pong have no seq.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
from collections.abc import Callable, Hashable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final, Generic, Literal, TypedDict, TypeVar

logger = logging.getLogger(__name__)

SUBSCRIBER_QUEUE_SIZE: Final = 1000

TOKEN_FLUSH_INTERVAL: Final = 0.05  # seconds
TOKEN_FLUSH_SIZE: Final = 200  # characters
COMMAND_FLUSH_INTERVAL: Final = 0.1  # seconds
COMMAND_FLUSH_SIZE: Final = 4 * 1024  # characters
COMMAND_OUTPUT_MAX: Final = 16 * 1024  # characters per command_output event
TOOL_SUMMARY_MAX: Final = 500  # characters

# WebSocket close codes used by the bus.
CLOSE_GOING_AWAY: Final = 1001
CLOSE_TRY_AGAIN_LATER: Final = 1013

EVENT_TYPES: Final = frozenset(
    {
        "run_state",
        "prompt_status",
        "round_started",
        "message_start",
        "token",
        "message_end",
        "tool_call",
        "tool_result",
        "command_started",
        "command_output",
        "command_finished",
        "permission_request",
        "permission_resolved",
        "verdict",
        "prompt_done",
        "needs_user",
        "manual_fix_applied",
        "queue_empty",
        "error",
    }
)
# "hello" is sent directly to one client on connect; it never goes through the bus.


# --- Event data shapes --------------------------------------------------------

RunStateName = Literal["idle", "running", "waiting_permission", "needs_user", "stopping"]
PromptStatusName = Literal["pending", "running", "done", "failed", "manual_fix"]
AgentName = Literal["boss", "worker"]
MessageKind = Literal["task", "report", "verdict", "thinking", "system"]
StreamName = Literal["stdout", "stderr"]
VerdictStatus = Literal["approved", "rejected"]


class RunStateData(TypedDict):
    state: RunStateName
    run_id: int | None
    prompt_id: int | None
    round: int | None
    max_rounds: int


class HelloData(TypedDict):
    server_version: str
    run_state: RunStateData


class PromptStatusData(TypedDict):
    prompt_id: int
    status: PromptStatusName
    rounds_used: int


class RoundStartedData(TypedDict):
    prompt_id: int
    round: int
    max_rounds: int


class MessageStartData(TypedDict):
    message_id: int
    prompt_id: int
    round: int
    agent: AgentName
    kind: MessageKind


class TokenData(TypedDict):
    message_id: int
    text: str


class MessageEndData(TypedDict):
    message_id: int
    content_length: int


class ToolCallData(TypedDict):
    call_id: str
    message_id: int | None
    agent: AgentName
    name: str
    arguments: dict[str, Any]


class ToolResultData(TypedDict):
    call_id: str
    agent: AgentName
    name: str
    ok: bool
    summary: str


class CommandStartedData(TypedDict):
    command_id: int
    command: str
    cwd: str
    reason: str


class CommandOutputData(TypedDict):
    command_id: int
    stream: StreamName
    text: str


class CommandFinishedData(TypedDict):
    command_id: int
    exit_code: int | None
    duration_ms: int
    killed: bool
    timed_out: bool


class PermissionRequestData(TypedDict):
    permission_id: int
    command: str
    reason: str
    danger_reason: str


class PermissionResolvedData(TypedDict):
    permission_id: int
    approved: bool


class VerdictData(TypedDict):
    prompt_id: int
    round: int
    status: VerdictStatus
    summary: str
    bugs: list[Any]
    fix_instructions: list[Any]


class PromptDoneData(TypedDict):
    prompt_id: int
    rounds_used: int


class NeedsUserData(TypedDict):
    prompt_id: int
    rounds_used: int
    summary: str
    bugs: list[Any]


class ManualFixAppliedData(TypedDict):
    prompt_id: int
    added: list[str]
    removed: list[str]
    modified: list[str]


class QueueEmptyData(TypedDict):
    pass


class ErrorData(TypedDict):
    code: str
    message: str
    details: dict[str, Any] | list[Any] | None


# --- Envelope helpers ---------------------------------------------------------


def utc_timestamp() -> str:
    """ISO 8601 UTC with milliseconds, e.g. 2026-11-02T10:00:00.123Z."""
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _dumps(obj: Mapping[str, Any]) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=str)


def make_ping_message() -> str:
    return _dumps({"type": "ping", "data": {}, "ts": utc_timestamp()})


def make_pong_message() -> str:
    return _dumps({"type": "pong", "data": {}, "ts": utc_timestamp()})


# --- Subscribers and bus ------------------------------------------------------


@dataclass(frozen=True)
class CloseRequest:
    """Placed in a subscriber queue to tell its socket to close."""

    code: int
    reason: str


class Subscriber:
    """One WebSocket client's view of the bus."""

    _ids = itertools.count(1)

    def __init__(self, maxsize: int = SUBSCRIBER_QUEUE_SIZE) -> None:
        self.id: int = next(self._ids)
        self._queue: asyncio.Queue[str | CloseRequest] = asyncio.Queue(maxsize)
        self.closed = False

    def offer(self, message: str) -> bool:
        """Queue a message without waiting. False means the queue is full."""
        if self.closed:
            return True
        try:
            self._queue.put_nowait(message)
        except asyncio.QueueFull:
            return False
        return True

    def close(self, code: int, reason: str) -> None:
        """Ask the socket to close. Pending events are discarded so the request always fits."""
        if self.closed:
            return
        self.closed = True
        while not self._queue.empty():
            self._queue.get_nowait()
        self._queue.put_nowait(CloseRequest(code, reason))

    async def get(self) -> str | CloseRequest:
        return await self._queue.get()


class EventBus:
    def __init__(self, queue_size: int = SUBSCRIBER_QUEUE_SIZE) -> None:
        self._queue_size = queue_size
        self._subscribers: set[Subscriber] = set()
        self._seq = 0
    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def subscribe(self) -> Subscriber:
        sub = Subscriber(self._queue_size)
        self._subscribers.add(sub)
        return sub

    def unsubscribe(self, subscriber: Subscriber) -> None:
        self._subscribers.discard(subscriber)

    def publish_nowait(self, type: str, data: Mapping[str, Any]) -> int:
        """Publish without awaiting. Returns the event's seq. Never blocks."""
        if type not in EVENT_TYPES:
            raise ValueError(f"Unknown event type: {type!r}")
        self._seq += 1
        message = _dumps({"type": type, "seq": self._seq, "ts": utc_timestamp(), "data": data})
        for sub in list(self._subscribers):
            if not sub.offer(message):
                logger.warning("Dropping slow WebSocket client %s (event queue full)", sub.id)
                self._subscribers.discard(sub)
                sub.close(CLOSE_TRY_AGAIN_LATER, "Client too slow")
        return self._seq

    async def publish(self, type: str, data: Mapping[str, Any]) -> int:
        """Publish, then yield once so socket writers can drain their queues.

        Without the yield, a burst of over 1000 events in one loop iteration
        would fill every queue and drop every client, not just slow ones.
        """
        seq = self.publish_nowait(type, data)
        await asyncio.sleep(0)
        return seq

    def make_hello_message(self, run_state: RunStateData, server_version: str) -> str:
        data: HelloData = {"server_version": server_version, "run_state": run_state}
        return _dumps({"type": "hello", "seq": self._seq, "ts": utc_timestamp(), "data": data})

    def close_all(self, code: int = CLOSE_GOING_AWAY, reason: str = "Server shutting down") -> None:
        for sub in list(self._subscribers):
            sub.close(code, reason)

    async def wait_until_empty(self, timeout: float) -> bool:
        """Wait for all subscribers to unsubscribe. False if some are still there after timeout."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while self._subscribers and loop.time() < deadline:
            await asyncio.sleep(0.05)
        return not self._subscribers


_bus = EventBus()


def get_event_bus() -> EventBus:
    return _bus


# --- Typed emit helpers -------------------------------------------------------


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 3] + "..."


async def emit_run_state(
    state: RunStateName, run_id: int | None, prompt_id: int | None, round: int | None, max_rounds: int
) -> None:
    data: RunStateData = {
        "state": state,
        "run_id": run_id,
        "prompt_id": prompt_id,
        "round": round,
        "max_rounds": max_rounds,
    }
    await _bus.publish("run_state", data)


async def emit_prompt_status(prompt_id: int, status: PromptStatusName, rounds_used: int) -> None:
    data: PromptStatusData = {"prompt_id": prompt_id, "status": status, "rounds_used": rounds_used}
    await _bus.publish("prompt_status", data)


async def emit_round_started(prompt_id: int, round: int, max_rounds: int) -> None:
    data: RoundStartedData = {"prompt_id": prompt_id, "round": round, "max_rounds": max_rounds}
    await _bus.publish("round_started", data)


async def emit_message_start(
    message_id: int, prompt_id: int, round: int, agent: AgentName, kind: MessageKind
) -> None:
    data: MessageStartData = {
        "message_id": message_id,
        "prompt_id": prompt_id,
        "round": round,
        "agent": agent,
        "kind": kind,
    }
    await _bus.publish("message_start", data)


async def emit_token(message_id: int, text: str) -> None:
    """Publish one token event directly. Prefer TokenBatcher for streamed LLM output."""
    data: TokenData = {"message_id": message_id, "text": text}
    await _bus.publish("token", data)


async def emit_message_end(message_id: int, content_length: int) -> None:
    data: MessageEndData = {"message_id": message_id, "content_length": content_length}
    await _bus.publish("message_end", data)


async def emit_tool_call(
    call_id: str, message_id: int | None, agent: AgentName, name: str, arguments: dict[str, Any]
) -> None:
    data: ToolCallData = {
        "call_id": call_id,
        "message_id": message_id,
        "agent": agent,
        "name": name,
        "arguments": arguments,
    }
    await _bus.publish("tool_call", data)


async def emit_tool_result(call_id: str, agent: AgentName, name: str, ok: bool, summary: str) -> None:
    """summary is cut to TOOL_SUMMARY_MAX characters; never pass full file contents."""
    data: ToolResultData = {
        "call_id": call_id,
        "agent": agent,
        "name": name,
        "ok": ok,
        "summary": _truncate(summary, TOOL_SUMMARY_MAX),
    }
    await _bus.publish("tool_result", data)


async def emit_command_started(command_id: int, command: str, cwd: str, reason: str) -> None:
    data: CommandStartedData = {"command_id": command_id, "command": command, "cwd": cwd, "reason": reason}
    await _bus.publish("command_started", data)


def _publish_command_output(command_id: int, stream: StreamName, text: str) -> None:
    for start in range(0, len(text), COMMAND_OUTPUT_MAX):
        data: CommandOutputData = {
            "command_id": command_id,
            "stream": stream,
            "text": text[start : start + COMMAND_OUTPUT_MAX],
        }
        _bus.publish_nowait("command_output", data)


async def emit_command_output(command_id: int, stream: StreamName, text: str) -> None:
    """Publish output directly, split into events of at most 16 KB.

    Prefer CommandOutputBatcher for live process output.
    """
    for start in range(0, len(text), COMMAND_OUTPUT_MAX):
        data: CommandOutputData = {
            "command_id": command_id,
            "stream": stream,
            "text": text[start : start + COMMAND_OUTPUT_MAX],
        }
        await _bus.publish("command_output", data)


async def emit_command_finished(
    command_id: int, exit_code: int | None, duration_ms: int, killed: bool, timed_out: bool
) -> None:
    data: CommandFinishedData = {
        "command_id": command_id,
        "exit_code": exit_code,
        "duration_ms": duration_ms,
        "killed": killed,
        "timed_out": timed_out,
    }
    await _bus.publish("command_finished", data)


async def emit_permission_request(
    permission_id: int, command: str, reason: str, danger_reason: str
) -> None:
    data: PermissionRequestData = {
        "permission_id": permission_id,
        "command": command,
        "reason": reason,
        "danger_reason": danger_reason,
    }
    await _bus.publish("permission_request", data)


async def emit_permission_resolved(permission_id: int, approved: bool) -> None:
    data: PermissionResolvedData = {"permission_id": permission_id, "approved": approved}
    await _bus.publish("permission_resolved", data)


async def emit_verdict(
    prompt_id: int,
    round: int,
    status: VerdictStatus,
    summary: str,
    bugs: list[Any],
    fix_instructions: list[Any],
) -> None:
    data: VerdictData = {
        "prompt_id": prompt_id,
        "round": round,
        "status": status,
        "summary": summary,
        "bugs": bugs,
        "fix_instructions": fix_instructions,
    }
    await _bus.publish("verdict", data)


async def emit_prompt_done(prompt_id: int, rounds_used: int) -> None:
    data: PromptDoneData = {"prompt_id": prompt_id, "rounds_used": rounds_used}
    await _bus.publish("prompt_done", data)


async def emit_needs_user(prompt_id: int, rounds_used: int, summary: str, bugs: list[Any]) -> None:
    data: NeedsUserData = {
        "prompt_id": prompt_id,
        "rounds_used": rounds_used,
        "summary": summary,
        "bugs": bugs,
    }
    await _bus.publish("needs_user", data)


async def emit_manual_fix_applied(
    prompt_id: int, added: list[str], removed: list[str], modified: list[str]
) -> None:
    data: ManualFixAppliedData = {
        "prompt_id": prompt_id,
        "added": added,
        "removed": removed,
        "modified": modified,
    }
    await _bus.publish("manual_fix_applied", data)


async def emit_queue_empty() -> None:
    data: QueueEmptyData = {}
    await _bus.publish("queue_empty", data)


async def emit_error(
    code: str, message: str, details: dict[str, Any] | list[Any] | None = None
) -> None:
    data: ErrorData = {"code": code, "message": message, "details": details}
    await _bus.publish("error", data)


# --- Batching -----------------------------------------------------------------

K = TypeVar("K", bound=Hashable)


class _Batcher(Generic[K]):
    """Buffers text per key; flushes after `interval` seconds or once `max_size` chars are buffered.

    Must be used from inside the running event loop.
    """

    def __init__(self, interval: float, max_size: int, emit: Callable[[K, str], None]) -> None:
        self._interval = interval
        self._max_size = max_size
        self._emit = emit
        self._buffers: dict[K, list[str]] = {}
        self._sizes: dict[K, int] = {}
        self._timers: dict[K, asyncio.TimerHandle] = {}

    def _add(self, key: K, text: str) -> None:
        if not text:
            return
        self._buffers.setdefault(key, []).append(text)
        self._sizes[key] = self._sizes.get(key, 0) + len(text)
        if self._sizes[key] >= self._max_size:
            self._flush_key(key)
        elif key not in self._timers:
            loop = asyncio.get_running_loop()
            self._timers[key] = loop.call_later(self._interval, self._flush_key, key)

    def _flush_key(self, key: K) -> None:
        timer = self._timers.pop(key, None)
        if timer is not None:
            timer.cancel()
        parts = self._buffers.pop(key, None)
        self._sizes.pop(key, None)
        if parts:
            self._emit(key, "".join(parts))

    def flush_all(self) -> None:
        for key in list(self._buffers):
            self._flush_key(key)

    def has_pending(self) -> bool:
        return bool(self._buffers)


class TokenBatcher(_Batcher[int]):
    """Batches LLM tokens per message_id: one token event every 50 ms or 200 chars.

    Call flush(message_id) before emit_message_end so no text is lost.
    """

    def __init__(
        self, interval: float = TOKEN_FLUSH_INTERVAL, max_size: int = TOKEN_FLUSH_SIZE
    ) -> None:
        super().__init__(interval, max_size, self._publish)

    @staticmethod
    def _publish(message_id: int, text: str) -> None:
        data: TokenData = {"message_id": message_id, "text": text}
        _bus.publish_nowait("token", data)

    def add(self, message_id: int, text: str) -> None:
        self._add(message_id, text)

    def flush(self, message_id: int) -> None:
        self._flush_key(message_id)


class CommandOutputBatcher(_Batcher[tuple[int, StreamName]]):
    """Batches process output per (command_id, stream): every 100 ms or 4 KB.

    Events are capped at 16 KB; larger text is split. Call flush(command_id)
    before emit_command_finished. Feed it pipe-sized reads (e.g. 64 KB) with an
    await between reads, so one flush never produces a huge burst of events.
    """

    def __init__(
        self, interval: float = COMMAND_FLUSH_INTERVAL, max_size: int = COMMAND_FLUSH_SIZE
    ) -> None:
        super().__init__(interval, max_size, self._publish)

    @staticmethod
    def _publish(key: tuple[int, StreamName], text: str) -> None:
        _publish_command_output(key[0], key[1], text)

    def add(self, command_id: int, stream: StreamName, text: str) -> None:
        self._add((command_id, stream), text)

    def flush(self, command_id: int) -> None:
        self._flush_key((command_id, "stdout"))
        self._flush_key((command_id, "stderr"))
