"""Tool registry for the agents.

- TOOL_SCHEMAS: OpenAI-style function schemas, generated from the argument models below.
- BOSS_TOOLS / WORKER_TOOLS: the schemas each agent is given.
- execute_tool(): checks access, validates arguments, runs the tool, emits
  tool_call / tool_result, and always returns a ToolOutput (never raises,
  except for cancellation when a run is stopped).
- Terminal tools (assign_task, submit_verdict, finish_task) end an agent's turn.
  execute_tool refuses them; the agent loop validates them with validate_tool_args().

Access rules:
- Worker: list, read, write, edit and delete files inside the project. Nothing else.
- Boss: list and read files, and run shell commands (dangerous ones need approval).
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.agents.schemas import Task, Verdict, WorkerReport
from app.core.events import emit_tool_call, emit_tool_result
from app.core.permissions import PermissionManager
from app.tools.command_tools import run_command
from app.tools.file_tools import (
    ToolError,
    ToolOutput,
    delete_file,
    edit_file,
    list_files,
    read_file,
    resolve_safe_path,
    write_file,
)

logger = logging.getLogger(__name__)

AgentName = Literal["boss", "worker"]

EVENT_ARG_MAX_CHARS = 4000  # long string arguments (file contents) are cut in tool_call events

__all__ = [
    "BOSS_TOOLS",
    "TERMINAL_TOOL_NAMES",
    "TOOL_SCHEMAS",
    "WORKER_TOOLS",
    "ToolContext",
    "ToolError",
    "ToolOutput",
    "execute_tool",
    "validate_tool_args",
]


# --- Argument models (single source for schema and validation) ------------------


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ListFilesArgs(_Args):
    path: str = Field(".", description="Folder relative to the project root. '.' is the project root.")
    recursive: bool = Field(False, description="Also list everything inside subfolders.")
    max_entries: int = Field(500, ge=1, le=5000, description="Stop after this many entries.")


class ReadFileArgs(_Args):
    path: str = Field(description="File path relative to the project root, e.g. 'src/app.py'.")
    start_line: int | None = Field(None, ge=1, description="First line to read (1-based). Optional.")
    end_line: int | None = Field(None, ge=1, description="Last line to read (inclusive). Optional.")


class WriteFileArgs(_Args):
    path: str = Field(description="File path relative to the project root. Parent folders are created.")
    content: str = Field(description="The complete new content of the file.")


class EditFileArgs(_Args):
    path: str = Field(description="File path relative to the project root.")
    old_text: str = Field(
        description="Exact text to replace, copied from the file including indentation. "
        "Must appear exactly once; include surrounding lines if needed to make it unique."
    )
    new_text: str = Field(description="Text that replaces old_text. Use an empty string to delete it.")


class DeleteFileArgs(_Args):
    path: str = Field(description="File path relative to the project root. Folders can't be deleted.")


class RunCommandArgs(_Args):
    command: str = Field(min_length=1, description="The shell command to run.")
    reason: str = Field(min_length=1, description="Why you are running it, in one short sentence (shown to the user).")
    cwd: str | None = Field(
        None, description="Working folder. Default: the project root. Relative paths are relative to the project."
    )
    timeout: int = Field(120, ge=1, le=600, description="Seconds before the command is killed (max 600).")


@dataclass(frozen=True)
class _ToolSpec:
    name: str
    description: str
    args: type[BaseModel]
    terminal: bool = False  # ends the agent's turn; handled by the agent loop, not execute_tool


_SPECS: dict[str, _ToolSpec] = {
    s.name: s
    for s in (
        _ToolSpec(
            "list_files",
            "List files and folders in the project. Build folders, .git, node_modules, virtualenvs and "
            "editor folders are skipped. Use recursive=true to see the whole tree.",
            ListFilesArgs,
        ),
        _ToolSpec(
            "read_file",
            "Read a text file from the project. Returns the content with line numbers and the total line "
            "count. Files over 1 MB need start_line/end_line. Binary files can't be read.",
            ReadFileArgs,
        ),
        _ToolSpec(
            "write_file",
            "Create a file or replace its whole content. Use edit_file for small changes to an existing "
            "file. Max 1 MB. Writing inside .git is not allowed.",
            WriteFileArgs,
        ),
        _ToolSpec(
            "edit_file",
            "Replace one exact piece of text in a file. old_text must appear exactly once in the file; "
            "if it is not found or appears more than once, nothing changes and you get an error.",
            EditFileArgs,
        ),
        _ToolSpec(
            "delete_file",
            "Delete one file from the project (for example when moving code during a refactor). "
            "Folders and files inside .git can't be deleted.",
            DeleteFileArgs,
        ),
        _ToolSpec(
            "run_command",
            "Run a shell command on the user's computer, e.g. to install dependencies, build, run tests "
            "or start the app. Output is returned (last 20 000 characters per stream). Stdin is closed, "
            "so don't run commands that wait for input. Dangerous commands (deleting outside the project, "
            "system changes, global installs, git push, ...) need the user's approval; if the user "
            "denies one, continue without it.",
            RunCommandArgs,
        ),
        _ToolSpec(
            "assign_task",
            "Send a task to the Worker. This ends your planning step. Give a complete goal, every file to "
            "create or change, and acceptance criteria you will test against.",
            Task,
            terminal=True,
        ),
        _ToolSpec(
            "submit_verdict",
            "Give your final decision for this round after testing the whole project. This ends your "
            "testing step. When rejecting, list every bug (location, description, how to reproduce) and "
            "concrete fix instructions.",
            Verdict,
            terminal=True,
        ),
        _ToolSpec(
            "finish_task",
            "Report to the Boss that you are done. This ends your turn. List every file you created, "
            "changed or deleted.",
            WorkerReport,
            terminal=True,
        ),
    )
}

TERMINAL_TOOL_NAMES: frozenset[str] = frozenset(n for n, s in _SPECS.items() if s.terminal)


def _clean_schema(node: Any) -> Any:
    """Make pydantic's JSON schema compact and model-friendly: no titles, no nullable anyOf."""
    if isinstance(node, dict):
        out: dict[str, Any] = {}
        for key, value in node.items():
            if key == "title":
                continue
            out[key] = _clean_schema(value)
        any_of = out.get("anyOf")
        if isinstance(any_of, list):
            non_null = [s for s in any_of if s.get("type") != "null"]
            if len(non_null) == 1 and len(non_null) < len(any_of):
                out.pop("anyOf")
                out = {**non_null[0], **out}
        if out.get("default", ...) is None:
            out.pop("default")
        return out
    if isinstance(node, list):
        return [_clean_schema(v) for v in node]
    return node


