"""Validated configuration, persistence, and view models."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SESSION_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,79}$")


def utc_now() -> datetime:
    """Return a timezone-aware timestamp for persisted state."""
    return datetime.now(UTC)


def require_absolute_cwd(value: Path) -> Path:
    if not value.is_absolute():
        raise ValueError("working directory must be absolute")
    return value


def normalize_tags(values: list[str]) -> list[str]:
    cleaned: list[str] = []
    for value in values:
        tag = value.strip().lower()
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,31}", tag):
            raise ValueError(f"invalid tag: {value!r}")
        if tag not in cleaned:
            cleaned.append(tag)
    return cleaned


class Tool(StrEnum):
    CLAUDE = "claude"
    CODEX = "codex"
    HERMES = "hermes"
    SHELL = "shell"


class RuntimeState(StrEnum):
    ATTACHED = "attached"
    DETACHED = "detached"
    STOPPED = "stopped"
    FAILED = "failed"
    UNKNOWN = "unknown"


class TaskState(StrEnum):
    IN_PROGRESS = "in_progress"
    WAITING = "waiting"
    NEEDS_INPUT = "needs_input"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    UNSPECIFIED = "unspecified"


class SessionState(StrEnum):
    """Schema-v1 state values retained for migration plan compatibility."""

    ACTIVE = "active"
    WAITING = "waiting"
    BLOCKED = "blocked"
    DONE = "done"
    PAUSED = "paused"


class InputState(StrEnum):
    NONE = "none"
    REQUIRED = "required"


class AgentState(StrEnum):
    """Conservative execution state derived from runtime, task, and pane output."""

    ACTIVE = "active"
    WAITING = "waiting"
    PAUSED = "paused"
    COMPLETED = "completed"
    STOPPED = "stopped"
    FAILED = "failed"
    UNKNOWN = "unknown"


class OutputSource(StrEnum):
    """Sanitized output source used by inspectors and the full Logs workspace."""

    PANE = "pane"
    SAVED = "saved"


class InterfacePreferences(BaseModel):
    """User-selectable interface state kept separate from session metadata."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal[1] = 1
    grouping: Literal["attention", "runtime", "agent", "project", "warning", "recent"] = "attention"
    density: Literal["compact", "comfortable"] = "comfortable"


LEGACY_TASK_STATES = {
    "active": TaskState.IN_PROGRESS,
    "done": TaskState.COMPLETED,
    "paused": TaskState.WAITING,
}


def normalize_task_state(value: object) -> TaskState:
    """Normalize schema-v1 and legacy launcher task state values."""
    if isinstance(value, TaskState):
        return value
    raw = str(value or "").strip().lower().replace(" ", "_")
    if raw in LEGACY_TASK_STATES:
        return LEGACY_TASK_STATES[raw]
    return TaskState(raw or TaskState.UNSPECIFIED)


class SessionName(BaseModel):
    """Validated tmux session name."""

    model_config = ConfigDict(frozen=True)
    value: str

    @field_validator("value")
    @classmethod
    def valid_name(cls, value: str) -> str:
        if not SESSION_NAME_PATTERN.fullmatch(value):
            raise ValueError(
                "use 1-80 lowercase letters, digits, hyphens, or underscores; "
                "the first character must be alphanumeric"
            )
        return value


class TmuxSession(BaseModel):
    """Read-only snapshot returned by tmux."""

    model_config = ConfigDict(frozen=True)
    session_id: str
    name: str
    created_at: datetime
    attached_clients: int = Field(ge=0)
    windows: int = Field(ge=1)
    cwd: Path
    current_command: str
    wf_owner: str | None = None
    logging_enabled: bool = False
    last_activity_at: datetime | None = None
    pane_dead: bool = False
    pane_dead_status: int | None = None

    @property
    def attached(self) -> bool:
        return self.attached_clients > 0


