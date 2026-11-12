"""run_command: the Boss's shell tool.

Flow: classify the command -> ask permission if dangerous -> start it in the
user's shell -> stream output through the event bus (command_started,
command_output batched, command_finished) -> return a result for the model.

Processes are started with subprocess.Popen and read by background threads,
not asyncio subprocesses: on Windows, uvicorn uses a SelectorEventLoop when
reload is on (WORKLOOP_DEBUG=true), and that loop can't start subprocesses.
The threads hand output to the event loop with call_soon_threadsafe.

Every command runs in its own process group / session so the whole process
tree can be killed on timeout or stop (taskkill /T /F on Windows, killpg elsewhere).
"""

from __future__ import annotations

import asyncio
import base64
import codecs
import logging
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Literal

from sqlalchemy.exc import IntegrityError

from app.config import get_settings
from app.core.events import (
    CommandOutputBatcher,
    emit_command_finished,
    emit_command_started,
)
from app.core.permissions import PermissionManager, get_permission_manager
from app.db.database import SessionLocal, utc_now
from app.db.models import Command, CommandStatus
from app.tools.file_tools import ToolError
from app.tools.safety import classify_command

logger = logging.getLogger(__name__)

OUTPUT_TAIL_CHARS = 20_000  # per stream, returned to the model
READ_CHUNK = 64 * 1024
MAX_COMMAND_LENGTH = 10_000
PIPE_DRAIN_SECONDS = 2.0  # after exit, wait this long for pipes held open by background children
DENIED_MESSAGE = "User denied this command. Continue without it."

IS_WINDOWS = sys.platform == "win32"
ShellName = Literal["pwsh", "powershell", "cmd", "bash", "sh"]


# --- Result ---------------------------------------------------------------------


@dataclass
class CommandResult:
    command_id: int | None
    command: str
    cwd: str
    exit_code: int | None = None
    timed_out: bool = False
    killed: bool = False
    denied: bool = False
    duration_ms: int = 0
    stdout: str = ""
    stderr: str = ""
    stdout_total: int = 0
    stderr_total: int = 0
    timeout: int = 0
    danger_reason: str | None = None

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not (self.timed_out or self.killed or self.denied)

    def to_model_text(self) -> str:
        if self.denied:
            return f"$ {self.command}\n{DENIED_MESSAGE}\n(It was flagged as dangerous: {self.danger_reason})"
        status = [f"exit code {self.exit_code}" if self.exit_code is not None else "no exit code",
                  f"{self.duration_ms} ms"]
        if self.timed_out:
            status.append(f"TIMED OUT after {self.timeout} s (process tree killed)")
        elif self.killed:
            status.append("KILLED (run stopped)")
        lines = [f"$ {self.command}", f"(cwd: {self.cwd})", "Result: " + ", ".join(status)]
        for name, text, total in (("stdout", self.stdout, self.stdout_total),
                                  ("stderr", self.stderr, self.stderr_total)):
            if not text:
                lines.append(f"--- {name}: (empty) ---")
                continue
            lines.append(f"--- {name} ---")
            if total > len(text):
                lines.append(f"[{name} truncated: showing the last {len(text)} of {total} characters]")
            lines.append(text.rstrip("\n"))
        return "\n".join(lines)

    def summary(self) -> str:
        short = self.command if len(self.command) <= 80 else self.command[:77] + "..."
        if self.denied:
            return f"Denied by user: {short}"
        if self.timed_out:
            return f"Timed out after {self.timeout} s: {short}"
        if self.killed:
            return f"Killed: {short}"
        return f"Exit {self.exit_code} in {self.duration_ms / 1000:.1f} s: {short}"


# --- Shell ----------------------------------------------------------------------


def pick_shell(preference: str | None = None) -> tuple[ShellName, str]:
    """(shell name, executable). preference: auto/pwsh/powershell/cmd/bash/sh."""
    pref = (preference or get_settings().shell).lower()
    candidates: list[ShellName]
    if pref != "auto":
        candidates = [pref]  # type: ignore[list-item]
    elif IS_WINDOWS:
        candidates = ["pwsh", "powershell", "cmd"]
    else:
        candidates = ["bash", "sh"]
    for name in candidates:
        if name == "cmd":
            return "cmd", os.environ.get("COMSPEC", "cmd.exe")
        exe = shutil.which(name)
        if exe:
            return name, exe
        if name == "sh" and not IS_WINDOWS:
            return "sh", "/bin/sh"
    raise ToolError(f"No usable shell found (setting: {pref}).")