def _inline_refs(node: Any, defs: dict[str, Any]) -> Any:
    """Replace {"$ref": "#/$defs/X"} with the definition; some providers don't support $ref."""
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/$defs/"):
            merged = {**defs[ref.split("/")[-1]], **{k: v for k, v in node.items() if k != "$ref"}}
            return _inline_refs(merged, defs)
        return {k: _inline_refs(v, defs) for k, v in node.items()}
    if isinstance(node, list):
        return [_inline_refs(v, defs) for v in node]
    return node


def _schema(spec: _ToolSpec) -> dict[str, Any]:
    raw = spec.args.model_json_schema()
    defs = raw.pop("$defs", {})
    raw.pop("description", None)  # the class docstring; the tool description is what the model reads
    return {
        "type": "function",
        "function": {
            "name": spec.name,
            "description": spec.description,
            "parameters": _clean_schema(_inline_refs(raw, defs)),
        },
    }


TOOL_SCHEMAS: dict[str, dict[str, Any]] = {name: _schema(spec) for name, spec in _SPECS.items()}

BOSS_TOOL_NAMES: tuple[str, ...] = ("list_files", "read_file", "run_command", "assign_task", "submit_verdict")
WORKER_TOOL_NAMES: tuple[str, ...] = (
    "list_files", "read_file", "write_file", "edit_file", "delete_file", "finish_task",
)
_ALLOWED: dict[str, frozenset[str]] = {"boss": frozenset(BOSS_TOOL_NAMES), "worker": frozenset(WORKER_TOOL_NAMES)}

BOSS_TOOLS: list[dict[str, Any]] = [TOOL_SCHEMAS[n] for n in BOSS_TOOL_NAMES]
WORKER_TOOLS: list[dict[str, Any]] = [TOOL_SCHEMAS[n] for n in WORKER_TOOL_NAMES]


# --- Execution ------------------------------------------------------------------


@dataclass
class ToolContext:
    project_root: Path
    run_id: int | None = None
    message_id: int | None = None
    permissions: PermissionManager | None = None
    # Called with the absolute path before a file is written, edited or deleted (snapshots, step 7+).
    before_file_change: Callable[[Path], Awaitable[None]] | None = None


def _error(name: str, message: str) -> ToolOutput:
    summary = f"{name} failed: {message}"
    return ToolOutput(False, f"Error: {message}", summary if len(summary) <= 200 else summary[:197] + "...")


