"""ORM models for all WorkLoop tables."""

from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.database import Base, UTCDateTime, utc_now


def _enum(enum_cls: type[StrEnum], name: str) -> Enum:
    return Enum(
        enum_cls,
        name=name,
        native_enum=False,
        validate_strings=True,
        values_callable=lambda e: [m.value for m in e],
        length=32,
    )


# --- Enums -----------------------------------------------------------------


class PromptStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    MANUAL_FIX = "manual_fix"


class RunStatus(StrEnum):
    RUNNING = "running"
    NEEDS_USER = "needs_user"
    COMPLETED = "completed"
    STOPPED = "stopped"
    ERROR = "error"


class AgentName(StrEnum):
    BOSS = "boss"
    WORKER = "worker"


class LlmRole(StrEnum):
    """Role in the LLM conversation; lets a history be rebuilt from the table."""

    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class MessageKind(StrEnum):
    TASK = "task"
    REPORT = "report"
    VERDICT = "verdict"
    THINKING = "thinking"
    SYSTEM = "system"  # template / reminder messages sent to an agent
    TOOL = "tool"  # tool results


class PermissionStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"
    CANCELLED = "cancelled"


class CommandStatus(StrEnum):
    RUNNING = "running"
    FINISHED = "finished"
    DENIED = "denied"
    FAILED_TO_START = "failed_to_start"


# --- Mixins ----------------------------------------------------------------


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=utc_now, onupdate=utc_now, nullable=False
    )


# --- Tables ----------------------------------------------------------------


class ApiProfile(TimestampMixin, Base):
    __tablename__ = "api_profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(50), nullable=False)
    # Casefolded copy of name, so "Mamad" and "mamad" (and non-ASCII variants) clash.
    name_key: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    model_name: Mapped[str] = mapped_column(String(200), nullable=False)
    base_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    api_key_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    key_preview: Mapped[str] = mapped_column(String(32), nullable=False)
    last_test_ok: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    last_test_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_tested_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)



class Setting(Base):
    """Simple key/value store (e.g. project_path, boss_profile_id, worker_profile_id)."""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=utc_now, onupdate=utc_now, nullable=False
    )