# The command runs inside a script block piped to Out-Default, so formatted output (tables
# from Get-Location, Get-ChildItem, ...) is written before `exit`. $global:wlOk records
# whether the last statement succeeded; native exit codes come from $LASTEXITCODE.
_PS_TEMPLATE = """$ProgressPreference = 'SilentlyContinue'
try {{ [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 }} catch {{ }}
$OutputEncoding = [System.Text.Encoding]::UTF8
$global:LASTEXITCODE = $null
$global:wlOk = $true
& {{
{command}
$global:wlOk = $?
}} | Out-Default
if ($null -ne $global:LASTEXITCODE -and $global:LASTEXITCODE -ne 0) {{ exit $global:LASTEXITCODE }}
if (-not $global:wlOk) {{ exit 1 }}
exit 0
"""


def build_argv(command: str, shell: ShellName, exe: str) -> list[str] | str:
    if shell in ("pwsh", "powershell"):
        # -EncodedCommand avoids every quoting problem; the wrapper makes native exit codes propagate.
        script = _PS_TEMPLATE.format(command=command)
        encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        return [exe, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                "-EncodedCommand", encoded]
    if shell == "cmd":
        # A string is passed to CreateProcess unchanged; /s strips the outer quotes.
        return f'"{exe}" /d /s /c "chcp 65001 >nul & {command}"'
    return [exe, "-c", command]


def _child_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("WORKLOOP_")}
    env.update({
        "CI": "1",
        "PYTHONUNBUFFERED": "1",
        "PYTHONIOENCODING": "utf-8",
        "PAGER": "cat",
        "GIT_PAGER": "cat",
        "GIT_TERMINAL_PROMPT": "0",
        "PIP_NO_INPUT": "1",
        "NO_COLOR": "1",
    })
    if not IS_WINDOWS:
        env.setdefault("TERM", "dumb")
    return env


# --- Process tree kill ----------------------------------------------------------


def _kill_tree_sync(proc: subprocess.Popen[bytes]) -> None:
    if sys.platform == "win32":  # spelled out (not IS_WINDOWS) so type checkers narrow the branch
        try:
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=15, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.SubprocessError):
            pass
        if proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass
    else:
        try:
            pgid = os.getpgid(proc.pid)
        except OSError:
            pgid = proc.pid  # leader already gone; its group id equals its pid (start_new_session)
        for sig, wait in ((signal.SIGTERM, 2.0), (signal.SIGKILL, 0.0)):
            try:
                os.killpg(pgid, sig)
            except (ProcessLookupError, PermissionError):
                return
            deadline = time.monotonic() + wait
            while time.monotonic() < deadline and proc.poll() is None:
                time.sleep(0.05)


async def kill_tree(proc: subprocess.Popen[bytes]) -> None:
    await asyncio.to_thread(_kill_tree_sync, proc)


# --- Registry of running commands -----------------------------------------------


@dataclass
class _Running:
    command_id: int
    proc: subprocess.Popen[bytes]
    killed: bool = False


_running: dict[int, _Running] = {}
async def kill_all_commands() -> int:
    """Kill every running command's process tree (used by stop and shutdown). Returns how many."""
    entries = list(_running.values())
    for entry in entries:
        entry.killed = True
    await asyncio.gather(*(kill_tree(e.proc) for e in entries), return_exceptions=True)
    if entries:
        logger.info("Killed %d running command(s)", len(entries))
    return len(entries)


# --- Output capture -------------------------------------------------------------


@dataclass
class _Tail:
    """Keeps the last OUTPUT_TAIL_CHARS characters and counts the total."""

    parts: list[str] = field(default_factory=list)
    size: int = 0
    total: int = 0

    def add(self, text: str) -> None:
        self.parts.append(text)
        self.size += len(text)
        self.total += len(text)
        if self.size > 2 * OUTPUT_TAIL_CHARS:
            joined = "".join(self.parts)[-OUTPUT_TAIL_CHARS:]
            self.parts, self.size = [joined], len(joined)

    def text(self) -> str:
        return "".join(self.parts)[-OUTPUT_TAIL_CHARS:]