def _event_arguments(arguments: Any) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        return {"_raw": str(arguments)[:EVENT_ARG_MAX_CHARS]}
    out: dict[str, Any] = {}
    for key, value in arguments.items():
        if isinstance(value, str) and len(value) > EVENT_ARG_MAX_CHARS:
            value = value[:EVENT_ARG_MAX_CHARS] + f"... [{len(value) - EVENT_ARG_MAX_CHARS} more characters]"
        out[key] = value
    return out


def _validation_message(exc: ValidationError) -> str:
    problems = []
    for err in exc.errors():
        field = ".".join(str(p) for p in err["loc"]) or "arguments"
        problems.append(f"{field}: {err['msg']}")
    return "Invalid arguments: " + "; ".join(problems)


def validate_tool_args(name: str, arguments: Any) -> tuple[BaseModel | None, str | None]:
    """Parse and validate arguments for a tool. Returns (model, None) or (None, error message)."""
    spec = _SPECS.get(name)
    if spec is None:
        return None, f"unknown tool '{name}'."
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments) if arguments.strip() else {}
        except json.JSONDecodeError:
            return None, "arguments are not valid JSON."
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        return None, "arguments must be a JSON object."
    try:
        return spec.args.model_validate(arguments), None
    except ValidationError as exc:
        return None, _validation_message(exc)


async def _dispatch(agent: str, name: str, arguments: Any, ctx: ToolContext) -> ToolOutput:
    if agent not in _ALLOWED:
        return _error(name, f"unknown agent '{agent}'.")
    if name not in _SPECS:
        return _error(name, f"unknown tool '{name}'. Available: {', '.join(sorted(_ALLOWED[agent]))}.")
    if name not in _ALLOWED[agent]:
        return _error(
            name, f"the {agent} is not allowed to use {name}. Available: {', '.join(sorted(_ALLOWED[agent]))}."
        )
    if name in TERMINAL_TOOL_NAMES:
        return _error(name, f"{name} ends the agent's turn and is handled by the agent loop.")

    args, problem = validate_tool_args(name, arguments)
    if args is None:
        return _error(name, problem or "invalid arguments.")

    root = ctx.project_root
    if isinstance(args, ListFilesArgs):
        return await asyncio.to_thread(list_files, root, args.path, args.recursive, args.max_entries)
    if isinstance(args, ReadFileArgs):
        return await asyncio.to_thread(read_file, root, args.path, args.start_line, args.end_line)
    if isinstance(args, WriteFileArgs | EditFileArgs | DeleteFileArgs):
        if ctx.before_file_change is not None:
            await ctx.before_file_change(resolve_safe_path(root, args.path, for_write=True))
        if isinstance(args, WriteFileArgs):
            return await asyncio.to_thread(write_file, root, args.path, args.content)
        if isinstance(args, EditFileArgs):
            return await asyncio.to_thread(edit_file, root, args.path, args.old_text, args.new_text)
        return await asyncio.to_thread(delete_file, root, args.path)
    if isinstance(args, RunCommandArgs):
        result = await run_command(
            args.command, args.reason, args.cwd, args.timeout,
            project_root=root, run_id=ctx.run_id, message_id=ctx.message_id, permissions=ctx.permissions,
        )
        return ToolOutput(result.ok, result.to_model_text(), result.summary())
    return _error(name, "tool is not implemented.")


async def execute_tool(
    agent: AgentName | str,
    name: str,
    arguments: dict[str, Any] | str | None,
    context: ToolContext,
    call_id: str | None = None,
) -> ToolOutput:
    """Run one tool call for an agent. Emits tool_call and tool_result. Never raises (except cancel)."""
    call_id = call_id or f"call_{uuid.uuid4().hex[:12]}"
    event_agent: Literal["boss", "worker"] = "boss" if agent == "boss" else "worker"
    await emit_tool_call(call_id, context.message_id, event_agent, name, _event_arguments(arguments))
    try:
        output = await _dispatch(str(agent), name, arguments, context)
    except ToolError as exc:
        output = _error(name, str(exc))
    except asyncio.CancelledError:
        await emit_tool_result(call_id, event_agent, name, False, f"{name} stopped")
        raise
    except Exception as exc:
        logger.exception("Tool %s failed unexpectedly", name)
        output = _error(name, f"internal error ({type(exc).__name__}). Try a different approach.")
    await emit_tool_result(call_id, event_agent, name, output.ok, output.summary)
    return output