class Prompt(TimestampMixin, Base):
    __tablename__ = "prompts"
    __table_args__ = (Index("ix_prompts_status_position", "status", "position"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[PromptStatus] = mapped_column(
        _enum(PromptStatus, "prompt_status"), default=PromptStatus.PENDING, nullable=False
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    rounds_used: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


class Run(TimestampMixin, Base):
    """One work session: from POST /run/start until stop, empty queue or a fatal error.

    A needs_user pause keeps the same run open. Messages, commands, permission
    requests and snapshots of every prompt processed in the session link to it.
    """

    __tablename__ = "runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    status: Mapped[RunStatus] = mapped_column(
        _enum(RunStatus, "run_status"), default=RunStatus.RUNNING, nullable=False, index=True
    )
    boss_profile_id: Mapped[int | None] = mapped_column(
        ForeignKey("api_profiles.id", ondelete="SET NULL"), nullable=True
    )
    worker_profile_id: Mapped[int | None] = mapped_column(
        ForeignKey("api_profiles.id", ondelete="SET NULL"), nullable=True
    )
    project_path: Mapped[str] = mapped_column(String(1000), nullable=False)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    prompts_done: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    prompts_failed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Kept so history still shows who worked even after a profile is deleted.
    boss_name: Mapped[str | None] = mapped_column(String(50), nullable=True)
    worker_name: Mapped[str | None] = mapped_column(String(50), nullable=True)

    messages: Mapped[list["Message"]] = relationship(
        back_populates="run", cascade="all, delete-orphan", order_by="Message.id"
    )
    permission_requests: Mapped[list["PermissionRequest"]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )
    commands: Mapped[list["Command"]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )


class Message(Base):
    """One message in an agent's conversation (user template, assistant reply or tool result)."""

    __tablename__ = "messages"
    __table_args__ = (Index("ix_messages_prompt_agent", "prompt_id", "agent", "id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int | None] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), nullable=True, index=True
    )
    prompt_id: Mapped[int | None] = mapped_column(
        ForeignKey("prompts.id", ondelete="CASCADE"), nullable=True
    )
    round: Mapped[int | None] = mapped_column(Integer, nullable=True)
    agent: Mapped[AgentName] = mapped_column(_enum(AgentName, "agent_name"), nullable=False)
    role: Mapped[LlmRole] = mapped_column(_enum(LlmRole, "llm_role"), nullable=False)
    kind: Mapped[MessageKind] = mapped_column(_enum(MessageKind, "message_kind"), nullable=False)
    content: Mapped[str] = mapped_column(Text, default="", nullable=False)
    tool_calls: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, nullable=True)
    tool_call_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    tool_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now, nullable=False)

    run: Mapped[Run | None] = relationship(back_populates="messages")


class PermissionRequest(Base):
    """A dangerous Boss command waiting for (or answered by) the user."""

    __tablename__ = "permission_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int | None] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), nullable=True, index=True
    )
    command: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    danger_reason: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[PermissionStatus] = mapped_column(
        _enum(PermissionStatus, "permission_status"),
        default=PermissionStatus.PENDING,
        nullable=False,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now, nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    run: Mapped[Run | None] = relationship(back_populates="permission_requests")


class Command(Base):
    """A shell command the Boss ran (or tried to run)."""

    __tablename__ = "commands"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int | None] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), nullable=True, index=True
    )
    message_id: Mapped[int | None] = mapped_column(
        ForeignKey("messages.id", ondelete="SET NULL"), nullable=True
    )
    permission_required: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    command: Mapped[str] = mapped_column(Text, nullable=False)
    cwd: Mapped[str] = mapped_column(String(1000), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[CommandStatus] = mapped_column(
        _enum(CommandStatus, "command_status"), default=CommandStatus.RUNNING, nullable=False
    )
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    timed_out: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    killed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    stdout_tail: Mapped[str | None] = mapped_column(Text, nullable=True)
    stderr_tail: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    run: Mapped[Run | None] = relationship(back_populates="commands")


class Snapshot(Base):
    """The project's files at the moment a prompt failed (taken when the run pauses in needs_user)."""

    __tablename__ = "snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True)
    prompt_id: Mapped[int | None] = mapped_column(ForeignKey("prompts.id", ondelete="SET NULL"), nullable=True)
    project_path: Mapped[str] = mapped_column(String(1000), nullable=False)
    file_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    truncated: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now, nullable=False)


class FileSnapshot(Base):
    """One file in a snapshot: path, size, mtime and content hash (no content)."""

    __tablename__ = "file_snapshots"
    __table_args__ = (Index("ix_file_snapshots_run_path", "run_id", "rel_path"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    snapshot_id: Mapped[int] = mapped_column(
        ForeignKey("snapshots.id", ondelete="CASCADE"), nullable=False, index=True
    )
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), nullable=False)
    prompt_id: Mapped[int | None] = mapped_column(ForeignKey("prompts.id", ondelete="SET NULL"), nullable=True)
    rel_path: Mapped[str] = mapped_column(String(1000), nullable=False)  # POSIX style
    size: Mapped[int] = mapped_column(Integer, nullable=False)
    mtime_ns: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now, nullable=False)


class RunPrompt(Base):
    """Which prompts a run worked on (a prompt can appear in several runs, e.g. after a stop)."""

    __tablename__ = "run_prompts"
    __table_args__ = (UniqueConstraint("run_id", "prompt_id", name="uq_run_prompts"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True)
    prompt_id: Mapped[int] = mapped_column(ForeignKey("prompts.id", ondelete="CASCADE"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now, nullable=False)


class VerdictRecord(Base):
    """A verdict the Boss submitted (one per tested round)."""

    __tablename__ = "verdicts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int | None] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), nullable=True, index=True)
    prompt_id: Mapped[int] = mapped_column(ForeignKey("prompts.id", ondelete="CASCADE"), nullable=False)
    round: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    bugs: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    fix_instructions: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now, nullable=False)


class ManualFix(Base):
    """The user fixed a failed prompt by hand: the file changes WorkLoop detected."""

    __tablename__ = "manual_fixes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True)
    prompt_id: Mapped[int] = mapped_column(ForeignKey("prompts.id", ondelete="CASCADE"), nullable=False)
    added: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    removed: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    modified: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now, nullable=False)