def _pipe_reader(pipe: IO[bytes], stream: str, loop: asyncio.AbstractEventLoop, on_chunk, on_eof) -> None:  # type: ignore[no-untyped-def]
    try:
        reader = getattr(pipe, "read1", None) or pipe.read
        while True:
            data = reader(READ_CHUNK)
            if not data:
                break
            loop.call_soon_threadsafe(on_chunk, stream, data)
    except (OSError, ValueError):
        pass
    finally:
        try:
            pipe.close()  # this thread owns the pipe
        except OSError:
            pass
        try:
            loop.call_soon_threadsafe(on_eof, stream)
        except RuntimeError:
            pass  # loop already closed


def _resolve_cwd(cwd: str | None, project_root: Path) -> Path:
    if cwd is None or not str(cwd).strip():
        folder = project_root
    else:
        folder = Path(os.path.expandvars(str(cwd).strip())).expanduser()
        if not folder.is_absolute():
            folder = project_root / folder
    try:
        folder = folder.resolve(strict=True)
    except OSError as exc:
        raise ToolError(f"Working folder does not exist: {cwd}") from exc
    if not folder.is_dir():
        raise ToolError(f"Working folder is not a folder: {cwd}")
    return folder


def _save_started(
    command: str, cwd: str, reason: str, dangerous: bool, run_id: int | None, message_id: int | None
) -> int:
    """Insert the command row. Unknown run/message ids are dropped rather than failing the command."""
    for ids in ((run_id, message_id), (None, None)):
        with SessionLocal() as db:
            row = Command(
                run_id=ids[0], message_id=ids[1], command=command, cwd=cwd, reason=reason,
                permission_required=dangerous, status=CommandStatus.RUNNING,
            )
            db.add(row)
            try:
                db.commit()
            except IntegrityError:
                db.rollback()
                logger.warning("Run %s / message %s not found; saving command without them", run_id, message_id)
                continue
            return row.id
    raise ToolError("Could not record the command in the database.")


def _save_finished(command_id: int, result: CommandResult, status: CommandStatus) -> None:
    with SessionLocal() as db:
        row = db.get(Command, command_id)
        if row is None:
            return
        row.status = status
        row.exit_code = result.exit_code
        row.timed_out = result.timed_out
        row.killed = result.killed
        row.duration_ms = result.duration_ms
        row.stdout_tail = result.stdout
        row.stderr_tail = result.stderr
        row.finished_at = utc_now()
        db.commit()


# --- run_command ----------------------------------------------------------------