class SessionMetadata(BaseModel):
    """ws-owned state. A tmux ID prevents accidental adoption after name reuse."""

    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[2] = 2
    record_id: UUID = Field(default_factory=uuid4)
    owner: Literal["workspace-session-manager"] = "workspace-session-manager"
    tmux_session_id: str
    name: str
    display_name: Annotated[str, Field(max_length=200)] = ""
    tool: Tool
    cwd: Path
    project: Annotated[str, Field(max_length=200)] = ""
    note: Annotated[str, Field(max_length=2000)] = ""
    tags: Annotated[list[str], Field(max_length=12)] = Field(default_factory=list)
    task_state: TaskState = TaskState.IN_PROGRESS
    input_state: InputState = InputState.NONE
    pinned: bool = False
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    last_attached_at: datetime | None = None

    @model_validator(mode="before")
    @classmethod
    def migrate_schema_v1(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        migrated = dict(value)
        if "task_state" not in migrated:
            migrated["task_state"] = normalize_task_state(migrated.pop("state", None)).value
        migrated.setdefault("input_state", InputState.NONE.value)
        migrated.setdefault("project", "")
        migrated["schema_version"] = 2
        return migrated

    @field_validator("name")
    @classmethod
    def valid_name(cls, value: str) -> str:
        return SessionName(value=value).value

    @field_validator("cwd")
    @classmethod
    def absolute_cwd(cls, value: Path) -> Path:
        return require_absolute_cwd(value)

    @field_validator("tags")
    @classmethod
    def valid_tags(cls, values: list[str]) -> list[str]:
        return normalize_tags(values)

    @property
    def state(self) -> TaskState:
        """Compatibility accessor for integrations written against schema v1."""
        return self.task_state


class LegacyMetadata(BaseModel):
    """Read-only data discovered from the legacy launcher."""

    model_config = ConfigDict(frozen=True)
    tool: Tool | None = None
    cwd: Path | None = None
    project: Path | None = None
    note: str = ""
    tags: list[str] = Field(default_factory=list)
    state: str = ""
    last_used: datetime | None = None
    pinned: bool = False
    source: Path | None = None


class SessionView(BaseModel):
    """Merged session data used by the CLI and TUI."""

    model_config = ConfigDict(frozen=True)
    name: str
    display_name: str = ""
    session_id: str
    tool: Tool
    cwd: Path
    current_command: str
    runtime: RuntimeState
    attached: bool
    attached_clients: int
    windows: int
    created_at: datetime
    project: str = ""
    note: str = ""
    tags: list[str] = Field(default_factory=list)
    task_state: TaskState = TaskState.UNSPECIFIED
    input_state: InputState = InputState.NONE
    pinned: bool = False
    owned: bool = False
    legacy_metadata: bool = False
    logging_enabled: bool = False
    last_active_at: datetime | None = None

    @property
    def state(self) -> str:
        """Compatibility accessor for schema-v1 CLI and API clients."""
        return self.task_state.value


class SessionDetails(BaseModel):
    """Session view plus a sanitized, non-persisted pane preview."""

    session: SessionView
    preview: str
    preview_truncated: bool = False
    output_source: OutputSource = OutputSource.PANE
    available_sources: tuple[OutputSource, ...] = (OutputSource.PANE,)


class CreateRequest(BaseModel):
    """Validated request shared by CLI and TUI."""

    name: str
    display_name: Annotated[str, Field(max_length=200)] = ""
    tool: Tool
    cwd: Path
    project: Annotated[str, Field(max_length=200)] = ""
    note: Annotated[str, Field(max_length=2000)] = ""
    tags: Annotated[list[str], Field(max_length=12)] = Field(default_factory=list)
    task_state: TaskState = TaskState.IN_PROGRESS
    input_state: InputState = InputState.NONE
    logging_enabled: bool = True
    automatic_prefix: bool = True

    @field_validator("cwd")
    @classmethod
    def absolute_cwd(cls, value: Path) -> Path:
        return require_absolute_cwd(value)

    @field_validator("tags")
    @classmethod
    def valid_tags(cls, values: list[str]) -> list[str]:
        return normalize_tags(values)


class Preset(BaseModel):
    """A saved tool/cwd/project/tags/logging combination, reused via `ws create --from-preset`."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str
    tool: Tool
    cwd: Path
    project: Annotated[str, Field(max_length=200)] = ""
    tags: Annotated[list[str], Field(max_length=12)] = Field(default_factory=list)
    logging_enabled: bool = True

    @field_validator("cwd")
    @classmethod
    def absolute_cwd(cls, value: Path) -> Path:
        return require_absolute_cwd(value)

    @field_validator("tags")
    @classmethod
    def valid_tags(cls, values: list[str]) -> list[str]:
        return normalize_tags(values)


class HealthStatus(StrEnum):
    PASS = "pass"  # noqa: S105
    WARN = "warn"
    FAIL = "fail"
    INFO = "info"


class HealthCheck(BaseModel):
    name: str
    status: HealthStatus
    detail: str
    corrective_action: str = ""
    fixable: bool = False
    affected: list[str] = []


class DoctorReport(BaseModel):
    checks: list[HealthCheck]

    @property
    def healthy(self) -> bool:
        return not any(check.status is HealthStatus.FAIL for check in self.checks)

    def count(self, status: HealthStatus) -> int:
        return sum(check.status is status for check in self.checks)
