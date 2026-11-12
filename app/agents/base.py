"""The agent tool-calling loop shared by the Boss and the Worker.

One call to Agent.run_turn() is one step of the work loop (plan, test, do a task, fix):

1. A message row is created and message_start is emitted.
2. The LLM streams; tokens go out through a TokenBatcher, tool calls are collected.
3. Tokens are flushed, the assistant message is saved, message_end is emitted.
4. Normal tool calls run through execute_tool(); results are added to the history and saved.
5. A terminal tool call (assign_task, submit_verdict, finish_task) ends the turn with
   its validated arguments. Invalid terminal arguments go back to the model as an error.
6. A reply with no tool call ends the turn only when allow_no_tool_end is set;
   otherwise the model gets a reminder (at most 3, then AgentError).

Every assistant message's tool calls always get a matching tool result in the history,
even on errors or cancellation, because providers reject broken call/result pairs.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import litellm
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.config import get_settings
from app.core.events import (
    TokenBatcher,
    emit_message_end,
    emit_message_start,
    emit_tool_call,
    emit_tool_result,
)
from app.core.permissions import PermissionManager
from app.db.database import SessionLocal
from app.db.models import AgentName as AgentNameEnum
from app.db.models import ApiProfile, LlmRole, Message, MessageKind
from app.llm import client as llm
from app.tools import (
    TERMINAL_TOOL_NAMES,
    ToolContext,
    ToolOutput,
    execute_tool,
    validate_tool_args,
)

logger = logging.getLogger(__name__)

AgentName = Literal["boss", "worker"]
TurnKind = Literal["task", "report", "verdict", "thinking", "system"]

RETRYABLE_KINDS = frozenset({"rate_limit", "timeout", "connection", "unavailable"})
RETRY_DELAYS: tuple[float, ...] = (2, 5, 10)
MAX_REMINDERS = 3
TOOL_LIMIT_GRACE = 10  # extra tool calls allowed after the "finish now" reminder
TRUNCATE_KEEP = 500  # characters kept at each end of a shortened tool result
TRUNCATE_MIN = 2 * TRUNCATE_KEEP + 200  # shorter results are left alone
CANCELLED_RESULT = "Cancelled: the run was stopped before this tool finished."

TERMINAL_ACK = {
    "assign_task": "Task sent to the Worker.",
    "submit_verdict": "Verdict recorded.",
    "finish_task": "Report sent to the Boss.",
}


class AgentError(Exception):
    """The agent could not finish its turn (limits reached, no terminal call)."""


@dataclass
class AgentContext:
    """Where and for what an agent works. The orchestrator updates run_id/round each round."""

    project_root: Path
    prompt_id: int
    run_id: int | None = None
    round: int = 1
    max_rounds: int = 6
    permissions: PermissionManager | None = None
    before_file_change: Callable[[Path], Awaitable[None]] | None = None


class TurnResult(BaseModel):
    terminal_name: str | None = None  # None when the turn ended with text only
    args: dict[str, Any] | None = None  # validated terminal arguments
    text: str = ""  # assistant text of the last message
    message_ids: list[int] = Field(default_factory=list)


@dataclass
class _Call:
    id: str
    name: str
    args: dict[str, Any]
    raw: str | None = None  # set when the model sent invalid JSON


@dataclass
class _TurnState:
    terminal_tools: frozenset[str]
    normal_allowed: frozenset[str]
    tool_calls: int = 0
    llm_calls: int = 0
    reminders: int = 0
    limit_warned: bool = False
    message_ids: list[int] = field(default_factory=list)


class Agent:
    def __init__(
        self,
        agent_name: AgentName,
        profile: ApiProfile | llm.ModelConfig,
        system_prompt: str | Callable[[], str],
        tools: Sequence[dict[str, Any]],
        context: AgentContext,
    ) -> None:
        self.name: AgentName = agent_name
        # Decrypt once; the ORM object may be detached from its session later.
        self.model = profile if isinstance(profile, llm.ModelConfig) else llm.config_from_profile(profile)
        self._system_prompt = system_prompt
        # The full tool list is always sent (stable for prompt caching, and some providers
        # reject history that mentions tools missing from the request). Per-turn limits
        # on which tools may run are enforced by the loop.
        self.tools = list(tools)
        self.tool_names = frozenset(t["function"]["name"] for t in self.tools)
        self.context = context
        self.history: list[dict[str, Any]] = []
        self._batcher = TokenBatcher()
        self._call_ids: set[str] = set()
        self._context_window: int | None = None

    # --- history ---------------------------------------------------------------------

    def system_prompt(self) -> str:
        return self._system_prompt() if callable(self._system_prompt) else self._system_prompt

    def reset(self) -> None:
        """Start a fresh conversation (new prompt)."""
        self.history = []
        self._call_ids.clear()

    def _refresh_system_prompt(self) -> None:
        message = {"role": "system", "content": self.system_prompt()}
        if self.history and self.history[0].get("role") == "system":
            self.history[0] = message
        else:
            self.history.insert(0, message)

    def _save(
        self,
        role: LlmRole,
        kind: MessageKind,
        content: str = "",
        tool_calls: list[dict[str, Any]] | None = None,
        tool_call_id: str | None = None,
        tool_name: str | None = None,
    ) -> int:
        ctx = self.context
        with SessionLocal() as db:
            row = Message(
                run_id=ctx.run_id, prompt_id=ctx.prompt_id, round=ctx.round,
                agent=AgentNameEnum(self.name), role=role, kind=kind, content=content,
                tool_calls=tool_calls, tool_call_id=tool_call_id, tool_name=tool_name,
            )
            db.add(row)
            db.commit()
            return row.id

    @staticmethod
    def _update(message_id: int, content: str, tool_calls: list[dict[str, Any]] | None) -> None:
        with SessionLocal() as db:
            row = db.get(Message, message_id)
            if row is not None:
                row.content = content
                row.tool_calls = tool_calls
                db.commit()

    def _add_user(self, text: str) -> None:
        self.history.append({"role": "user", "content": text})
        self._save(LlmRole.USER, MessageKind.SYSTEM, text)

    def _add_tool_result(self, call: _Call, content: str) -> None:
        self.history.append({"role": "tool", "tool_call_id": call.id, "name": call.name, "content": content})
        self._save(LlmRole.TOOL, MessageKind.TOOL, content, tool_call_id=call.id, tool_name=call.name)

    def _close_open_calls(self, calls: Iterable[_Call], content: str = CANCELLED_RESULT) -> None:
        """Give every call that has no result yet a placeholder result (keeps pairs intact)."""
        answered = {m.get("tool_call_id") for m in self.history if m.get("role") == "tool"}
        for call in calls:
            if call.id not in answered:
                self._add_tool_result(call, content)

    # --- context size ----------------------------------------------------------------

    def _window(self) -> int:
        if self._context_window is None:
            default = get_settings().default_context_window
            try:
                info = litellm.get_model_info(model=llm.build_model_params(self.model)["model"])
                self._context_window = int(info.get("max_input_tokens") or info.get("max_tokens") or default)
            except Exception:  # noqa: BLE001 - unknown / custom models
                self._context_window = default
        return self._context_window

    def _count_tokens(self) -> int:
        try:
            model = llm.build_model_params(self.model)["model"]
            return int(litellm.token_counter(model=model, messages=self.history))
        except Exception:  # noqa: BLE001 - fall back to a rough estimate
            return sum(len(json.dumps(m, ensure_ascii=False)) for m in self.history) // 4

    def fit_context(self) -> int:
        """Shorten old tool results until the history fits the budget. Returns the token count.

        The system prompt, user messages and assistant messages are never changed or
        removed, and no message is dropped, so tool-call / tool-result pairs stay intact.
        """
        budget = int(self._window() * get_settings().context_threshold)
        tokens = self._count_tokens()
        if tokens <= budget:
            return tokens
        for message in self.history:  # oldest first
            if message.get("role") != "tool":
                continue
            content = message.get("content") or ""
            if len(content) <= TRUNCATE_MIN:
                continue
            removed = len(content) - 2 * TRUNCATE_KEEP
            message["content"] = (
                content[:TRUNCATE_KEEP] + f"\n...[truncated {removed} characters]...\n" + content[-TRUNCATE_KEEP:]
            )
            tokens -= removed // 4
            if tokens <= budget:
                break
        tokens = self._count_tokens()
        if tokens > budget:
            logger.warning("%s history is still %d tokens (budget %d) after shortening", self.name, tokens, budget)
        return tokens

    # --- one LLM call ----------------------------------------------------------------

    async def _start_message(self, kind: TurnKind) -> int:
        message_id = self._save(LlmRole.ASSISTANT, MessageKind(kind))
        await emit_message_start(message_id, self.context.prompt_id, self.context.round, self.name, kind)
        return message_id

    async def _end_message(self, message_id: int, content: str, tool_calls: list[dict[str, Any]] | None) -> None:
        self._batcher.flush(message_id)
        self._update(message_id, content, tool_calls)
        await emit_message_end(message_id, len(content))

    def _normalize_call(self, event: dict[str, Any]) -> _Call:
        call_id = str(event.get("id") or "")
        if not call_id or call_id in self._call_ids or call_id.startswith("call_") and call_id[5:].isdigit():
            call_id = f"call_{uuid.uuid4().hex[:24]}"  # provider gave none, or a reused one
        self._call_ids.add(call_id)
        raw = event.get("raw_arguments")
        arguments = event.get("arguments")
        args: dict[str, Any] = arguments if isinstance(arguments, dict) else {}
        return _Call(call_id, str(event.get("name") or ""), args, raw if isinstance(raw, str) else None)

    async def _call_llm(self, kind: TurnKind, state: _TurnState, force_tools: bool = False) -> tuple[str, list[_Call], int]:
        """Stream one assistant message (with retries). Returns (text, tool calls, message id)."""
        message_id = await self._start_message(kind)
        state.message_ids.append(message_id)
        parts: list[str] = []
        events: list[dict[str, Any]] = []
        attempt = 0
        open_message = True
        try:
            while True:
                parts, events = [], []
                try:
                    async for event in llm.stream_chat(
                        self.model, self.history, tools=self.tools or None,
                        tool_choice="required" if force_tools and self.tools else "auto",
                    ):
                        if event["type"] == "token":
                            parts.append(event["text"])
                            self._batcher.add(message_id, event["text"])
                        elif event["type"] == "tool_call":
                            events.append(dict(event))
                    break
                except llm.ProviderError as exc:
                    partial = "".join(parts)
                    retry = exc.kind in RETRYABLE_KINDS and attempt < len(RETRY_DELAYS)
                    if not retry:
                        open_message = False
                        await self._end_message(message_id, partial, None)
                        raise
                    logger.warning(
                        "%s: provider error (%s), retry %d in %ss", self.name, exc.kind, attempt + 1,
                        RETRY_DELAYS[attempt],
                    )
                    if partial:
                        # Text already reached the UI: close this message and retry in a new one.
                        open_message = False
                        await self._end_message(message_id, partial + "\n[interrupted, retrying]", None)
                    await asyncio.sleep(RETRY_DELAYS[attempt])
                    attempt += 1
                    if not open_message:
                        message_id = await self._start_message(kind)
                        state.message_ids.append(message_id)
                        open_message = True
        except BaseException:  # cancel or an unexpected error: never leave a message open in the UI
            if open_message:
                await self._end_message(message_id, "".join(parts), None)
            raise

        text = "".join(parts)
        calls = [self._normalize_call(e) for e in events]
        tool_calls = [
            {"id": c.id, "type": "function", "function": {"name": c.name, "arguments": json.dumps(c.args)}}
            for c in calls
        ]
        await self._end_message(message_id, text, tool_calls or None)
        assistant: dict[str, Any] = {"role": "assistant", "content": text or None}
        if tool_calls:
            assistant["tool_calls"] = tool_calls
        self.history.append(assistant)
        return text, calls, message_id

    # --- tools -----------------------------------------------------------------------

    def _tool_context(self, message_id: int) -> ToolContext:
        ctx = self.context
        return ToolContext(
            project_root=ctx.project_root, run_id=ctx.run_id, message_id=message_id,
            permissions=ctx.permissions, before_file_change=ctx.before_file_change,
        )

    def _finish_hint(self, state: _TurnState) -> str:
        if not state.terminal_tools:
            return "Answer in plain text without calling tools."
        return "Finish by calling " + " or ".join(sorted(state.terminal_tools)) + "."

    async def _run_normal(self, call: _Call, state: _TurnState, message_id: int) -> None:
        limit = self._tool_limit()
        if state.tool_calls >= limit + TOOL_LIMIT_GRACE:
            output = ToolOutput(False, "Error: tool call limit reached. " + self._finish_hint(state), "limit reached")
            await emit_tool_call(call.id, message_id, self.name, call.name, call.args)
            await emit_tool_result(call.id, self.name, call.name, False, output.summary)
        elif call.name not in state.normal_allowed:
            available = ", ".join(sorted(state.normal_allowed | state.terminal_tools)) or "none"
            output = ToolOutput(
                False,
                f"Error: {call.name} is not available in this step. Available now: {available}. "
                + self._finish_hint(state),
                f"{call.name} not available now",
            )
            await emit_tool_call(call.id, message_id, self.name, call.name, call.args)
            await emit_tool_result(call.id, self.name, call.name, False, output.summary)
        else:
            arguments: dict[str, Any] | str = call.raw if call.raw is not None else call.args
            output = await execute_tool(self.name, call.name, arguments, self._tool_context(message_id), call.id)
        state.tool_calls += 1
        self._add_tool_result(call, output.content)

    async def _handle_terminal(
        self, calls: list[_Call], state: _TurnState, text: str, message_id: int
    ) -> TurnResult | None:
        result: TurnResult | None = None
        for call in calls:
            await emit_tool_call(call.id, message_id, self.name, call.name, call.args)
            if result is not None:
                self._add_tool_result(call, "Ignored: only one finishing call is used per turn.")
                await emit_tool_result(call.id, self.name, call.name, False, "ignored")
                continue
            if call.name not in state.terminal_tools:
                hint = self._finish_hint(state)
                self._add_tool_result(call, f"Error: {call.name} can't be used in this step. {hint}")
                await emit_tool_result(call.id, self.name, call.name, False, f"{call.name} not available now")
                continue
            parsed, problem = validate_tool_args(call.name, call.raw if call.raw is not None else call.args)
            if parsed is None:
                self._add_tool_result(call, f"Error: {problem} Fix the arguments and call {call.name} again.")
                await emit_tool_result(call.id, self.name, call.name, False, f"invalid arguments: {problem}"[:200])
                continue
            self._add_tool_result(call, TERMINAL_ACK.get(call.name, "Done."))
            await emit_tool_result(call.id, self.name, call.name, True, TERMINAL_ACK.get(call.name, "Done."))
            result = TurnResult(
                terminal_name=call.name, args=parsed.model_dump(), text=text, message_ids=list(state.message_ids)
            )
        return result

    def _tool_limit(self) -> int:
        settings = get_settings()
        return settings.boss_max_tool_calls_per_turn if self.name == "boss" else settings.worker_max_tool_calls_per_turn

    # --- the turn ----------------------------------------------------------------------

    async def run_turn(
        self,
        user_message: str,
        kind: TurnKind,
        terminal_tools: Iterable[str],
        allow_no_tool_end: bool = False,
        allowed_tools: Iterable[str] | None = None,
    ) -> TurnResult:
        """Run one step until a terminal tool is called (or text, if allow_no_tool_end).

        allowed_tools limits which normal tools may run in this step (default: all of the
        agent's tools). Raises AgentError, llm.ProviderError or asyncio.CancelledError.
        """
        terminal = frozenset(terminal_tools)
        unknown = terminal - self.tool_names
        if unknown:
            raise ValueError(f"{self.name} has no terminal tool(s): {sorted(unknown)}")
        normal = frozenset(allowed_tools) if allowed_tools is not None else self.tool_names
        state = _TurnState(terminal_tools=terminal, normal_allowed=normal - TERMINAL_TOOL_NAMES)
        settings = get_settings()

        self._refresh_system_prompt()
        self._add_user(user_message)

        while True:
            if state.llm_calls >= settings.max_llm_calls_per_turn:
                raise AgentError(f"{self.name} made {state.llm_calls} LLM calls without finishing.")
            # Token counting over a long history is CPU-heavy: keep it off the event loop.
            await asyncio.to_thread(self.fit_context)
            state.llm_calls += 1
            # After a reply without the needed tool call, require one: some models (e.g. Qwen via
            # OpenAI-compatible APIs) answer with empty text under "auto" but call tools when required.
            force = state.reminders > 0 and not allow_no_tool_end
            text, calls, message_id = await self._call_llm(kind, state, force_tools=force)

            if not calls:
                if allow_no_tool_end:
                    return TurnResult(text=text, message_ids=list(state.message_ids))
                state.reminders += 1
                if state.reminders > MAX_REMINDERS:
                    raise AgentError(f"{self.name} kept answering without calling a finishing tool.")
                self._add_user("You must call " + " or ".join(sorted(terminal)) + " to finish.")
                continue

            terminal_calls = [c for c in calls if c.name in TERMINAL_TOOL_NAMES]
            normal_calls = [c for c in calls if c.name not in TERMINAL_TOOL_NAMES]
            try:
                for call in normal_calls:  # normal tools first, then the terminal one
                    await self._run_normal(call, state, message_id)
                result = await self._handle_terminal(terminal_calls, state, text, message_id)
            except BaseException:  # cancel or any error: keep every call paired with a result
                try:
                    self._close_open_calls(calls)
                except Exception:
                    logger.exception("Could not close open tool calls")
                raise
            if result is not None:
                return result

            limit = self._tool_limit()
            if state.tool_calls > limit + TOOL_LIMIT_GRACE:
                raise AgentError(f"{self.name} used more than {limit + TOOL_LIMIT_GRACE} tool calls in one turn.")
            if state.tool_calls >= limit and not state.limit_warned:
                state.limit_warned = True
                self._add_user(
                    f"You have used {state.tool_calls} tool calls in this step (limit {limit}). "
                    + self._finish_hint(state) + " Stop exploring and finish now."
                )
            if allow_no_tool_end and not terminal and not state.normal_allowed and calls:
                # Tools are disabled in this step and the model still called them; it was told why.
                state.reminders += 1
                if state.reminders > MAX_REMINDERS:
                    raise AgentError(f"{self.name} kept calling tools in a text-only step.")


# --- Rebuilding a history from the database ------------------------------------------


def history_from_rows(rows: Iterable[Message], system_prompt: str | None = None) -> list[dict[str, Any]]:
    """LLM messages from saved rows (ordered by id). The system prompt is not stored; pass it in."""
    history: list[dict[str, Any]] = []
    if system_prompt is not None:
        history.append({"role": "system", "content": system_prompt})
    for row in rows:
        if row.role == LlmRole.USER:
            history.append({"role": "user", "content": row.content})
        elif row.role == LlmRole.ASSISTANT:
            message: dict[str, Any] = {"role": "assistant", "content": row.content or None}
            if row.tool_calls:
                message["tool_calls"] = row.tool_calls
            history.append(message)
        elif row.role == LlmRole.TOOL:
            history.append(
                {"role": "tool", "tool_call_id": row.tool_call_id, "name": row.tool_name, "content": row.content}
            )
        elif row.role == LlmRole.SYSTEM:
            history.append({"role": "system", "content": row.content})
    return history


def load_history(prompt_id: int, agent: AgentName, system_prompt: str | None = None) -> list[dict[str, Any]]:
    with SessionLocal() as db:
        rows = db.scalars(
            select(Message)
            .where(Message.prompt_id == prompt_id, Message.agent == AgentNameEnum(agent))
            .order_by(Message.id)
        ).all()
        return history_from_rows(rows, system_prompt)