async def run_command(
    command: str,
    reason: str,
    cwd: str | None = None,
    timeout: int | None = None,
    *,
    project_root: Path,
    run_id: int | None = None,
    message_id: int | None = None,
    permissions: PermissionManager | None = None,
    shell: str | None = None,
) -> CommandResult:
    """Run a shell command for the Boss. Raises ToolError for bad input; otherwise returns a result."""
    settings = get_settings()
    command = (command or "").strip()
    if not command:
        raise ToolError("command is empty.")
    if len(command) > MAX_COMMAND_LENGTH:
        raise ToolError(f"command is too long ({len(command)} characters, limit {MAX_COMMAND_LENGTH}).")
    reason = (reason or "").strip() or "No reason given"
    limit = settings.command_timeout_max
    seconds = int(timeout if timeout is not None else settings.command_timeout_default)
    seconds = max(1, min(seconds, limit))
    folder = _resolve_cwd(cwd, project_root)
    shell_name, shell_exe = pick_shell(shell)

    # 1. Safety check and permission.
    safety = classify_command(command, project_root, sys.platform, cwd=folder)
    command_id = _save_started(command, str(folder), reason, safety.dangerous, run_id, message_id)

    result = CommandResult(command_id=command_id, command=command, cwd=str(folder), timeout=seconds)
    if safety.dangerous:
        manager = permissions or get_permission_manager()
        try:
            approved = await manager.request(command, reason, safety.reason, run_id)
        except asyncio.CancelledError:
            result.killed = True
            _save_finished(command_id, result, CommandStatus.DENIED)
            raise
        if not approved:
            result.denied = True
            result.danger_reason = safety.reason
            _save_finished(command_id, result, CommandStatus.DENIED)
            logger.info("Command %s denied by the user", command_id)
            return result

    # 2. Start it.
    await emit_command_started(command_id, command, str(folder), reason)
    started = time.monotonic()
    argv = build_argv(command, shell_name, shell_exe)
    popen_kwargs: dict[str, object] = {
        "cwd": str(folder), "env": _child_env(),
        "stdin": subprocess.DEVNULL, "stdout": subprocess.PIPE, "stderr": subprocess.PIPE,
    }
    if IS_WINDOWS:
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    else:
        popen_kwargs["start_new_session"] = True
    try:
        proc = subprocess.Popen(argv, **popen_kwargs)  # type: ignore[call-overload]
    except OSError as exc:
        result.stderr = f"Could not start {shell_name}: {exc}"
        result.stderr_total = len(result.stderr)
        _save_finished(command_id, result, CommandStatus.FAILED_TO_START)
        await emit_command_finished(command_id, None, 0, False, False)
        raise ToolError(f"Could not start the shell ({shell_name}): {exc}") from exc

    entry = _Running(command_id, proc)
    _running[command_id] = entry
    logger.info("Command %s started (pid %s, %s)", command_id, proc.pid, shell_name)

    # 3. Stream output.
    loop = asyncio.get_running_loop()
    batcher = CommandOutputBatcher()
    decoders = {s: codecs.getincrementaldecoder("utf-8")(errors="replace") for s in ("stdout", "stderr")}
    tails = {"stdout": _Tail(), "stderr": _Tail()}
    open_streams = {"stdout", "stderr"}
    streams_done = asyncio.Event()
    exited = asyncio.Event()
    finished = False

    def on_chunk(stream: str, data: bytes) -> None:
        if finished:
            return  # late output from a background child after we stopped listening
        text = decoders[stream].decode(data)
        if text:
            tails[stream].add(text)
            batcher.add(command_id, stream, text)  # type: ignore[arg-type]

    def on_eof(stream: str) -> None:
        open_streams.discard(stream)
        if not open_streams:
            streams_done.set()

    for stream, pipe in (("stdout", proc.stdout), ("stderr", proc.stderr)):
        assert pipe is not None
        threading.Thread(
            target=_pipe_reader, args=(pipe, stream, loop, on_chunk, on_eof),
            name=f"cmd-{command_id}-{stream}", daemon=True,
        ).start()

    def wait_exit() -> None:
        proc.wait()
        try:
            loop.call_soon_threadsafe(exited.set)
        except RuntimeError:
            pass

    threading.Thread(target=wait_exit, name=f"cmd-{command_id}-wait", daemon=True).start()

    # 4. Wait for exit, timeout or cancellation.
    try:
        try:
            await asyncio.wait_for(exited.wait(), timeout=seconds)
        except TimeoutError:
            result.timed_out = True
            logger.info("Command %s timed out after %s s; killing its process tree", command_id, seconds)
            await kill_tree(proc)
            try:
                await asyncio.wait_for(exited.wait(), timeout=10)
            except TimeoutError:
                logger.warning("Command %s did not exit after being killed", command_id)
        try:
            await asyncio.wait_for(streams_done.wait(), timeout=PIPE_DRAIN_SECONDS)
        except TimeoutError:
            pass  # a background child still holds the pipes; don't wait for it
    except asyncio.CancelledError:
        entry.killed = True
        await asyncio.shield(kill_tree(proc))
        raise
    finally:
        finished = True
        _running.pop(command_id, None)
        for stream in ("stdout", "stderr"):
            tail_text = decoders[stream].decode(b"", final=True)
            if tail_text:
                tails[stream].add(tail_text)
                batcher.add(command_id, stream, tail_text)  # type: ignore[arg-type]
        batcher.flush(command_id)
        result.duration_ms = int((time.monotonic() - started) * 1000)
        result.exit_code = proc.poll()
        result.killed = entry.killed or result.timed_out
        result.stdout, result.stdout_total = tails["stdout"].text(), tails["stdout"].total
        result.stderr, result.stderr_total = tails["stderr"].text(), tails["stderr"].total
        _save_finished(command_id, result, CommandStatus.FINISHED)
        # Awaiting in finally is fine even when cancelled: the cancellation was already delivered.
        await emit_command_finished(command_id, result.exit_code, result.duration_ms, result.killed,
                                    result.timed_out)
        logger.info("Command %s finished: exit %s in %d ms", command_id, result.exit_code, result.duration_ms)
    return result
