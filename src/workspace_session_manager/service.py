"""Session lifecycle policy, ownership enforcement, and merged discovery."""

from __future__ import annotations

import contextlib
import getpass
import hashlib
import hmac
import io
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import tarfile
import tempfile
import time
import tomllib
import unicodedata
from base64 import urlsafe_b64decode, urlsafe_b64encode
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from workspace_session_manager.config import AppConfig
from workspace_session_manager.errors import (
    OwnershipError,
    PresetNotFoundError,
    SessionExistsError,
    SessionNotFoundError,
    StateError,
    ToolUnavailableError,
    WsError,
)
from workspace_session_manager.health import (
    apt_updates_check,
    docker_containers_check,
    git_dirty_repos_check,
    idle_live_sessions_check,
    missing_cwd_check,
    orphaned_logs_check,
    reboot_required_check,
    zombie_sessions_check,
)
from workspace_session_manager.legacy import LegacyMetadataReader
from workspace_session_manager.models import (
    CreateRequest,
    DoctorReport,
    FilterPreset,
    HealthCheck,
    HealthStatus,
    InputState,
    OutputSource,
    Preset,
    RuntimeState,
    SessionDetails,
    SessionMetadata,
    SessionTemplate,
    SessionTimelineEvent,
    SessionView,
    TaskState,
    TmuxSession,
    Tool,
    UndoEntry,
    normalize_task_state,
    utc_now,
)
from workspace_session_manager.paths import AppPaths
from workspace_session_manager.security import BoundedOutput, bounded_output, redact_text
from workspace_session_manager.store import (
    FilterPresetStore,
    MetadataStore,
    PresetStore,
    TemplateStore,
    TimelineStore,
    UndoStore,
)
from workspace_session_manager.tmux import Runner, subprocess_runner

SEARCH_CONTEXT_LINES = 2
SEARCH_MAX_MATCHES_PER_SESSION = 20
SEARCH_READ_CAP_BYTES = 2_097_152
DEFAULT_CREATE_TOOL_PRIORITY: tuple[Tool, ...] = (
    Tool.CLAUDE,
    Tool.COPILOT,
    Tool.CODEX,
    Tool.HERMES,
    Tool.SHELL,
)
BUILTIN_PLAYBOOKS: dict[str, dict[str, object]] = {
    "quota-hit": {
        "description": "Collect context after usage/session limit issues.",
        "match_any": ("session limit", "usage limit", "quota"),
        "commands": (("ws", "report"), ("ws", "health", "--actionable")),
        "timeout_seconds": 10.0,
    },
    "stale-detached": {
        "description": "Review stale detached inventory and archive candidates.",
        "match_any": ("detached", "stale"),
        "commands": (("ws", "archive", "--dry-run"), ("ws", "report")),
        "timeout_seconds": 10.0,
    },
    "dirty-repo": {
        "description": "Collect hygiene diagnostics for dirty working trees.",
        "match_any": ("dirty repo", "working tree"),
        "commands": (("ws", "health", "--actionable"),),
        "timeout_seconds": 10.0,
    },
    "command-failed": {
        "description": "Gather recovery hints after command failures.",
        "match_any": ("failed", "error", "permission denied", "not found"),
        "commands": (("ws", "recover"), ("ws", "report")),
        "timeout_seconds": 10.0,
    },
}
FEDERATED_GUARDED_ACTIONS: frozenset[str] = frozenset({"resume", "attach"})
FEDERATED_CAPABILITY_ACTIONS: tuple[str, ...] = ("list", "health", "report", "resume", "attach")
INCIDENT_ALLOWED_SEVERITIES: frozenset[str] = frozenset({"info", "warn", "fail", "critical"})


class SessionBackend(Protocol):
    def version(self) -> str: ...

    def list_sessions(self) -> list[TmuxSession]: ...

    def get_session(self, name: str) -> TmuxSession: ...

    def session_exists(self, name: str) -> bool: ...

    def create_session(
        self,
        name: str,
        cwd: Path,
        shell_command: Sequence[str],
        agent_command: Sequence[str] | None,
    ) -> TmuxSession: ...

    def capture_pane(self, name: str, lines: int, expected_id: str | None = None) -> str: ...

    def send_interrupt(self, name: str, expected_id: str | None = None) -> None: ...

    def restart_session(
        self,
        name: str,
        cwd: Path,
        shell_command: Sequence[str],
        agent_command: Sequence[str] | None,
        expected_id: str | None = None,
    ) -> None: ...

    def set_logging(
        self,
        name: str,
        log_path: Path | None,
        expected_id: str | None = None,
    ) -> None: ...

    def attach(self, name: str, expected_id: str | None = None) -> int: ...

    def rename_session(
        self, old_name: str, new_name: str, expected_id: str | None = None
    ) -> None: ...

    def kill_session(self, name: str, expected_id: str | None = None) -> None: ...

    def set_option(
        self, name: str, option: str, value: str, expected_id: str | None = None
    ) -> None: ...

    def get_option(self, name: str, option: str, expected_id: str | None = None) -> str | None: ...

    def unset_option(self, name: str, option: str, expected_id: str | None = None) -> None: ...


@dataclass(frozen=True, slots=True)
class CreateValidation:
    normalized_name: str
    cwd: Path | None
    detected_project: str
    command: tuple[str, ...]
    name_error: str = ""
    cwd_error: str = ""
    tool_error: str = ""

    @property
    def errors(self) -> tuple[str, ...]:
        return tuple(issue for issue in (self.name_error, self.cwd_error, self.tool_error) if issue)

    @property
    def valid(self) -> bool:
        return not self.errors


@dataclass(frozen=True, slots=True)
class RenameValidation:
    normalized_name: str
    name_error: str = ""

    @property
    def valid(self) -> bool:
        return not self.name_error


@dataclass(frozen=True, slots=True)
class TailResult:
    text: str
    offset: int
    rotated: bool
    truncated: bool = False


@dataclass(frozen=True, slots=True)
class LogSearchMatch:
    line_number: int
    line: str
    context_before: tuple[str, ...]
    context_after: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class LogSearchResult:
    name: str
    display_name: str
    matches: tuple[LogSearchMatch, ...]


@dataclass(frozen=True, slots=True)
class LogSearchSummary:
    results: tuple[LogSearchResult, ...]
    skipped_no_log: int


def slugify_name(value: str) -> str:
    ascii_value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    normalized = re.sub(r"[^a-z0-9]+", "-", ascii_value.lower())
    return re.sub(r"-{2,}", "-", normalized).strip("-")[:80]


def normalized_session_name(tool: Tool, requested: str, *, automatic_prefix: bool = True) -> str:
    purpose = slugify_name(requested)
    if not purpose:
        raise WsError("session name must contain a letter or digit")
    if not automatic_prefix or tool is Tool.SHELL:
        return purpose
    if purpose == tool.value or purpose.startswith(f"{tool.value}-"):
        return purpose
    available = 80 - len(tool.value) - 1
    return f"{tool.value}-{purpose[:available].rstrip('-_')}"


def infer_tool(name: str, current_command: str) -> Tool:
    for tool in (Tool.CLAUDE, Tool.COPILOT, Tool.CODEX, Tool.HERMES):
        if name == tool.value or name.startswith(f"{tool.value}-"):
            return tool
    command = Path(current_command).name.lower()
    if command in {tool.value for tool in Tool if tool is not Tool.SHELL}:
        return Tool(command)
    return Tool.SHELL


def default_enabled_tool(config: AppConfig) -> Tool | None:
    for tool in DEFAULT_CREATE_TOOL_PRIORITY:
        profile = config.tools.get(tool)
        if profile is not None and profile.enabled:
            return tool
    return None


@dataclass(frozen=True)
class HealthCheckSpec:
    name: str
    enabled: bool
    ttl_seconds: float
    run: Callable[[], HealthCheck]


def disk_space_check(root: Path, *, warn_percent: int, fail_percent: int) -> HealthCheck:
    usage = shutil.disk_usage(root)
    available_percent = int((usage.free / usage.total) * 100) if usage.total else 0
    status = (
        HealthStatus.FAIL
        if available_percent < fail_percent
        else HealthStatus.WARN
        if available_percent < warn_percent
        else HealthStatus.PASS
    )
    return HealthCheck(
        name="disk-space",
        status=status,
        detail=f"{available_percent}% available",
        corrective_action="Free disk space before creating sessions or enabling logs."
        if status is not HealthStatus.PASS
        else "",
    )


def command_available(command: tuple[str, ...]) -> bool:
    executable = command[0]
    if "/" in executable:
        path = Path(executable).expanduser()
        return path.is_file() and os.access(path, os.X_OK)
    return shutil.which(executable) is not None


def runtime_state(session: TmuxSession) -> RuntimeState:
    if session.pane_dead:
        if session.pane_dead_status not in (None, 0):
            return RuntimeState.FAILED
        return RuntimeState.STOPPED
    if session.attached:
        return RuntimeState.ATTACHED
    return RuntimeState.DETACHED


def legacy_task_state(value: str) -> TaskState:
    try:
        return normalize_task_state(value)
    except ValueError:
        return TaskState.UNSPECIFIED


class SessionService:
    def __init__(
        self,
        backend: SessionBackend,
        store: MetadataStore,
        config: AppConfig,
        paths: AppPaths,
        legacy: LegacyMetadataReader | None = None,
        runner: Runner = subprocess_runner,
        preset_store: PresetStore | None = None,
        filter_preset_store: FilterPresetStore | None = None,
        timeline_store: TimelineStore | None = None,
        template_store: TemplateStore | None = None,
        undo_store: UndoStore | None = None,
    ) -> None:
        self.backend = backend
        self.store = store
        self.config = config
        self.paths = paths
        self.legacy = legacy or LegacyMetadataReader(config.legacy_state_dirs)
        self.runner = runner
        self.preset_store = preset_store or PresetStore(paths)
        self.filter_preset_store = filter_preset_store or FilterPresetStore(paths)
        self.timeline_store = timeline_store or TimelineStore(paths)
        self.template_store = template_store or TemplateStore(paths)
        self.undo_store = undo_store or UndoStore(paths)
        self._guardrail_depth = 0

    def _action_allowed(self, action: str) -> bool:
        active = self.active_operator_profile()
        allowed_raw = active.get("allowed_actions")
        if not isinstance(allowed_raw, list):
            return True
        allowed = {str(item).strip() for item in allowed_raw if str(item).strip()}
        if not allowed:
            return True
        return "*" in allowed or action in allowed

    def _enforce_action_allowed(self, action: str) -> None:
        if self._guardrail_depth > 0:
            return
        if self._action_allowed(action):
            return
        active = self.active_operator_profile()
        profile_name = str(active.get("name") or "active profile")
        allowed_raw = active.get("allowed_actions")
        allowed = (
            ", ".join(str(item) for item in allowed_raw)
            if isinstance(allowed_raw, list) and allowed_raw
            else "*"
        )
        raise WsError(
            f"action blocked by operator profile '{profile_name}': {action} (allowed: {allowed})"
        )

    @contextlib.contextmanager
    def _guarded_action(self, action: str):
        self._enforce_action_allowed(action)
        self._guardrail_depth += 1
        try:
            yield
        finally:
            self._guardrail_depth = max(0, self._guardrail_depth - 1)

    def list_sessions(self, *, include_unmanaged: bool = False) -> list[SessionView]:
        self.apply_idle_policy()
        owned_records = self.store.load_all()
        live_sessions = self.backend.list_sessions()
        live_names = {session.name for session in live_sessions}
        views = [self._merge(session, owned_records.get(session.name)) for session in live_sessions]
        if not include_unmanaged:
            views = [view for view in views if view.owned]
            views.extend(
                self._stopped(record)
                for name, record in owned_records.items()
                if name not in live_names
            )
        minimum = datetime.min.replace(tzinfo=UTC)
        return sorted(
            views,
            key=lambda view: (
                view.pinned,
                view.attached,
                view.last_active_at or view.created_at or minimum,
                view.name,
            ),
            reverse=True,
        )

    def _merge(self, session: TmuxSession, record: SessionMetadata | None) -> SessionView:
        owned = (
            record is not None
            and record.tmux_session_id == session.session_id
            and session.wf_owner == "workspace-session-manager"
        )
        legacy = None if owned else self.legacy.read(session.name)
        return SessionView(
            name=session.name,
            display_name=record.display_name if owned and record else "",
            session_id=session.session_id,
            tool=record.tool
            if owned and record
            else legacy.tool
            if legacy and legacy.tool
            else infer_tool(session.name, session.current_command),
            cwd=record.cwd
            if owned and record
            else legacy.cwd
            if legacy and legacy.cwd
            else session.cwd,
            current_command=session.current_command,
            runtime=runtime_state(session),
            attached=session.attached,
            attached_clients=session.attached_clients,
            windows=session.windows,
            created_at=session.created_at,
            project=(
                record.project
                if owned and record
                else legacy.project.name
                if legacy and legacy.project
                else ""
            ),
            note=record.note if owned and record else legacy.note if legacy else "",
            tags=record.tags if owned and record else [],
            task_state=(
                record.task_state
                if owned and record
                else legacy_task_state(legacy.state)
                if legacy
                else TaskState.UNSPECIFIED
            ),
            input_state=record.input_state if owned and record else InputState.NONE,
            pinned=record.pinned if owned and record else legacy.pinned if legacy else False,
            owned=owned,
            legacy_metadata=legacy is not None,
            logging_enabled=session.logging_enabled,
            last_active_at=max(
                filter(
                    None,
                    (
                        session.last_activity_at,
                        record.last_attached_at if owned and record else None,
                        legacy.last_used if legacy else None,
                        session.created_at,
                    ),
                )
            ),
        )

    def _stopped(self, record: SessionMetadata) -> SessionView:
        profile = self.config.tools[record.tool]
        return SessionView(
            name=record.name,
            display_name=record.display_name,
            session_id=record.tmux_session_id,
            tool=record.tool,
            cwd=record.cwd,
            current_command=Path(profile.command[0]).name,
            runtime=RuntimeState.STOPPED,
            attached=False,
            attached_clients=0,
            windows=0,
            created_at=record.created_at,
            project=record.project,
            note=record.note,
            tags=record.tags,
            task_state=record.task_state,
            input_state=record.input_state,
            pinned=record.pinned,
            owned=True,
            logging_enabled=False,
            last_active_at=record.last_attached_at or record.updated_at,
        )

    def get(self, name: str, *, include_unmanaged: bool = False) -> SessionView:
        for session in self.list_sessions(include_unmanaged=include_unmanaged):
            if session.name == name:
                return session
        raise SessionNotFoundError(f"session not found: {name}")

    def inspect(self, name: str) -> SessionDetails:
        return self.inspect_snapshot(self.get(name))

    def inspect_snapshot(
        self,
        session: SessionView,
        *,
        preview_lines: int | None = None,
        preview_bytes: int | None = None,
    ) -> SessionDetails:
        """Inspect one inventory snapshot with an exact tmux-ID guard."""
        max_lines = preview_lines or self.config.preview_lines
        max_bytes = preview_bytes or self.config.preview_bytes
        if max_lines < 1 or max_bytes < 1:
            raise ValueError("preview limits must be positive")
        saved_available = self._saved_log_available(session.name)
        available_sources = (
            *((OutputSource.PANE,) if session.runtime is not RuntimeState.STOPPED else ()),
            *((OutputSource.SAVED,) if saved_available else ()),
        )
        if session.runtime is RuntimeState.STOPPED:
            preview = self._read_log(session.name, max_lines, max_bytes)
            return SessionDetails(
                session=session,
                preview=preview.text,
                preview_truncated=preview.truncated,
                output_source=OutputSource.SAVED,
                available_sources=available_sources,
            )
        output = self.backend.capture_pane(
            session.name, max_lines + 1, expected_id=session.session_id
        )
        preview = bounded_output(
            output,
            max_lines=max_lines,
            max_bytes=max_bytes,
        )
        return SessionDetails(
            session=session,
            preview=preview.text,
            preview_truncated=preview.truncated,
            output_source=OutputSource.PANE,
            available_sources=available_sources,
        )

    def logs(self, name: str, *, source: OutputSource | None = None) -> SessionDetails:
        """Return a larger, still bounded and sanitized pane capture."""
        session = self.get(name)
        saved_available = self._saved_log_available(name)
        live_available = session.runtime is not RuntimeState.STOPPED
        available_sources = (
            *((OutputSource.PANE,) if live_available else ()),
            *((OutputSource.SAVED,) if saved_available else ()),
        )

        selected_source = source
        persisted: BoundedOutput | None = None
        if selected_source is OutputSource.PANE and not live_available:
            raise SessionNotFoundError(f"live pane unavailable for stopped session: {name}")
        if selected_source is OutputSource.SAVED and not saved_available:
            raise StateError(f"saved log unavailable for session: {name}")
        if selected_source is None and saved_available:
            persisted = self._read_log(name, self.config.log_lines, self.config.log_bytes)
            if persisted.text or not live_available:
                selected_source = OutputSource.SAVED
        if selected_source is None:
            selected_source = OutputSource.PANE if live_available else OutputSource.SAVED

        if selected_source is OutputSource.PANE:
            output = self.backend.capture_pane(
                name, self.config.log_lines + 1, expected_id=session.session_id
            )
            preview = bounded_output(
                output,
                max_lines=self.config.log_lines,
                max_bytes=self.config.log_bytes,
            )
        else:
            preview = persisted or self._read_log(
                name, self.config.log_lines, self.config.log_bytes
            )
        return SessionDetails(
            session=session,
            preview=preview.text,
            preview_truncated=preview.truncated,
            output_source=selected_source,
            available_sources=available_sources,
        )

    def detect_project(self, cwd: Path) -> str:
        """Detect a project without turning the user's home directory into a project."""
        try:
            current = cwd.expanduser().resolve(strict=True)
        except OSError:
            return ""
        if not current.is_dir():
            return ""
        try:
            home = Path.home().resolve(strict=True)
        except OSError:
            home = Path.home()
        if current == home:
            return ""
        for candidate in (current, *current.parents):
            marker = candidate / ".git"
            if marker.is_dir() or marker.is_file():
                return candidate.name

        for candidate in (current, *current.parents):
            pyproject = candidate / "pyproject.toml"
            if pyproject.is_file():
                try:
                    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
                    project = data.get("project", {})
                    if isinstance(project, dict) and isinstance(project.get("name"), str):
                        return str(project["name"]).strip()
                except (OSError, tomllib.TOMLDecodeError):
                    pass
                return candidate.name
            package_json = candidate / "package.json"
            if package_json.is_file():
                try:
                    data = json.loads(package_json.read_text(encoding="utf-8"))
                    if isinstance(data, dict) and isinstance(data.get("name"), str):
                        return str(data["name"]).strip()
                except (OSError, json.JSONDecodeError):
                    pass
                return candidate.name

        return current.name

    def project_default(self, project: str) -> tuple[Tool | None, list[str], str, bool | None]:
        for item in self.config.project_defaults:
            if item.project == project:
                return item.tool, list(item.tags), item.task_template, item.logging_enabled
        return None, [], "", None

    def apply_idle_policy(self) -> int:
        days = self.config.health.idle_auto_wait_days
        if days <= 0:
            return 0
        changed = 0
        now = utc_now()
        for record in self.store.load_all().values():
            if record.task_state in {TaskState.COMPLETED, TaskState.BLOCKED, TaskState.WAITING}:
                continue
            anchor = record.last_attached_at or record.updated_at
            if (now - anchor) < timedelta(days=days):
                continue
            self.store.save(
                record.model_copy(update={"task_state": TaskState.WAITING, "updated_at": now})
            )
            self.append_timeline(
                record.name, "idle-policy", f"Auto-marked waiting after {days}d idle"
            )
            changed += 1
        return changed

    def validate_create(
        self,
        tool: Tool,
        requested_name: str,
        cwd: Path,
        *,
        automatic_prefix: bool = True,
    ) -> CreateValidation:
        name_error = ""
        cwd_error = ""
        tool_error = ""
        normalized = ""
        resolved: Path | None = None
        try:
            normalized = normalized_session_name(
                tool, requested_name, automatic_prefix=automatic_prefix
            )
        except WsError as error:
            name_error = str(error)
        if len(requested_name.strip()) > 200:
            name_error = "display name must be 200 characters or fewer"
        if normalized and (
            self.backend.session_exists(normalized) or self.store.load(normalized) is not None
        ):
            name_error = f"session already exists: {normalized}"
        try:
            resolved = cwd.expanduser().resolve(strict=True)
            if not resolved.is_dir():
                cwd_error = f"working directory is not a directory: {resolved}"
                resolved = None
        except OSError:
            cwd_error = f"working directory does not exist: {cwd}"
        profile = self.config.tools[tool]
        if not profile.enabled:
            tool_error = f"{tool.value} is disabled in configuration"
        elif not command_available(profile.command):
            tool_error = f"command not found: {profile.command[0]}"
        return CreateValidation(
            normalized_name=normalized,
            cwd=resolved,
            detected_project=self.detect_project(resolved) if resolved else "",
            command=profile.command,
            name_error=name_error,
            cwd_error=cwd_error,
            tool_error=tool_error,
        )

    def validate_rename(self, current_name: str, requested_name: str) -> RenameValidation:
        record = self._managed_record(current_name)
        normalized = ""
        name_error = ""
        try:
            normalized = normalized_session_name(record.tool, requested_name)
        except WsError as error:
            name_error = str(error)
        if (
            normalized
            and normalized != current_name
            and (self.backend.session_exists(normalized) or self.store.load(normalized) is not None)
        ):
            name_error = f"session already exists: {normalized}"
        return RenameValidation(normalized_name=normalized, name_error=name_error)

    def list_presets(self) -> list[Preset]:
        return sorted(self.preset_store.load_all().values(), key=lambda preset: preset.name)

    def list_filter_presets(self) -> list[FilterPreset]:
        return sorted(self.filter_preset_store.load_all().values(), key=lambda preset: preset.name)

    def get_filter_preset(self, name: str) -> FilterPreset:
        preset = self.filter_preset_store.load(name)
        if preset is None:
            raise PresetNotFoundError(f"filter preset not found: {name}")
        return preset

    def save_filter_preset(self, preset: FilterPreset) -> FilterPreset:
        normalized_name = slugify_name(preset.name)
        if not normalized_name:
            raise WsError(f"invalid filter preset name: {preset.name!r}")
        saved = preset.model_copy(update={"name": normalized_name})
        self.filter_preset_store.save(saved)
        return saved

    def delete_filter_preset(self, name: str) -> None:
        if self.filter_preset_store.load(name) is None:
            raise PresetNotFoundError(f"filter preset not found: {name}")
        self.filter_preset_store.delete(name)

    def timeline(self, session_name: str, *, limit: int = 50) -> list[SessionTimelineEvent]:
        return self.timeline_store.list(session_name, limit=limit)

    def append_timeline(self, session_name: str, action: str, detail: str = "") -> None:
        with contextlib.suppress(StateError, OSError):
            self.timeline_store.append(
                session_name,
                SessionTimelineEvent(action=action, detail=detail),
            )

    def _push_undo(self, action: str, payload: dict[str, object], *, ttl_minutes: int = 10) -> None:
        with contextlib.suppress(StateError, OSError):
            self.undo_store.push(
                UndoEntry(
                    action=action,
                    payload=payload,
                    expires_at=utc_now() + timedelta(minutes=max(1, ttl_minutes)),
                )
            )

    def undo_last(self) -> str:
        with self._guarded_action("undo"):
            entry = self.undo_store.pop(now=utc_now())
            if entry is None:
                raise WsError("no undo entry available")
            payload = entry.payload
            if entry.action == "stop-session":
                name = str(payload.get("name", ""))
                if not name:
                    raise WsError("invalid undo payload for stop-session")
                self.restart(name)
                return f"restored session runtime: {name}"
            if entry.action == "remove-metadata":
                raw = payload.get("metadata")
                if not isinstance(raw, dict):
                    raise WsError("invalid undo payload for remove-metadata")
                record = SessionMetadata.model_validate(raw)
                self.store.save(record)
                if self.backend.session_exists(record.name):
                    self.backend.set_option(
                        record.name,
                        "@wf_owner",
                        "workspace-session-manager",
                        expected_id=record.tmux_session_id,
                    )
                return f"restored metadata: {record.name}"
            if entry.action == "delete-logs":
                name = str(payload.get("name", ""))
                content = str(payload.get("content", ""))
                record = self._managed_record(name, require_live=False)
                path = self._log_path(record)
                self._prepare_log(path)
                path.write_text(content, encoding="utf-8")
                return f"restored logs: {name}"
            if entry.action == "organize-pin":
                name = str(payload.get("name", ""))
                previous = payload.get("previous_pinned")
                if not isinstance(previous, bool):
                    raise WsError("invalid undo payload for organize-pin")
                self.organize(name, pinned=previous, record_undo=False)
                return f"restored pin state: {name}"
            if entry.action == "organize-status":
                name = str(payload.get("name", ""))
                previous_task = str(payload.get("previous_task_state", "")).strip().lower()
                previous_input = str(payload.get("previous_input_state", "")).strip().lower()
                if not previous_task or not previous_input:
                    raise WsError("invalid undo payload for organize-status")
                self.organize(
                    name,
                    state=TaskState(previous_task),
                    input_state=InputState(previous_input),
                    record_undo=False,
                )
                return f"restored status state: {name}"
            if entry.action == "set-logging":
                name = str(payload.get("name", ""))
                previous_enabled = payload.get("previous_enabled")
                if not isinstance(previous_enabled, bool):
                    raise WsError("invalid undo payload for set-logging")
                self.set_logging(name, previous_enabled, record_undo=False)
                return f"restored logging mode: {name}"
            if entry.action == "stop-command":
                name = str(payload.get("name", ""))
                if not name:
                    raise WsError("invalid undo payload for stop-command")
                self.restart(name)
                return f"restored runtime after interrupt: {name}"
            raise WsError(f"undo for action not supported: {entry.action}")

    def get_preset(self, name: str) -> Preset:
        preset = self.preset_store.load(name)
        if preset is None:
            raise PresetNotFoundError(f"preset not found: {name}")
        return preset

    def save_preset(
        self,
        name: str,
        *,
        tool: Tool,
        cwd: Path,
        project: str = "",
        tags: Sequence[str] = (),
        logging_enabled: bool = True,
    ) -> Preset:
        normalized_name = slugify_name(name)
        if not normalized_name:
            raise WsError(f"invalid preset name: {name!r}")
        preset = Preset(
            name=normalized_name,
            tool=tool,
            cwd=cwd,
            project=project,
            tags=list(tags),
            logging_enabled=logging_enabled,
        )
        self.preset_store.save(preset)
        return preset

    def delete_preset(self, name: str) -> None:
        if self.preset_store.load(name) is None:
            raise PresetNotFoundError(f"preset not found: {name}")
        self.preset_store.delete(name)

    def list_templates(self) -> list[SessionTemplate]:
        return sorted(self.template_store.load_all().values(), key=lambda template: template.name)

    def get_template(self, name: str) -> SessionTemplate:
        template = self.template_store.load(name)
        if template is None:
            raise PresetNotFoundError(f"template not found: {name}")
        return template

    def save_template(self, template: SessionTemplate) -> SessionTemplate:
        normalized_name = slugify_name(template.name)
        if not normalized_name:
            raise WsError(f"invalid template name: {template.name!r}")
        saved = template.model_copy(update={"name": normalized_name})
        self.template_store.save(saved)
        return saved

    def delete_template(self, name: str) -> None:
        if self.template_store.load(name) is None:
            raise PresetNotFoundError(f"template not found: {name}")
        self.template_store.delete(name)

    def render_template(self, template: str, variables: dict[str, str]) -> str:
        pattern = re.compile(r"\{([a-zA-Z0-9_-]+)\}")

        def replace_match(match: re.Match[str]) -> str:
            key = match.group(1)
            return variables.get(key, "")

        rendered = pattern.sub(replace_match, template).strip()
        return rendered

    def create(self, request: CreateRequest, *, dry_run: bool = False) -> SessionView:
        with self._guarded_action("create"):
            detected_project = request.project or self.detect_project(request.cwd)
            default_tool, default_tags, default_task, default_logging = self.project_default(
                detected_project
            )
            effective_request = request
            if (
                detected_project != request.project
                or default_tags
                or default_task
                or default_logging is not None
                or default_tool is not None
            ):
                merged_tags = list(dict.fromkeys([*request.tags, *default_tags]))
                merged_note = request.note or default_task
                logging_enabled = (
                    request.logging_enabled if default_logging is None else default_logging
                )
                effective_request = request.model_copy(
                    update={
                        "project": detected_project,
                        "tags": merged_tags,
                        "note": merged_note,
                        "logging_enabled": logging_enabled,
                        "tool": default_tool or request.tool,
                    }
                )
            validation = self.validate_create(
                effective_request.tool,
                effective_request.name,
                effective_request.cwd,
                automatic_prefix=effective_request.automatic_prefix,
            )
            if not validation.valid or validation.cwd is None:
                message = validation.errors[0] if validation.errors else "invalid session request"
                if message.startswith("session already exists"):
                    raise SessionExistsError(message)
                if (
                    message.startswith("command not found")
                    or "disabled in configuration" in message
                ):
                    raise ToolUnavailableError(message)
                raise WsError(message)
            name = validation.normalized_name
            cwd = validation.cwd
            profile = self.config.tools[effective_request.tool]
            shell_profile = self.config.tools[Tool.SHELL]
            if not profile.enabled:
                raise ToolUnavailableError(
                    f"{effective_request.tool.value} is disabled in configuration"
                )
            if not command_available(profile.command):
                raise ToolUnavailableError(f"command not found: {profile.command[0]}")
            if not command_available(shell_profile.command):
                raise ToolUnavailableError(f"shell command not found: {shell_profile.command[0]}")

            if dry_run:
                now = utc_now()
                return SessionView(
                    name=name,
                    display_name=effective_request.display_name or effective_request.name.strip(),
                    session_id="dry-run",
                    tool=effective_request.tool,
                    cwd=cwd,
                    current_command=Path(profile.command[0]).name,
                    runtime=RuntimeState.DETACHED,
                    attached=False,
                    attached_clients=0,
                    windows=1,
                    created_at=now,
                    project=effective_request.project,
                    note=effective_request.note,
                    tags=effective_request.tags,
                    task_state=effective_request.task_state,
                    input_state=effective_request.input_state,
                    owned=True,
                    logging_enabled=effective_request.logging_enabled,
                    last_active_at=now,
                )

            agent_command = None if effective_request.tool is Tool.SHELL else profile.command
            session = self.backend.create_session(
                name=name,
                cwd=cwd,
                shell_command=shell_profile.command,
                agent_command=agent_command,
            )
            record = SessionMetadata(
                tmux_session_id=session.session_id,
                name=name,
                display_name=effective_request.display_name or effective_request.name.strip(),
                tool=effective_request.tool,
                cwd=cwd,
                project=effective_request.project,
                note=effective_request.note,
                tags=effective_request.tags,
                task_state=effective_request.task_state,
                input_state=effective_request.input_state,
            )
            log_path = self._log_path(record)
            try:
                if effective_request.logging_enabled:
                    self._prepare_log(log_path)
                    self.backend.set_logging(name, log_path, expected_id=session.session_id)
                self.store.save_new(record)
            except Exception:
                self.backend.kill_session(name, expected_id=session.session_id)
                self._delete_log_path(log_path)
                raise
            self.append_timeline(name, "created", f"{record.tool.value} {record.cwd}")
            self.emit_hook(
                "session.created", {"name": name, "tool": record.tool.value, "cwd": str(record.cwd)}
            )
            return self.get(name)

    def _log_path(self, record: SessionMetadata) -> Path:
        return self.paths.logs_dir / f"{record.record_id}.log"

    def _prepare_log(self, path: Path) -> None:
        self.paths.logs_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.paths.logs_dir, 0o700)
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags, 0o600)
        try:
            details = os.fstat(descriptor)
            if not stat.S_ISREG(details.st_mode) or details.st_uid != os.getuid():
                raise StateError("refusing unsafe log file")
            os.fchmod(descriptor, 0o600)
        finally:
            os.close(descriptor)

    def _delete_log_path(self, path: Path) -> None:
        if not path.exists():
            return
        if path.is_symlink():
            raise StateError(f"refusing symlinked log: {path.name}")
        details = path.stat()
        if not stat.S_ISREG(details.st_mode) or details.st_uid != os.getuid():
            raise StateError(f"refusing unsafe log: {path.name}")
        try:
            path.unlink()
        except OSError as error:
            raise StateError(f"unable to delete log for {path.name}: {error}") from error

    def _read_log(self, name: str, max_lines: int, max_bytes: int) -> BoundedOutput:
        record = self.store.load(name)
        if record is None:
            return bounded_output("", max_lines=max_lines, max_bytes=max_bytes)
        path = self._log_path(record)
        if not path.exists() or path.is_symlink():
            return bounded_output("", max_lines=max_lines, max_bytes=max_bytes)
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(path, flags)
            try:
                details = os.fstat(descriptor)
                if not stat.S_ISREG(details.st_mode) or details.st_uid != os.getuid():
                    raise StateError(f"refusing unsafe log: {path.name}")
                os.lseek(descriptor, max(0, details.st_size - max_bytes * 2), os.SEEK_SET)
                content = os.read(descriptor, max_bytes * 2).decode("utf-8", errors="replace")
            finally:
                os.close(descriptor)
        except OSError as error:
            raise StateError(f"unable to read log for {name}: {error}") from error
        return bounded_output(content, max_lines=max_lines, max_bytes=max_bytes)

    def _saved_log_available(self, name: str) -> bool:
        record = self.store.load(name)
        if record is None:
            return False
        path = self._log_path(record)
        if path.is_symlink():
            return False
        try:
            details = path.stat()
        except FileNotFoundError:
            return False
        except OSError as error:
            raise StateError(f"unable to inspect log for {name}: {error}") from error
        return stat.S_ISREG(details.st_mode) and details.st_uid == os.getuid()

    def tail_log(self, name: str, offset: int, *, max_bytes: int | None = None) -> TailResult:
        """Read content appended to a session's saved log since `offset`.

        `stream_to_log` (log_sink.py) already redacts/sanitizes each line as
        it's written, so a plain incremental read is safe here; rotation
        (log_sink.py's `_rotate`) can shrink the file out from under a stale
        offset, which this detects (`size < offset`) and reports as
        `rotated=True` with a fresh bounded tail instead of a negative read.
        """
        bytes_cap = max_bytes or self.config.log_bytes
        record = self.store.load(name)
        if record is None:
            return TailResult(text="", offset=0, rotated=False)
        path = self._log_path(record)
        if not path.exists() or path.is_symlink():
            return TailResult(text="", offset=0, rotated=False)
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(path, flags)
            try:
                details = os.fstat(descriptor)
                if not stat.S_ISREG(details.st_mode) or details.st_uid != os.getuid():
                    raise StateError(f"refusing unsafe log: {path.name}")
                size = details.st_size
                if size < offset:
                    start = max(0, size - bytes_cap)
                    os.lseek(descriptor, start, os.SEEK_SET)
                    content = os.read(descriptor, bytes_cap).decode("utf-8", errors="replace")
                    bounded = bounded_output(
                        content, max_lines=self.config.log_lines, max_bytes=bytes_cap
                    )
                    return TailResult(
                        text=bounded.text,
                        offset=size,
                        rotated=True,
                        truncated=bounded.truncated,
                    )
                new_bytes = size - offset
                if new_bytes <= 0:
                    return TailResult(text="", offset=offset, rotated=False)
                read_size = min(new_bytes, bytes_cap)
                start = size - read_size
                os.lseek(descriptor, start, os.SEEK_SET)
                content = os.read(descriptor, read_size).decode("utf-8", errors="replace")
                return TailResult(
                    text=redact_text(content),
                    offset=size,
                    rotated=False,
                    truncated=start > offset,
                )
            finally:
                os.close(descriptor)
        except OSError as error:
            raise StateError(f"unable to tail log for {name}: {error}") from error

    def search_logs(
        self,
        query: str,
        sessions: Sequence[SessionView],
        *,
        context_lines: int = SEARCH_CONTEXT_LINES,
        max_matches: int = SEARCH_MAX_MATCHES_PER_SESSION,
        read_cap_bytes: int = SEARCH_READ_CAP_BYTES,
    ) -> LogSearchSummary:
        """Search each session's saved log for `query`, newest bytes first.

        Sessions with no captured output (never logged, or the log file is
        missing/unsafe) are skipped and counted rather than erroring -- a
        cross-session search should degrade gracefully, not fail because one
        session has nothing to search.
        """
        needle = query.strip().casefold()
        if not needle:
            return LogSearchSummary(results=(), skipped_no_log=0)
        results: list[LogSearchResult] = []
        skipped = 0
        for session in sessions:
            record = self.store.load(session.name)
            if record is None:
                skipped += 1
                continue
            path = self._log_path(record)
            if not path.exists() or path.is_symlink():
                skipped += 1
                continue
            try:
                details = path.stat()
                if not stat.S_ISREG(details.st_mode) or details.st_uid != os.getuid():
                    skipped += 1
                    continue
                with path.open("rb") as handle:
                    handle.seek(max(0, details.st_size - read_cap_bytes))
                    raw = handle.read()
            except OSError:
                skipped += 1
                continue
            content = redact_text(raw.decode("utf-8", errors="replace"))
            lines = content.splitlines()
            matches: list[LogSearchMatch] = []
            for index, line in enumerate(lines):
                if needle not in line.casefold():
                    continue
                before = tuple(lines[max(0, index - context_lines) : index])
                after = tuple(lines[index + 1 : index + 1 + context_lines])
                matches.append(
                    LogSearchMatch(
                        line_number=index + 1,
                        line=line,
                        context_before=before,
                        context_after=after,
                    )
                )
                if len(matches) >= max_matches:
                    break
            if matches:
                results.append(
                    LogSearchResult(
                        name=session.name,
                        display_name=session.display_name or session.name,
                        matches=tuple(matches),
                    )
                )
        return LogSearchSummary(results=tuple(results), skipped_no_log=skipped)

    def _managed_record(self, name: str, *, require_live: bool = False) -> SessionMetadata:
        record = self.store.load(name)
        if record is None:
            raise OwnershipError(
                f"refusing to modify {name}: it was not created by this ws installation"
            )
        try:
            session = self.backend.get_session(name)
        except SessionNotFoundError:
            if require_live:
                raise
            return record
        marker = self.backend.get_option(name, "@wf_owner", expected_id=session.session_id)
        if record.tmux_session_id != session.session_id or marker != "workspace-session-manager":
            raise OwnershipError(
                f"refusing to modify {name}: it was not created by this ws installation"
            )
        return record

    def _owned_record(self, name: str) -> SessionMetadata:
        return self._managed_record(name, require_live=True)

    def attach(self, name: str) -> int:
        session = self.get(name)
        if session.runtime in {RuntimeState.STOPPED, RuntimeState.FAILED}:
            # The tmux session is gone but the metadata record survives; revive it
            # in place (same name/cwd/tool, preserving note/tags/history) instead of
            # failing with "session not found", so a stopped session can be
            # continued with a single attach/resume call.
            self.restart(name)
        record = self._owned_record(name)
        self.store.save(
            record.model_copy(update={"last_attached_at": utc_now(), "updated_at": utc_now()})
        )
        self.append_timeline(name, "attach")
        return self.backend.attach(name, expected_id=record.tmux_session_id)

    def resume_target(self) -> SessionView:
        sessions = self.list_sessions()
        detached = [session for session in sessions if session.runtime is RuntimeState.DETACHED]
        live = [
            session
            for session in sessions
            if session.runtime in {RuntimeState.ATTACHED, RuntimeState.DETACHED}
        ]
        candidates = detached or live
        if not candidates:
            raise SessionNotFoundError("no tmux sessions are available")
        return candidates[0]

    def update_note(self, name: str, note: str) -> SessionView:
        with self._guarded_action("update-note"):
            if len(note) > 2000:
                raise WsError("note cannot exceed 2000 characters")
            record = self._managed_record(name)
            self.store.save(record.model_copy(update={"note": note, "updated_at": utc_now()}))
            self.append_timeline(name, "task-updated", note[:160])
            self.emit_hook("session.note_updated", {"name": name, "note": note[:200]})
            return self.get(name)

    def organize(
        self,
        name: str,
        *,
        display_name: str | None = None,
        tags: list[str] | None = None,
        state: TaskState | None = None,
        input_state: InputState | None = None,
        project: str | None = None,
        pinned: bool | None = None,
        record_undo: bool = True,
    ) -> SessionView:
        with self._guarded_action("organize"):
            record = self._managed_record(name)
            previous = {
                "pinned": record.pinned,
                "task_state": record.task_state.value,
                "input_state": record.input_state.value,
            }
            updates: dict[str, object] = {"updated_at": utc_now()}
            if display_name is not None:
                updates["display_name"] = display_name
            if tags is not None:
                updates["tags"] = tags
            if state is not None:
                updates["task_state"] = state
            if input_state is not None:
                updates["input_state"] = input_state
            if project is not None:
                updates["project"] = project
            if pinned is not None:
                updates["pinned"] = pinned
            updated = SessionMetadata.model_validate(record.model_copy(update=updates).model_dump())
            self.store.save(updated)
            if record_undo and "pinned" in updates and record.pinned != updated.pinned:
                self._push_undo(
                    "organize-pin",
                    {
                        "name": name,
                        "previous_pinned": previous["pinned"],
                    },
                )
            if (
                record_undo
                and ("task_state" in updates or "input_state" in updates)
                and (
                    record.task_state != updated.task_state
                    or record.input_state != updated.input_state
                )
            ):
                self._push_undo(
                    "organize-status",
                    {
                        "name": name,
                        "previous_task_state": previous["task_state"],
                        "previous_input_state": previous["input_state"],
                    },
                )
            self.append_timeline(name, "organized", ", ".join(sorted(updates.keys())))
            self.emit_hook("session.organized", {"name": name, "fields": sorted(updates.keys())})
            return self.get(name)

    def rename(self, old_name: str, requested_name: str) -> SessionView:
        with self._guarded_action("rename"):
            record = self._managed_record(old_name)
            new_name = normalized_session_name(record.tool, requested_name)
            if old_name == new_name:
                return self.get(old_name)
            if self.backend.session_exists(new_name) or self.store.load(new_name) is not None:
                raise SessionExistsError(f"session already exists: {new_name}")
            live = self.backend.session_exists(old_name)
            if live:
                self.backend.rename_session(old_name, new_name, expected_id=record.tmux_session_id)
            updated = record.model_copy(update={"name": new_name, "updated_at": utc_now()})
            try:
                self.store.replace(old_name, updated)
            except StateError:
                if live:
                    self.backend.rename_session(
                        new_name, old_name, expected_id=record.tmux_session_id
                    )
                raise
            self.append_timeline(new_name, "renamed", f"{old_name} -> {new_name}")
            self.emit_hook("session.renamed", {"old_name": old_name, "new_name": new_name})
            return self.get(new_name)

    def delete(self, name: str) -> None:
        with self._guarded_action("delete"):
            record = self._managed_record(name)
            if self.backend.session_exists(name):
                if self.get(name).logging_enabled:
                    self.backend.set_logging(name, None, expected_id=record.tmux_session_id)
                self.backend.kill_session(name, expected_id=record.tmux_session_id)
            self.store.delete(name)
            self._delete_log_path(self._log_path(record))
            self.append_timeline(name, "deleted")
            self.emit_hook("session.deleted", {"name": name})

    def stop_command(self, name: str) -> None:
        with self._guarded_action("stop-command"):
            record = self._owned_record(name)
            self._push_undo("stop-command", {"name": name})
            self.backend.send_interrupt(name, expected_id=record.tmux_session_id)
            self.append_timeline(name, "command-stopped")
            self.emit_hook("session.command_stopped", {"name": name})

    def stop_session(self, name: str) -> SessionView:
        with self._guarded_action("stop-session"):
            record = self._owned_record(name)
            session = self.get(name)
            self._push_undo("stop-session", {"name": name})
            if session.logging_enabled:
                self.backend.set_logging(name, None, expected_id=record.tmux_session_id)
            self.backend.kill_session(name, expected_id=record.tmux_session_id)
            self.store.save(record.model_copy(update={"updated_at": utc_now()}))
            self.append_timeline(name, "session-stopped")
            self.emit_hook("session.stopped", {"name": name})
            return self.get(name)

    def restart(self, name: str) -> SessionView:
        with self._guarded_action("restart"):
            record = self._managed_record(name)
            profile = self.config.tools[record.tool]
            shell_profile = self.config.tools[Tool.SHELL]
            if not profile.enabled:
                raise ToolUnavailableError(f"{record.tool.value} is disabled in configuration")
            if not command_available(profile.command):
                raise ToolUnavailableError(f"command not found: {profile.command[0]}")
            if not command_available(shell_profile.command):
                raise ToolUnavailableError(f"shell command not found: {shell_profile.command[0]}")
            agent_command = None if record.tool is Tool.SHELL else profile.command
            if self.backend.session_exists(name):
                was_logging = self.get(name).logging_enabled
                if was_logging:
                    self.backend.set_logging(name, None, expected_id=record.tmux_session_id)
                try:
                    self.backend.restart_session(
                        name,
                        record.cwd,
                        shell_profile.command,
                        agent_command,
                        expected_id=record.tmux_session_id,
                    )
                except Exception:
                    if was_logging:
                        self.backend.set_logging(
                            name,
                            self._log_path(record),
                            expected_id=record.tmux_session_id,
                        )
                    raise
                if was_logging:
                    self.backend.set_logging(
                        name,
                        self._log_path(record),
                        expected_id=record.tmux_session_id,
                    )
            else:
                session = self.backend.create_session(
                    name=name,
                    cwd=record.cwd,
                    shell_command=shell_profile.command,
                    agent_command=agent_command,
                )
                record = record.model_copy(
                    update={"tmux_session_id": session.session_id, "updated_at": utc_now()}
                )
                try:
                    self.store.save(record)
                except Exception:
                    self.backend.kill_session(name, expected_id=session.session_id)
                    raise
            self.append_timeline(name, "restarted")
            self.emit_hook("session.restarted", {"name": name})
            return self.get(name)

    def set_logging(self, name: str, enabled: bool, *, record_undo: bool = True) -> SessionView:
        with self._guarded_action("set-logging"):
            record = self._owned_record(name)
            current = self.get(name)
            path = self._log_path(record)
            if enabled:
                self._prepare_log(path)
                self.backend.set_logging(name, path, expected_id=record.tmux_session_id)
            else:
                self.backend.set_logging(name, None, expected_id=record.tmux_session_id)
            if record_undo and current.logging_enabled != enabled:
                self._push_undo(
                    "set-logging",
                    {
                        "name": name,
                        "previous_enabled": current.logging_enabled,
                    },
                )
            self.append_timeline(name, "logging", "enabled" if enabled else "disabled")
            self.emit_hook("session.logging_updated", {"name": name, "enabled": enabled})
            return self.get(name)

    def delete_logs(self, name: str) -> SessionView:
        with self._guarded_action("delete-logs"):
            record = self._managed_record(name)
            live = self.backend.session_exists(name)
            was_logging = live and self.get(name).logging_enabled
            existing = ""
            with contextlib.suppress(Exception):
                existing = self._read_log(
                    name, self.config.log_lines * 4, self.config.log_bytes * 4
                ).text
            self._push_undo("delete-logs", {"name": name, "content": existing}, ttl_minutes=30)
            if was_logging:
                self.backend.set_logging(name, None, expected_id=record.tmux_session_id)
            self._delete_log_path(self._log_path(record))
            if was_logging:
                path = self._log_path(record)
                self._prepare_log(path)
                self.backend.set_logging(name, path, expected_id=record.tmux_session_id)
            self.append_timeline(name, "logs-cleared")
            self.emit_hook("session.logs_cleared", {"name": name})
            return self.get(name)

    def remove_metadata(self, name: str) -> None:
        with self._guarded_action("remove-metadata"):
            record = self._managed_record(name)
            self._push_undo(
                "remove-metadata",
                {"metadata": record.model_dump(mode="json")},
                ttl_minutes=30,
            )
            live = self.backend.session_exists(name)
            was_logging = live and self.get(name).logging_enabled
            if live:
                if was_logging:
                    self.backend.set_logging(name, None, expected_id=record.tmux_session_id)
                self.backend.unset_option(name, "@wf_owner", expected_id=record.tmux_session_id)
            try:
                self.store.delete(name)
            except StateError:
                if live:
                    self.backend.set_option(
                        name,
                        "@wf_owner",
                        "workspace-session-manager",
                        expected_id=record.tmux_session_id,
                    )
                    if was_logging:
                        self.backend.set_logging(
                            name,
                            self._log_path(record),
                            expected_id=record.tmux_session_id,
                        )
                raise
            self.append_timeline(name, "metadata-removed")
            self.emit_hook("session.metadata_removed", {"name": name})

    def doctor(self) -> DoctorReport:
        checks: list[HealthCheck] = []
        try:
            checks.append(
                HealthCheck(name="tmux", status=HealthStatus.PASS, detail=self.backend.version())
            )
        except WsError as error:
            checks.append(
                HealthCheck(
                    name="tmux",
                    status=HealthStatus.FAIL,
                    detail=str(error),
                    corrective_action="Install tmux or verify the configured tmux socket.",
                )
            )

        for tool, profile in self.config.tools.items():
            available = command_available(profile.command)
            status = HealthStatus.PASS if available else HealthStatus.WARN
            detail = shutil.which(profile.command[0]) or f"not found: {profile.command[0]}"
            checks.append(
                HealthCheck(
                    name=f"tool:{tool.value}",
                    status=status,
                    detail=detail,
                    corrective_action="Install the tool or disable its profile in config.toml."
                    if not available
                    else "",
                )
            )

        errors = self.store.validation_errors()
        checks.append(
            HealthCheck(
                name="state",
                status=HealthStatus.FAIL if errors else HealthStatus.PASS,
                detail="; ".join(errors) if errors else str(self.paths.state_dir),
                corrective_action="Repair or remove the invalid owner-only metadata file."
                if errors
                else "",
            )
        )
        unmanaged = sum(not session.owned for session in self.list_sessions(include_unmanaged=True))
        checks.append(
            HealthCheck(
                name="unmanaged-sessions",
                status=HealthStatus.INFO,
                detail=f"{unmanaged} hidden from managed views",
            )
        )
        readable_legacy = [
            str(path) for path in self.config.legacy_state_dirs if path.expanduser().is_dir()
        ]
        checks.append(
            HealthCheck(
                name="legacy-readonly",
                status=HealthStatus.INFO,
                detail=", ".join(readable_legacy) or "no legacy state directories found",
            )
        )
        disk_root = (
            self.paths.state_dir if self.paths.state_dir.exists() else self.paths.state_dir.parent
        )
        checks.append(
            disk_space_check(
                disk_root,
                warn_percent=self.config.health.disk_warn_percent,
                fail_percent=self.config.health.disk_fail_percent,
            )
        )
        return DoctorReport(checks=checks)

    def export_doctor_report(self, report: DoctorReport) -> Path:
        self.paths.diagnostics_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.paths.diagnostics_dir, 0o700)
        destination = (
            self.paths.diagnostics_dir / f"ws-diagnostics-{utc_now():%Y%m%d-%H%M%S-%f}.txt"
        )
        lines = ["ws privacy-safe diagnostics", f"Generated: {utc_now().isoformat()}", ""]
        for check in report.checks:
            detail = redact_text(check.detail)
            lines.append(f"{check.status.value.upper():<4} {check.name}: {detail}")
            if check.corrective_action:
                lines.append(f"     Action: {check.corrective_action}")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(destination, flags, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write("\n".join(lines))
            stream.write("\n")
        return destination

    def correlate_output(self, *, limit_sessions: int = 50) -> dict[str, list[str]]:
        sessions = self.list_sessions()[: max(1, limit_sessions)]
        grouped: dict[str, set[str]] = {}
        ignored = re.compile(
            r"(?i)^(?:tokens?|context|model|working directory|approval|session id|"
            r"[-=]{3,}|[>$#]\s*)"
        )
        for session in sessions:
            try:
                details = self.inspect(session.name)
            except WsError:
                continue
            lines = [
                line.strip()
                for line in details.preview.splitlines()
                if line.strip() and not ignored.match(line.strip())
            ]
            seen: set[str] = set()
            for line in lines[-12:]:
                normalized = re.sub(r"\s+", " ", line)
                normalized = re.sub(r"\b[0-9a-f]{7,}\b", "<id>", normalized, flags=re.IGNORECASE)
                normalized = re.sub(r"\d+", "<n>", normalized)
                key = normalized[:140]
                if key in seen:
                    continue
                seen.add(key)
                grouped.setdefault(key, set()).add(session.name)
        return {line: sorted(names) for line, names in grouped.items() if len(names) >= 2}

    def auto_handoff(
        self,
        *,
        project: str = "",
        include_timeline: int = 5,
    ) -> dict[str, object]:
        sessions = self.list_sessions()
        if project:
            sessions = [session for session in sessions if session.project == project]
        items: list[dict[str, object]] = []
        for session in sessions:
            details = self.inspect_snapshot(session)
            timeline = [
                event.model_dump(mode="json")
                for event in self.timeline(session.name, limit=include_timeline)
            ]
            items.append(
                {
                    "name": session.name,
                    "display_name": session.display_name,
                    "project": session.project,
                    "runtime": session.runtime.value,
                    "task": session.task_state.value,
                    "input": session.input_state.value,
                    "summary": (details.preview.splitlines()[-1] if details.preview else ""),
                    "timeline": timeline,
                }
            )
        return {
            "generated_at": utc_now().isoformat(),
            "project": project,
            "sessions": items,
        }

    def export_incident_bundle(
        self,
        *,
        sessions: Sequence[str] = (),
        project: str = "",
        timeline_limit: int = 20,
        audit_limit: int = 400,
        include_federation: bool = False,
        federation_hosts: Sequence[str] = (),
        destination: Path | None = None,
    ) -> Path:
        selected = self.list_sessions()
        if project:
            selected = [session for session in selected if session.project == project]
        if sessions:
            by_name = {session.name: session for session in selected}
            missing = [name for name in sessions if name not in by_name]
            if missing:
                raise WsError(f"sessions not found for incident bundle: {', '.join(missing)}")
            unique_names = tuple(dict.fromkeys(sessions))
            selected = [by_name[name] for name in unique_names]
        if not selected:
            raise WsError("no sessions matched incident bundle filters")

        health = self.refresh_health_alerts(force=True)
        audit_lines = self.read_audit(limit=max(1, audit_limit))
        timeline_rows = max(1, timeline_limit)
        per_session_payload: list[dict[str, object]] = []
        for session in selected:
            details = self.inspect_snapshot(session)
            per_session_payload.append(
                {
                    "session": session.model_dump(mode="json"),
                    "timeline": [
                        event.model_dump(mode="json")
                        for event in self.timeline(session.name, limit=timeline_rows)
                    ],
                    "recent_output": details.preview,
                    "recent_output_truncated": details.preview_truncated,
                }
            )

        self.paths.diagnostics_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.paths.diagnostics_dir, 0o700)
        target = destination or (
            self.paths.diagnostics_dir
            / f"ws-incident-bundle-{datetime.now().astimezone().strftime('%Y%m%d-%H%M%S')}.tar.gz"
        )
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        generated_at = utc_now().isoformat()
        warnings = [
            check for check in health if check.status in {HealthStatus.WARN, HealthStatus.FAIL}
        ]
        federation_payload: dict[str, object] = {
            "enabled": include_federation,
            "hosts": list(federation_hosts),
            "sessions": [],
            "health": [],
            "report": [],
            "errors": [],
        }
        if include_federation:
            selected_hosts = list(federation_hosts)
            try:
                federation_payload["sessions"] = self.federated_sessions(selected_hosts)
            except (WsError, OSError) as error:
                federation_payload["errors"] = [f"sessions: {error}"]
                federation_payload["sessions"] = []
            for action in ("health", "report"):
                try:
                    federation_payload[action] = self.federated_action(
                        action,
                        hosts=selected_hosts,
                    )
                except (WsError, OSError) as error:
                    error_rows = federation_payload.get("errors")
                    errors = list(error_rows) if isinstance(error_rows, list) else []
                    errors.append(f"{action}: {error}")
                    federation_payload["errors"] = errors
                    federation_payload[action] = []
        manifest = {
            "generated_at": generated_at,
            "project": project,
            "session_count": len(selected),
            "sessions": [session.name for session in selected],
            "health_warning_count": len(warnings),
            "audit_line_count": len(audit_lines),
            "timeline_limit": timeline_rows,
            "federation_enabled": include_federation,
            "federation_hosts": list(federation_hosts),
            "federation_error_count": len(federation_payload["errors"]),
        }

        def add_tar_text(archive: tarfile.TarFile, arcname: str, text: str) -> None:
            content = text.encode("utf-8")
            info = tarfile.TarInfo(name=arcname)
            info.size = len(content)
            info.mode = 0o600
            archive.addfile(info, io.BytesIO(content))

        with tarfile.open(target, "w:gz") as archive:
            add_tar_text(archive, "manifest.json", json.dumps(manifest, indent=2))
            add_tar_text(
                archive,
                "health.json",
                json.dumps(
                    [check.model_dump(mode="json") for check in health],
                    indent=2,
                ),
            )
            add_tar_text(
                archive, "audit.log", "\n".join(audit_lines) + ("\n" if audit_lines else "")
            )
            if include_federation:
                add_tar_text(
                    archive,
                    "federation/sessions.json",
                    json.dumps(federation_payload["sessions"], indent=2),
                )
                add_tar_text(
                    archive,
                    "federation/health.json",
                    json.dumps(federation_payload["health"], indent=2),
                )
                add_tar_text(
                    archive,
                    "federation/report.json",
                    json.dumps(federation_payload["report"], indent=2),
                )
                add_tar_text(
                    archive,
                    "federation/errors.json",
                    json.dumps(federation_payload["errors"], indent=2),
                )
            for item in per_session_payload:
                session_obj = item["session"]
                assert isinstance(session_obj, dict)
                raw_name = str(session_obj.get("name", "session"))
                safe_name = re.sub(r"[^a-zA-Z0-9_.-]+", "_", raw_name)
                add_tar_text(
                    archive,
                    f"sessions/{safe_name}.json",
                    json.dumps(item, indent=2),
                )
        os.chmod(target, 0o600)
        self.append_audit("incident.bundle", f"{target} sessions={len(selected)}")
        return target

    def _incidents_payload(self) -> dict[str, object]:
        return self._read_json_object(self.paths.incidents_file, default={"incidents": []})

    def _incident_rows(self) -> list[dict[str, object]]:
        payload = self._incidents_payload()
        raw = payload.get("incidents", [])
        if not isinstance(raw, list):
            return []
        rows = [item for item in raw if isinstance(item, dict)]
        return sorted(rows, key=lambda item: str(item.get("updated_at", "")), reverse=True)

    def list_incidents(self, *, status: str = "") -> list[dict[str, object]]:
        selected_status = status.strip().lower()
        rows = self._incident_rows()
        if not selected_status:
            return rows
        return [
            row for row in rows if str(row.get("status", "")).strip().lower() == selected_status
        ]

    def get_incident(self, incident_id: str) -> dict[str, object]:
        normalized = slugify_name(incident_id)
        if not normalized:
            raise WsError(f"invalid incident id: {incident_id!r}")
        row = next(
            (item for item in self._incident_rows() if str(item.get("id", "")) == normalized), None
        )
        if row is None:
            raise WsError(f"incident not found: {incident_id}")
        return row

    def start_incident(
        self,
        *,
        title: str,
        severity: str = "warn",
        owner: str = "",
        summary: str = "",
        sessions: Sequence[str] = (),
        hosts: Sequence[str] = (),
    ) -> dict[str, object]:
        with self._guarded_action("incident-start"):
            normalized_title = title.strip()
            if not normalized_title:
                raise WsError("incident title cannot be empty")
            normalized_severity = severity.strip().lower()
            if normalized_severity not in INCIDENT_ALLOWED_SEVERITIES:
                raise WsError(f"unsupported incident severity: {severity}")
            incident_id = slugify_name(f"{normalized_title}-{uuid4().hex[:8]}")
            now = utc_now().isoformat()
            row = {
                "id": incident_id,
                "title": normalized_title,
                "severity": normalized_severity,
                "status": "open",
                "owner": owner.strip() or getpass.getuser(),
                "summary": summary.strip(),
                "sessions": sorted({str(name).strip() for name in sessions if str(name).strip()}),
                "hosts": sorted({str(host).strip() for host in hosts if str(host).strip()}),
                "resolution": "",
                "created_at": now,
                "updated_at": now,
                "events": [
                    {
                        "timestamp": now,
                        "action": "incident.start",
                        "detail": summary.strip() or "incident opened",
                    }
                ],
            }
            rows = self._incident_rows()
            rows.append(row)
            self._write_json_object(self.paths.incidents_file, {"incidents": rows})
            self.append_audit("incident.start", f"{incident_id} severity={normalized_severity}")
            return row

    def update_incident(
        self,
        incident_id: str,
        *,
        owner: str | None = None,
        summary: str | None = None,
        sessions: Sequence[str] | None = None,
        hosts: Sequence[str] | None = None,
        note: str = "",
    ) -> dict[str, object]:
        with self._guarded_action("incident-update"):
            normalized = slugify_name(incident_id)
            if not normalized:
                raise WsError(f"invalid incident id: {incident_id!r}")
            rows = self._incident_rows()
            selected: dict[str, object] | None = None
            for row in rows:
                if str(row.get("id", "")) == normalized:
                    selected = row
                    break
            if selected is None:
                raise WsError(f"incident not found: {incident_id}")
            if owner is not None:
                selected["owner"] = owner.strip()
            if summary is not None:
                selected["summary"] = summary.strip()
            if sessions is not None:
                selected["sessions"] = sorted(
                    {str(name).strip() for name in sessions if str(name).strip()}
                )
            if hosts is not None:
                selected["hosts"] = sorted(
                    {str(host).strip() for host in hosts if str(host).strip()}
                )
            updated_at = utc_now().isoformat()
            selected["updated_at"] = updated_at
            events_raw = selected.get("events", [])
            events = events_raw if isinstance(events_raw, list) else []
            if note.strip():
                events.append(
                    {
                        "timestamp": updated_at,
                        "action": "incident.update",
                        "detail": note.strip(),
                    }
                )
            selected["events"] = events
            self._write_json_object(self.paths.incidents_file, {"incidents": rows})
            self.append_audit("incident.update", normalized)
            return selected

    def close_incident(self, incident_id: str, *, resolution: str = "") -> dict[str, object]:
        with self._guarded_action("incident-close"):
            normalized = slugify_name(incident_id)
            if not normalized:
                raise WsError(f"invalid incident id: {incident_id!r}")
            rows = self._incident_rows()
            selected: dict[str, object] | None = None
            for row in rows:
                if str(row.get("id", "")) == normalized:
                    selected = row
                    break
            if selected is None:
                raise WsError(f"incident not found: {incident_id}")
            updated_at = utc_now().isoformat()
            selected["status"] = "closed"
            selected["resolution"] = resolution.strip()
            selected["updated_at"] = updated_at
            events_raw = selected.get("events", [])
            events = events_raw if isinstance(events_raw, list) else []
            events.append(
                {
                    "timestamp": updated_at,
                    "action": "incident.close",
                    "detail": resolution.strip() or "resolved",
                }
            )
            selected["events"] = events
            self._write_json_object(self.paths.incidents_file, {"incidents": rows})
            self.append_audit("incident.close", normalized)
            return selected

    def backup_state(self, destination: Path | None = None) -> Path:
        self.paths.backups_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.paths.backups_dir, 0o700)
        target = destination or (
            self.paths.backups_dir
            / f"ws-backup-{datetime.now().astimezone().strftime('%Y%m%d-%H%M%S')}.tar.gz"
        )
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with tarfile.open(target, "w:gz") as archive:
            for item in (
                self.paths.sessions_dir,
                self.paths.logs_dir,
                self.paths.timeline_dir,
                self.paths.presets_file,
                self.paths.filter_presets_file,
                self.paths.templates_file,
                self.paths.interface_preferences_file,
                self.paths.incidents_file,
            ):
                if item.exists():
                    archive.add(item, arcname=item.name)
        os.chmod(target, 0o600)
        return target

    def restore_state(self, archive_path: Path) -> None:
        with self._guarded_action("restore-state"):
            if not archive_path.exists() or archive_path.is_symlink():
                raise WsError(f"backup archive not found: {archive_path}")
            self.paths.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(self.paths.state_dir, 0o700)
            with tarfile.open(archive_path, "r:gz") as archive:
                for member in archive.getmembers():
                    if member.isdev():
                        continue
                    if ".." in Path(member.name).parts:
                        continue
                    archive.extract(member, path=self.paths.state_dir, filter="data")

    def federated_sessions(self, hosts: Sequence[str] | None = None) -> list[dict[str, object]]:
        configured_hosts = list(hosts or self.config.federation.hosts)
        if not configured_hosts:
            return []
        rows: list[dict[str, object]] = []
        ssh_prefix = list(self.config.federation.ssh_command)
        remote_command = list(self.config.federation.remote_ws_command)
        timeout = self.config.federation.timeout_seconds
        for host in configured_hosts:
            command = [*ssh_prefix, host, *remote_command]
            try:
                result = subprocess.run(  # noqa: S603
                    command,
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                )
            except (OSError, subprocess.SubprocessError) as error:
                rows.append({"host": host, "error": str(error), "sessions": []})
                continue
            if result.returncode != 0:
                rows.append(
                    {
                        "host": host,
                        "error": result.stderr.strip() or f"exit {result.returncode}",
                        "sessions": [],
                    }
                )
                continue
            try:
                data = json.loads(result.stdout)
            except ValueError:
                rows.append(
                    {"host": host, "error": "invalid JSON from remote host", "sessions": []}
                )
                continue
            rows.append(
                {"host": host, "error": "", "sessions": data if isinstance(data, list) else []}
            )
        return rows

    def federated_action(
        self,
        action: str,
        *,
        hosts: Sequence[str] | None = None,
        args: Sequence[str] = (),
        approval_code: str = "",
        approval_tokens: Sequence[str] = (),
        retry_attempts: int | None = None,
        retry_delay_seconds: float | None = None,
    ) -> list[dict[str, object]]:
        self._enforce_action_allowed("federation-action")
        configured_hosts = list(hosts or self.config.federation.hosts)
        if not configured_hosts:
            return []
        rows: list[dict[str, object]] = []
        allowed = {"list", "resume", "health", "report", "attach"}
        if action not in allowed:
            raise WsError(f"unsupported federation action: {action}")
        if action in FEDERATED_GUARDED_ACTIONS:
            self._require_federated_action_approval(
                action,
                configured_hosts,
                code=approval_code,
                approval_tokens=approval_tokens,
            )
        timeout = self.config.federation.timeout_seconds
        max_attempts = max(
            1,
            retry_attempts
            if retry_attempts is not None
            else self.config.federation.action_retry_attempts,
        )
        retry_delay = (
            retry_delay_seconds
            if retry_delay_seconds is not None
            else self.config.federation.action_retry_delay_seconds
        )
        for host in configured_hosts:
            command = [
                *self.config.federation.ssh_command,
                host,
                "ws",
                action,
                *args,
                *(("--json",) if action in {"list", "health", "report"} else ()),
            ]
            attempts: list[str] = []
            last_stdout = ""
            last_error = ""
            ok = False
            for attempt in range(1, max_attempts + 1):
                try:
                    result = subprocess.run(  # noqa: S603
                        command,
                        check=False,
                        capture_output=True,
                        text=True,
                        timeout=timeout,
                    )
                except (OSError, subprocess.SubprocessError) as error:
                    reason = str(error)
                    attempts.append(f"attempt {attempt}: {reason}")
                    last_error = reason
                    if attempt < max_attempts and retry_delay > 0:
                        time.sleep(retry_delay)
                    continue
                last_stdout = result.stdout.strip()
                if result.returncode == 0:
                    ok = True
                    attempts.append(f"attempt {attempt}: ok")
                    last_error = ""
                    break
                reason = result.stderr.strip() or f"exit {result.returncode}"
                attempts.append(f"attempt {attempt}: {reason}")
                last_error = reason
                if attempt < max_attempts and retry_delay > 0:
                    time.sleep(retry_delay)

            summary = (
                f"failed after {max_attempts} attempt(s): {last_error or 'unknown error'}"
                if not ok
                else ""
            )
            rows.append(
                {
                    "host": host,
                    "ok": ok,
                    "error": "" if ok else last_error,
                    "stdout": last_stdout,
                    "attempts": attempts,
                    "attempt_count": len(attempts),
                    "failure_summary": summary,
                    "retried": len(attempts) > 1,
                }
            )
        return rows

    def federation_action_plan(
        self,
        action: str,
        *,
        hosts: Sequence[str] | None = None,
        args: Sequence[str] = (),
        approval_code: str = "",
        approval_tokens: Sequence[str] = (),
    ) -> dict[str, object]:
        configured_hosts = list(hosts or self.config.federation.hosts)
        allowed = {"list", "resume", "health", "report", "attach"}
        if action not in allowed:
            raise WsError(f"unsupported federation action: {action}")
        rows = self.federated_sessions(configured_hosts or None)
        metrics = self._federation_host_metrics(rows)
        selected_hosts = configured_hosts or sorted(metrics)
        host_details: list[dict[str, object]] = []
        total_sessions = 0
        blocked_sessions = 0
        needs_input_sessions = 0
        reachable_hosts = 0
        approval_contexts: list[str] = []
        approval_missing_contexts: list[str] = []
        for host in selected_hosts:
            row = metrics.get(host, {})
            if not isinstance(row, dict):
                continue
            reachable = bool(row.get("reachable"))
            session_count = int(row.get("session_count", 0))
            blocked = int(row.get("blocked", 0))
            needs_input = int(row.get("needs_input", 0))
            total_sessions += session_count
            blocked_sessions += blocked
            needs_input_sessions += needs_input
            if reachable:
                reachable_hosts += 1
            host_details.append(
                {
                    "host": host,
                    "reachable": reachable,
                    "session_count": session_count,
                    "blocked": blocked,
                    "needs_input": needs_input,
                    "error": str(row.get("error", "")),
                }
            )
            if action in FEDERATED_GUARDED_ACTIONS:
                approval_contexts.append(f"federation-action:{action}:{host}")
        if action in FEDERATED_GUARDED_ACTIONS:
            approval_contexts.extend((f"federation-action:{action}", "federation-action"))
        dedup_contexts = [context for context in dict.fromkeys(approval_contexts)]

        approval_required = False
        approval_satisfied = True
        for context in dedup_contexts:
            requirement = self.approval_requirement(context)
            if bool(requirement.get("required")):
                approval_required = True
                if not self.evaluate_approval(
                    context,
                    code=approval_code,
                    approval_tokens=approval_tokens,
                ):
                    approval_satisfied = False
                    approval_missing_contexts.append(context)

        risk_score = 1
        if action in {"resume", "attach"}:
            risk_score += 2
        if blocked_sessions > 0:
            risk_score += 1
        if len(selected_hosts) >= 3:
            risk_score += 1
        risk_level = "low" if risk_score <= 1 else "medium" if risk_score <= 3 else "high"

        return {
            "action": action,
            "args": [str(arg) for arg in args],
            "targets": {
                "hosts": selected_hosts,
                "host_count": len(selected_hosts),
                "reachable_hosts": reachable_hosts,
                "unreachable_hosts": max(0, len(selected_hosts) - reachable_hosts),
            },
            "blast_radius": {
                "session_count": total_sessions,
                "blocked_sessions": blocked_sessions,
                "needs_input_sessions": needs_input_sessions,
                "risk_score": risk_score,
                "risk_level": risk_level,
            },
            "approval": {
                "required": approval_required,
                "satisfied": approval_satisfied,
                "contexts": dedup_contexts,
                "missing_contexts": approval_missing_contexts,
            },
            "hosts": host_details,
        }

    def _require_federated_action_approval(
        self,
        action: str,
        hosts: Sequence[str],
        *,
        code: str,
        approval_tokens: Sequence[str] = (),
    ) -> None:
        for host in hosts:
            contexts = (
                f"federation-action:{action}:{host}",
                f"federation-action:{action}",
                "federation-action",
            )
            for context in contexts:
                if not self.evaluate_approval(context, code=code, approval_tokens=approval_tokens):
                    raise WsError(
                        "approval required for remote action "
                        f"{action} on host {host}; pass --approval <code> or --approval-token "
                        f"(matched guard: {context})"
                    )

    def _remote_ws_probe(self, host: str, *remote_args: str) -> dict[str, object]:
        command = [
            *self.config.federation.ssh_command,
            host,
            self.config.federation.remote_ws_command[0],
            *remote_args,
        ]
        timeout = self.config.federation.timeout_seconds
        try:
            result = subprocess.run(  # noqa: S603
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except (OSError, subprocess.SubprocessError) as error:
            return {
                "ok": False,
                "stdout": "",
                "stderr": "",
                "returncode": None,
                "error": str(error),
            }
        return {
            "ok": result.returncode == 0,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "returncode": result.returncode,
            "error": ""
            if result.returncode == 0
            else (result.stderr.strip() or f"exit {result.returncode}"),
        }

    def federation_capability_matrix(
        self, hosts: Sequence[str] | None = None
    ) -> list[dict[str, object]]:
        configured_hosts = list(hosts or self.config.federation.hosts)
        if not configured_hosts:
            return []
        session_rows = {
            str(row.get("host", "")).strip(): row
            for row in self.federated_sessions(configured_hosts)
            if str(row.get("host", "")).strip()
        }
        rows: list[dict[str, object]] = []
        for host in configured_hosts:
            session_row = session_rows.get(
                host, {"host": host, "error": "host unavailable", "sessions": []}
            )
            command_probe = self._remote_ws_probe(host, "--help")
            command_help = str(command_probe.get("stdout", ""))
            command_caps: dict[str, dict[str, object]] = {}
            for action in FEDERATED_CAPABILITY_ACTIONS:
                supported = bool(re.search(rf"(?m)^\s*{re.escape(action)}\b", command_help))
                reason = ""
                if not bool(command_probe.get("ok")):
                    reason = str(command_probe.get("error", "failed to probe commands"))
                    supported = False
                elif not supported:
                    reason = "command not listed in remote ws --help"
                command_caps[action] = {"supported": supported, "reason": reason}

            tool_caps: dict[str, dict[str, object]] = {}
            for tool in Tool:
                tool_caps[tool.value] = {
                    "supported": False,
                    "status": "unknown",
                    "reason": "not reported by remote doctor",
                }
            doctor_probe = self._remote_ws_probe(host, "doctor", "--json")
            if bool(doctor_probe.get("ok")):
                try:
                    doctor_payload = json.loads(str(doctor_probe.get("stdout", "")))
                except ValueError:
                    doctor_payload = {}
                checks = doctor_payload.get("checks") if isinstance(doctor_payload, dict) else None
                if isinstance(checks, list):
                    for item in checks:
                        if not isinstance(item, dict):
                            continue
                        name = str(item.get("name", ""))
                        if not name.startswith("tool:"):
                            continue
                        tool_name = name.split(":", 1)[1].strip()
                        if tool_name not in tool_caps:
                            continue
                        status = str(item.get("status", "unknown")).lower() or "unknown"
                        detail = str(item.get("detail", "")).strip()
                        supported = status == "pass"
                        tool_caps[tool_name] = {
                            "supported": supported,
                            "status": status,
                            "reason": "" if supported else (detail or f"status={status}"),
                        }
                else:
                    reason = "invalid doctor response"
                    for tool_name in tool_caps:
                        tool_caps[tool_name] = {
                            "supported": False,
                            "status": "unknown",
                            "reason": reason,
                        }
            else:
                reason = str(doctor_probe.get("error", "doctor probe failed"))
                for tool_name in tool_caps:
                    tool_caps[tool_name] = {
                        "supported": False,
                        "status": "unknown",
                        "reason": reason,
                    }

            rows.append(
                {
                    "host": host,
                    "session_error": str(session_row.get("error", "")),
                    "session_count": len(session_row.get("sessions", []))
                    if isinstance(session_row.get("sessions"), list)
                    else 0,
                    "commands": command_caps,
                    "tools": tool_caps,
                }
            )
        return rows

    def project_board(self, *, project: str = "") -> dict[str, list[SessionView]]:
        sessions = self.list_sessions()
        if project:
            sessions = [session for session in sessions if session.project == project]
        board = {"todo": [], "doing": [], "blocked": [], "done": []}
        for session in sessions:
            if session.task_state is TaskState.COMPLETED:
                board["done"].append(session)
            elif session.task_state is TaskState.BLOCKED:
                board["blocked"].append(session)
            elif session.task_state in {TaskState.WAITING, TaskState.NEEDS_INPUT}:
                board["todo"].append(session)
            else:
                board["doing"].append(session)
        return board

    def archive_completed_sessions(self, *, dry_run: bool | None = None) -> list[str]:
        policy = self.config.archive_policy
        if not policy.enabled:
            return []
        execute_dry_run = policy.dry_run_default if dry_run is None else dry_run
        if not execute_dry_run:
            self._enforce_action_allowed("archive")
        now = utc_now()
        archived: list[str] = []
        threshold = timedelta(days=policy.completed_days)
        with self._guarded_action("archive") if not execute_dry_run else contextlib.nullcontext():
            for session in self.list_sessions():
                if session.task_state is not TaskState.COMPLETED:
                    continue
                anchor = session.last_active_at or session.created_at
                if anchor is None or (now - anchor) < threshold:
                    continue
                archived.append(session.name)
                if execute_dry_run:
                    continue
                with contextlib.suppress(WsError):
                    self.stop_session(session.name)
                with contextlib.suppress(WsError):
                    self.remove_metadata(session.name)
                if policy.include_logs:
                    with contextlib.suppress(WsError):
                        self.delete_logs(session.name)
        return archived

    def suggest_fixes(self, output: str) -> list[str]:
        rules: tuple[tuple[re.Pattern[str], str], ...] = (
            (
                re.compile(r"session limit|usage limit", re.IGNORECASE),
                "Wait for quota reset, then run ws resume.",
            ),
            (
                re.compile(r"command not found", re.IGNORECASE),
                "Verify configured tool command in config.toml and PATH.",
            ),
            (
                re.compile(r"permission denied", re.IGNORECASE),
                "Check executable and file permissions for the working directory.",
            ),
            (
                re.compile(r"no such file|not found", re.IGNORECASE),
                "Confirm cwd/project path exists before restarting the session.",
            ),
            (
                re.compile(r"timed out|timeout", re.IGNORECASE),
                "Retry with stable network or reduce workload; consider ws report for triage.",
            ),
        )
        suggestions: list[str] = []
        for pattern, hint in rules:
            if pattern.search(output):
                suggestions.append(hint)
        return suggestions

    def global_timeline(
        self,
        *,
        limit_per_session: int = 30,
        action_filter: str = "",
    ) -> list[tuple[str, SessionTimelineEvent]]:
        rows: list[tuple[str, SessionTimelineEvent]] = []
        if not self.paths.timeline_dir.exists():
            return rows
        for path in sorted(self.paths.timeline_dir.glob("*.json")):
            name = path.stem
            events = self.timeline(name, limit=limit_per_session)
            for event in events:
                if action_filter and action_filter not in event.action:
                    continue
                rows.append((name, event))
        rows.sort(key=lambda item: item[1].timestamp, reverse=True)
        return rows

    def emit_hook(self, event: str, payload: dict[str, object]) -> None:
        hooks = [hook for hook in self.config.automation_hooks if hook.event == event]
        if not hooks:
            return
        data = json.dumps(payload, ensure_ascii=True)
        for hook in hooks:
            try:
                self.runner(
                    (*hook.command, event, data),
                    capture=True,
                    timeout=hook.timeout_seconds,
                )
            except (OSError, RuntimeError, subprocess.SubprocessError, ValueError):
                continue

    def integrity_report(self) -> dict[str, list[str]]:
        errors: dict[str, list[str]] = {
            "sessions": self.store.validation_errors(),
            "presets": [],
            "filter_presets": [],
            "templates": [],
            "timeline": [],
        }
        for key, path in (
            ("presets", self.paths.presets_file),
            ("filter_presets", self.paths.filter_presets_file),
            ("templates", self.paths.templates_file),
        ):
            if not path.exists() or path.is_symlink():
                continue
            try:
                json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as error:
                errors[key].append(str(error))
        if self.paths.timeline_dir.exists():
            for path in sorted(self.paths.timeline_dir.glob("*.json")):
                if path.is_symlink():
                    errors["timeline"].append(f"{path.name}: symlink not allowed")
                    continue
                try:
                    json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError) as error:
                    errors["timeline"].append(f"{path.name}: {error}")
        return errors

    def repair_integrity(self) -> dict[str, list[str]]:
        with self._guarded_action("recover-repair"):
            report = self.integrity_report()
            repaired: dict[str, list[str]] = {key: [] for key in report}
            for key, path in (
                ("presets", self.paths.presets_file),
                ("filter_presets", self.paths.filter_presets_file),
                ("templates", self.paths.templates_file),
            ):
                if not report[key]:
                    continue
                with contextlib.suppress(OSError):
                    backup = path.with_suffix(path.suffix + ".corrupt")
                    if path.exists():
                        path.replace(backup)
                    path.write_text("{}\n", encoding="utf-8")
                    repaired[key].append(str(path))
            if report["timeline"] and self.paths.timeline_dir.exists():
                for path in sorted(self.paths.timeline_dir.glob("*.json")):
                    if path.name in {item.split(":", 1)[0] for item in report["timeline"]}:
                        with contextlib.suppress(OSError):
                            path.unlink()
                            repaired["timeline"].append(path.name)
            return repaired

    def _read_json_object(
        self, path: Path, *, default: dict[str, object] | None = None
    ) -> dict[str, object]:
        if not path.exists():
            return dict(default or {})
        if path.is_symlink():
            return dict(default or {})
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            return loaded if isinstance(loaded, dict) else dict(default or {})
        except (OSError, ValueError):
            return dict(default or {})

    def _write_json_object(self, path: Path, payload: dict[str, object]) -> None:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(path.parent, 0o700)
        temporary_name: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=path.parent,
                prefix=f".{path.name}.",
                delete=False,
            ) as temporary:
                temporary_name = temporary.name
                temporary.write(json.dumps(payload, indent=2))
                temporary.write("\n")
                temporary.flush()
                os.fsync(temporary.fileno())
            os.chmod(temporary_name, 0o600)
            os.replace(temporary_name, path)
        except OSError as error:
            if temporary_name:
                Path(temporary_name).unlink(missing_ok=True)
            raise WsError(f"unable to write {path.name}: {error}") from error

    def append_audit(self, action: str, detail: str = "", *, operator: str = "") -> None:
        timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
        actor = operator or getpass.getuser()
        line = f"{timestamp}\t{actor}\t{action}\t{detail}\n"
        self.paths.audit_log_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.paths.audit_log_file.parent, 0o700)
        with open(self.paths.audit_log_file, "a", encoding="utf-8") as handle:
            handle.write(line)
        with contextlib.suppress(OSError):
            os.chmod(self.paths.audit_log_file, 0o600)

    def read_audit(self, *, limit: int = 200) -> list[str]:
        path = self.paths.audit_log_file
        if not path.exists() or path.is_symlink():
            return []
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        return lines[-max(1, limit) :]

    def select_operator_profile(self, name: str) -> dict[str, object]:
        profile = next((item for item in self.config.operator_profiles if item.name == name), None)
        if profile is None:
            raise WsError(f"operator profile not found: {name}")
        payload = {
            "name": profile.name,
            "default_project": profile.default_project,
            "require_approvals": profile.require_approvals,
            "allowed_actions": list(profile.allowed_actions),
        }
        self._write_json_object(self.paths.state_dir / "active-profile.json", payload)
        self.append_audit("profile.selected", profile.name)
        return payload

    def active_operator_profile(self) -> dict[str, object]:
        return self._read_json_object(self.paths.state_dir / "active-profile.json", default={})

    def _approval_secret(self) -> str:
        return self.config.approvals.token_signing_secret.strip()

    def _approval_sign_payload(self, payload: str) -> str:
        secret = self._approval_secret()
        if not secret:
            raise WsError("approval token signing secret is not configured")
        digest = hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).digest()
        return urlsafe_b64encode(digest).decode("utf-8").rstrip("=")

    def issue_approval_token(
        self,
        *,
        action: str,
        operator: str = "",
        ttl_seconds: int | None = None,
    ) -> dict[str, object]:
        normalized = str(action).strip()
        if not normalized:
            raise WsError("approval token action cannot be empty")
        ttl = ttl_seconds if ttl_seconds is not None else self.config.approvals.token_ttl_seconds
        ttl = max(30, min(int(ttl), 86_400))
        issued_at = utc_now()
        expires_at = issued_at + timedelta(seconds=ttl)
        payload = {
            "action": normalized,
            "operator": (operator or getpass.getuser()).strip() or "unknown",
            "issued_at": issued_at.isoformat(),
            "expires_at": expires_at.isoformat(),
            "nonce": uuid4().hex,
        }
        serialized = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        signature = self._approval_sign_payload(serialized)
        token = (
            urlsafe_b64encode(serialized.encode("utf-8")).decode("utf-8").rstrip("=")
            + "."
            + signature
        )
        return {
            "token": token,
            "action": payload["action"],
            "operator": payload["operator"],
            "issued_at": payload["issued_at"],
            "expires_at": payload["expires_at"],
            "ttl_seconds": ttl,
        }

    def _decode_approval_token(self, token: str) -> dict[str, object] | None:
        parts = token.strip().split(".", 1)
        if len(parts) != 2:
            return None
        payload_b64, signature = parts
        padding = "=" * ((4 - len(payload_b64) % 4) % 4)
        try:
            payload_raw = urlsafe_b64decode((payload_b64 + padding).encode("utf-8")).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return None
        try:
            expected = self._approval_sign_payload(payload_raw)
        except WsError:
            return None
        if not hmac.compare_digest(signature, expected):
            return None
        try:
            payload = json.loads(payload_raw)
        except ValueError:
            return None
        return payload if isinstance(payload, dict) else None

    @staticmethod
    def _approval_action_matches(required_action: str, token_action: str) -> bool:
        if token_action == "*":  # noqa: S105
            return True
        if required_action == token_action:
            return True
        return required_action.startswith(f"{token_action}:")

    def _valid_approval_operators(self, action: str, approval_tokens: Sequence[str]) -> set[str]:
        operators: set[str] = set()
        now = utc_now()
        for token in approval_tokens:
            payload = self._decode_approval_token(token)
            if not payload:
                continue
            token_action = str(payload.get("action", "")).strip()
            if not token_action or not self._approval_action_matches(action, token_action):
                continue
            expires_at_raw = str(payload.get("expires_at", "")).strip()
            with contextlib.suppress(ValueError):
                expires_at = datetime.fromisoformat(expires_at_raw)
                if expires_at.tzinfo is None:
                    expires_at = expires_at.replace(tzinfo=UTC)
                if expires_at < now:
                    continue
                operator = str(payload.get("operator", "")).strip() or "unknown"
                operators.add(operator)
        return operators

    def approval_requirement(self, action: str) -> dict[str, object]:
        active = self.active_operator_profile()
        requires_profile = bool(active.get("require_approvals"))
        requires_config = self.config.approvals.enabled and action in set(
            self.config.approvals.guarded_actions
        )
        required = requires_profile or requires_config
        dual = self.config.approvals.dual_control_enabled and action in set(
            self.config.approvals.dual_control_actions
        )
        return {
            "required": required,
            "requires_profile": requires_profile,
            "requires_config": requires_config,
            "dual_control": dual,
            "minimum_approvers": 2 if dual else 1,
        }

    def evaluate_approval(
        self,
        action: str,
        *,
        code: str = "",
        approval_tokens: Sequence[str] = (),
    ) -> bool:
        requirement = self.approval_requirement(action)
        if not bool(requirement.get("required")):
            return True
        active = self.active_operator_profile()
        code_valid = True
        if active.get("require_approvals"):
            profile = next(
                (item for item in self.config.operator_profiles if item.name == active.get("name")),
                None,
            )
            if profile and profile.approval_code and code != profile.approval_code:
                code_valid = False
        if (
            self.config.approvals.enabled
            and action in self.config.approvals.guarded_actions
            and self.config.approvals.code
            and code != self.config.approvals.code
        ):
            code_valid = False
        valid_operators = self._valid_approval_operators(action, approval_tokens)
        minimum_approvers = int(requirement.get("minimum_approvers", 1))
        if bool(requirement.get("dual_control")):
            return len(valid_operators) >= minimum_approvers
        return code_valid or len(valid_operators) >= minimum_approvers

    def policy_simulation(self) -> dict[str, object]:
        sessions = self.list_sessions()
        archive_candidates = self.archive_completed_sessions(dry_run=True)
        checks = self.refresh_health_alerts(force=True)
        sla = next((check for check in checks if check.name == "sla-alerts"), None)
        approvals = {
            "enabled": self.config.approvals.enabled,
            "guarded_actions": list(self.config.approvals.guarded_actions),
            "dual_control_enabled": self.config.approvals.dual_control_enabled,
            "dual_control_actions": list(self.config.approvals.dual_control_actions),
        }
        return {
            "sessions_total": len(sessions),
            "archive_candidates": archive_candidates,
            "sla": sla.model_dump(mode="json") if sla else None,
            "approvals": approvals,
        }

    def dependency_graph(self) -> dict[str, list[str]]:
        payload = self._read_json_object(self.paths.dependencies_file, default={"edges": {}})
        raw_edges = payload.get("edges", {})
        if not isinstance(raw_edges, dict):
            return {}
        graph: dict[str, list[str]] = {}
        for key, value in raw_edges.items():
            if isinstance(value, list):
                graph[str(key)] = [str(item) for item in value]
        return graph

    def add_dependency(self, session: str, depends_on: str) -> None:
        with self._guarded_action("dependency-add"):
            graph = self.dependency_graph()
            values = set(graph.get(session, []))
            values.add(depends_on)
            graph[session] = sorted(values)
            self._write_json_object(self.paths.dependencies_file, {"edges": graph})
            self.append_audit("dependency.added", f"{session}->{depends_on}")

    def remove_dependency(self, session: str, depends_on: str) -> None:
        with self._guarded_action("dependency-remove"):
            graph = self.dependency_graph()
            values = set(graph.get(session, []))
            values.discard(depends_on)
            graph[session] = sorted(values)
            self._write_json_object(self.paths.dependencies_file, {"edges": graph})
            self.append_audit("dependency.removed", f"{session}->{depends_on}")

    def dependency_critical_path(self) -> list[str]:
        graph = self.dependency_graph()
        memo: dict[str, list[str]] = {}
        visiting: set[str] = set()

        def walk(node: str) -> list[str]:
            if node in memo:
                return memo[node]
            if node in visiting:
                return [node]
            visiting.add(node)
            best = [node]
            for parent in graph.get(node, []):
                candidate = [*walk(parent), node]
                if len(candidate) > len(best):
                    best = candidate
            visiting.discard(node)
            memo[node] = best
            return best

        best_path: list[str] = []
        for node in graph:
            candidate = walk(node)
            if len(candidate) > len(best_path):
                best_path = candidate
        return best_path

    def rebuild_search_index(self) -> dict[str, list[str]]:
        index: dict[str, list[str]] = {}
        for session in self.list_sessions():
            tokens: list[str] = []
            tokens.extend((session.name, session.display_name, session.project, session.note))
            for event in self.timeline(session.name, limit=30):
                tokens.append(f"{event.action} {event.detail}")
            with contextlib.suppress(WsError):
                preview = self.inspect_snapshot(session).preview
                if preview:
                    tokens.append(preview)
            index[session.name] = [token for token in tokens if token]
        self._write_json_object(self.paths.search_index_file, {"index": index})
        return index

    def save_search_query(self, name: str, query: str) -> dict[str, str]:
        with self._guarded_action("search-query-save"):
            normalized = slugify_name(name)
            if not normalized:
                raise WsError(f"invalid search query name: {name!r}")
            text = query.strip()
            if not text:
                raise WsError("search query cannot be empty")
            payload = self._read_json_object(
                self.paths.search_queries_file, default={"queries": {}}
            )
            raw = payload.get("queries", {})
            queries = raw if isinstance(raw, dict) else {}
            queries[normalized] = text
            self._write_json_object(self.paths.search_queries_file, {"queries": queries})
            self.append_audit("search-query.saved", f"{normalized}: {text[:120]}")
            return {"name": normalized, "query": text}

    def list_search_queries(self) -> list[dict[str, str]]:
        payload = self._read_json_object(self.paths.search_queries_file, default={"queries": {}})
        raw = payload.get("queries", {})
        if not isinstance(raw, dict):
            return []
        rows = [
            {"name": str(name), "query": str(query)}
            for name, query in raw.items()
            if str(name).strip() and str(query).strip()
        ]
        return sorted(rows, key=lambda item: item["name"])

    def delete_search_query(self, name: str) -> None:
        with self._guarded_action("search-query-delete"):
            normalized = slugify_name(name)
            payload = self._read_json_object(
                self.paths.search_queries_file, default={"queries": {}}
            )
            raw = payload.get("queries", {})
            queries = raw if isinstance(raw, dict) else {}
            if normalized not in queries:
                raise WsError(f"search query not found: {name}")
            del queries[normalized]
            self._write_json_object(self.paths.search_queries_file, {"queries": queries})
            self.append_audit("search-query.deleted", normalized)

    def get_search_query(self, name: str) -> str:
        normalized = slugify_name(name)
        rows = {row["name"]: row["query"] for row in self.list_search_queries()}
        if normalized not in rows:
            raise WsError(f"search query not found: {name}")
        return rows[normalized]

    def save_federation_dashboard(
        self,
        *,
        name: str,
        hosts: Sequence[str],
        host_filter: str = "",
        scope_all_hosts: bool = False,
        safe_mode: bool = False,
    ) -> dict[str, object]:
        with self._guarded_action("federation-dashboard-save"):
            normalized = slugify_name(name)
            if not normalized:
                raise WsError(f"invalid federation dashboard name: {name!r}")
            selected_hosts = sorted({str(host).strip() for host in hosts if str(host).strip()})
            if not selected_hosts:
                raise WsError("federation dashboard requires at least one host")
            row = {
                "name": normalized,
                "hosts": selected_hosts,
                "host_filter": host_filter.strip(),
                "scope_all_hosts": bool(scope_all_hosts),
                "safe_mode": bool(safe_mode),
                "updated_at": utc_now().isoformat(),
            }
            payload = self._read_json_object(
                self.paths.federation_dashboards_file,
                default={"dashboards": {}},
            )
            raw = payload.get("dashboards", {})
            dashboards = raw if isinstance(raw, dict) else {}
            dashboards[normalized] = row
            self._write_json_object(
                self.paths.federation_dashboards_file,
                {"dashboards": dashboards},
            )
            self.append_audit(
                "federation-dashboard.saved",
                f"{normalized} hosts={len(selected_hosts)}",
            )
            return row

    def list_federation_dashboards(self) -> list[dict[str, object]]:
        payload = self._read_json_object(
            self.paths.federation_dashboards_file,
            default={"dashboards": {}},
        )
        raw = payload.get("dashboards", {})
        if not isinstance(raw, dict):
            return []
        rows: list[dict[str, object]] = []
        for name, value in raw.items():
            if not isinstance(value, dict):
                continue
            hosts_raw = value.get("hosts", [])
            hosts = (
                sorted({str(host).strip() for host in hosts_raw if str(host).strip()})
                if isinstance(hosts_raw, list)
                else []
            )
            if not hosts:
                continue
            rows.append(
                {
                    "name": str(name),
                    "hosts": hosts,
                    "host_filter": str(value.get("host_filter", "")).strip(),
                    "scope_all_hosts": bool(value.get("scope_all_hosts")),
                    "safe_mode": bool(value.get("safe_mode")),
                    "updated_at": str(value.get("updated_at", "")),
                }
            )
        return sorted(rows, key=lambda item: str(item["name"]))

    def get_federation_dashboard(self, name: str) -> dict[str, object]:
        normalized = slugify_name(name)
        if not normalized:
            raise WsError(f"invalid federation dashboard name: {name!r}")
        rows = {str(item["name"]): item for item in self.list_federation_dashboards()}
        selected = rows.get(normalized)
        if selected is None:
            raise WsError(f"federation dashboard not found: {name}")
        return selected

    def delete_federation_dashboard(self, name: str) -> None:
        with self._guarded_action("federation-dashboard-delete"):
            normalized = slugify_name(name)
            if not normalized:
                raise WsError(f"invalid federation dashboard name: {name!r}")
            payload = self._read_json_object(
                self.paths.federation_dashboards_file,
                default={"dashboards": {}},
            )
            raw = payload.get("dashboards", {})
            dashboards = raw if isinstance(raw, dict) else {}
            if normalized not in dashboards:
                raise WsError(f"federation dashboard not found: {name}")
            del dashboards[normalized]
            self._write_json_object(
                self.paths.federation_dashboards_file,
                {"dashboards": dashboards},
            )
            self.append_audit("federation-dashboard.deleted", normalized)

    def _federation_host_metrics(
        self, rows: Sequence[dict[str, object]]
    ) -> dict[str, dict[str, object]]:
        metrics: dict[str, dict[str, object]] = {}
        for row in rows:
            host = str(row.get("host", "")).strip()
            if not host:
                continue
            sessions = row.get("sessions", [])
            safe_sessions = sessions if isinstance(sessions, list) else []
            attached = detached = stopped = blocked = needs_input = 0
            tools: dict[str, int] = {}
            for item in safe_sessions:
                if not isinstance(item, dict):
                    continue
                runtime = str(item.get("runtime", "")).lower()
                task_state = str(item.get("task_state", "")).lower()
                input_state = str(item.get("input_state", "")).lower()
                tool = str(item.get("tool", "")).strip().lower()
                if runtime == RuntimeState.ATTACHED.value:
                    attached += 1
                elif runtime == RuntimeState.DETACHED.value:
                    detached += 1
                elif runtime in {RuntimeState.STOPPED.value, RuntimeState.FAILED.value}:
                    stopped += 1
                if task_state == TaskState.BLOCKED.value:
                    blocked += 1
                if input_state == InputState.REQUIRED.value:
                    needs_input += 1
                if tool:
                    tools[tool] = tools.get(tool, 0) + 1
            metrics[host] = {
                "reachable": not bool(str(row.get("error", "")).strip()),
                "error": str(row.get("error", "")).strip(),
                "session_count": len(safe_sessions),
                "attached": attached,
                "detached": detached,
                "stopped": stopped,
                "blocked": blocked,
                "needs_input": needs_input,
                "tools": tools,
            }
        return metrics

    def save_fleet_snapshot(
        self,
        *,
        name: str,
        hosts: Sequence[str] = (),
    ) -> dict[str, object]:
        with self._guarded_action("fleet-snapshot-save"):
            normalized = slugify_name(name)
            if not normalized:
                raise WsError(f"invalid fleet snapshot name: {name!r}")
            selected_hosts = [str(host).strip() for host in hosts if str(host).strip()]
            rows = self.federated_sessions(selected_hosts or None)
            summary = self._federation_host_metrics(rows)
            snapshot = {
                "name": normalized,
                "created_at": utc_now().isoformat(),
                "hosts": sorted(summary),
                "summary": summary,
            }
            payload = self._read_json_object(
                self.paths.federation_fleet_snapshots_file,
                default={"snapshots": {}},
            )
            raw = payload.get("snapshots", {})
            snapshots = raw if isinstance(raw, dict) else {}
            snapshots[normalized] = snapshot
            self._write_json_object(
                self.paths.federation_fleet_snapshots_file,
                {"snapshots": snapshots},
            )
            self.append_audit("fleet-snapshot.saved", f"{normalized} hosts={len(summary)}")
            return snapshot

    def list_fleet_snapshots(self) -> list[dict[str, object]]:
        payload = self._read_json_object(
            self.paths.federation_fleet_snapshots_file,
            default={"snapshots": {}},
        )
        raw = payload.get("snapshots", {})
        if not isinstance(raw, dict):
            return []
        rows: list[dict[str, object]] = []
        for name, value in raw.items():
            if not isinstance(value, dict):
                continue
            summary = value.get("summary", {})
            hosts = value.get("hosts", [])
            if not isinstance(summary, dict):
                continue
            rows.append(
                {
                    "name": str(name),
                    "created_at": str(value.get("created_at", "")),
                    "hosts": hosts if isinstance(hosts, list) else sorted(summary),
                    "host_count": len(summary),
                }
            )
        return sorted(rows, key=lambda item: str(item.get("created_at", "")), reverse=True)

    def get_fleet_snapshot(self, name: str) -> dict[str, object]:
        normalized = slugify_name(name)
        if not normalized:
            raise WsError(f"invalid fleet snapshot name: {name!r}")
        payload = self._read_json_object(
            self.paths.federation_fleet_snapshots_file,
            default={"snapshots": {}},
        )
        raw = payload.get("snapshots", {})
        snapshots = raw if isinstance(raw, dict) else {}
        snapshot = snapshots.get(normalized)
        if not isinstance(snapshot, dict):
            raise WsError(f"fleet snapshot not found: {name}")
        return snapshot

    def fleet_diff(
        self,
        *,
        left: str,
        right: str = "",
        hosts: Sequence[str] = (),
    ) -> dict[str, object]:
        left_snapshot = self.get_fleet_snapshot(left)
        left_summary = left_snapshot.get("summary", {})
        if not isinstance(left_summary, dict):
            raise WsError(f"invalid fleet snapshot data: {left}")
        if right.strip():
            right_snapshot = self.get_fleet_snapshot(right)
            right_summary = right_snapshot.get("summary", {})
            if not isinstance(right_summary, dict):
                raise WsError(f"invalid fleet snapshot data: {right}")
            right_label = str(right_snapshot.get("name", right))
            right_created = str(right_snapshot.get("created_at", ""))
        else:
            selected_hosts = [str(host).strip() for host in hosts if str(host).strip()]
            rows = self.federated_sessions(selected_hosts or None)
            right_summary = self._federation_host_metrics(rows)
            right_label = "live"
            right_created = utc_now().isoformat()

        selected = sorted(
            {
                host
                for host in (
                    set(left_summary)
                    | set(right_summary)
                    | {str(host).strip() for host in hosts if str(host).strip()}
                )
                if host
            }
        )
        if hosts:
            selected = [
                host
                for host in selected
                if host in {str(h).strip() for h in hosts if str(h).strip()}
            ]

        drifts: list[dict[str, object]] = []
        for host in selected:
            left_row = left_summary.get(host, {})
            right_row = right_summary.get(host, {})
            if not isinstance(left_row, dict) or not isinstance(right_row, dict):
                continue
            for field in (
                "reachable",
                "session_count",
                "attached",
                "detached",
                "stopped",
                "blocked",
                "needs_input",
            ):
                left_value = left_row.get(field)
                right_value = right_row.get(field)
                if left_value == right_value:
                    continue
                drift: dict[str, object] = {
                    "host": host,
                    "field": field,
                    "left": left_value,
                    "right": right_value,
                }
                if isinstance(left_value, (int, float)) and isinstance(right_value, (int, float)):
                    drift["delta"] = right_value - left_value
                drifts.append(drift)
            left_tools = left_row.get("tools", {})
            right_tools = right_row.get("tools", {})
            if isinstance(left_tools, dict) and isinstance(right_tools, dict):
                all_tools = sorted(set(left_tools) | set(right_tools))
                for tool in all_tools:
                    left_count = int(left_tools.get(tool, 0))
                    right_count = int(right_tools.get(tool, 0))
                    if left_count == right_count:
                        continue
                    drifts.append(
                        {
                            "host": host,
                            "field": f"tool:{tool}",
                            "left": left_count,
                            "right": right_count,
                            "delta": right_count - left_count,
                        }
                    )

        host_spread: list[dict[str, object]] = []
        for field in ("session_count", "attached", "detached", "stopped", "blocked", "needs_input"):
            values: dict[str, int] = {}
            for host in selected:
                row = right_summary.get(host, {})
                if not isinstance(row, dict):
                    continue
                value = row.get(field)
                if isinstance(value, int):
                    values[host] = value
            if len(values) < 2:
                continue
            spread = max(values.values()) - min(values.values())
            if spread > 0:
                host_spread.append({"field": field, "spread": spread, "values": values})

        anomalies = self._fleet_diff_anomalies(drifts=drifts, host_spread=host_spread)
        return {
            "left": {
                "name": str(left_snapshot.get("name", left)),
                "created_at": str(left_snapshot.get("created_at", "")),
            },
            "right": {"name": right_label, "created_at": right_created},
            "hosts": selected,
            "drifts": drifts,
            "host_spread": host_spread,
            "anomalies": anomalies,
        }

    def _fleet_diff_anomalies(
        self,
        *,
        drifts: Sequence[dict[str, object]],
        host_spread: Sequence[dict[str, object]],
    ) -> list[dict[str, object]]:
        anomalies: list[dict[str, object]] = []
        spread_threshold = self.config.federation.anomaly_spread_threshold
        delta_threshold = self.config.federation.anomaly_delta_threshold
        for item in host_spread:
            spread = item.get("spread")
            if not isinstance(spread, int) or spread < spread_threshold:
                continue
            anomalies.append(
                {
                    "type": "spread",
                    "field": str(item.get("field", "")),
                    "score": spread,
                    "severity": "high" if spread >= (spread_threshold * 2) else "medium",
                    "reason": f"cross-host spread reached {spread}",
                }
            )
        for item in drifts:
            delta = item.get("delta")
            if not isinstance(delta, (int, float)):
                continue
            absolute = abs(int(delta))
            if absolute < delta_threshold:
                continue
            anomalies.append(
                {
                    "type": "delta",
                    "host": str(item.get("host", "")),
                    "field": str(item.get("field", "")),
                    "score": absolute,
                    "severity": "high" if absolute >= (delta_threshold * 2) else "medium",
                    "reason": f"host delta reached {delta}",
                }
            )
        return sorted(anomalies, key=lambda row: int(row.get("score", 0)), reverse=True)

    def fleet_diff_live(self, *, hosts: Sequence[str] = ()) -> dict[str, object]:
        snapshots = self.list_fleet_snapshots()
        if not snapshots:
            return {"baseline": "", "drifts": [], "host_spread": [], "anomalies": []}
        baseline = str(snapshots[0].get("name", ""))
        if not baseline:
            return {"baseline": "", "drifts": [], "host_spread": [], "anomalies": []}
        payload = self.fleet_diff(left=baseline, hosts=hosts)
        payload["baseline"] = baseline
        return payload

    def _parse_search_facets(self, query: str) -> tuple[dict[str, str], list[str]]:
        facets: dict[str, str] = {}
        terms: list[str] = []
        try:
            tokens = shlex.split(query)
        except ValueError:
            tokens = query.split()
        for token in tokens:
            key, separator, value = token.partition(":")
            if separator and key in {"tool", "project", "state", "tag"} and value.strip():
                facets[key] = value.strip()
            elif token.strip():
                terms.append(token.strip())
        return facets, terms

    def unified_search(self, query: str, *, limit: int = 50) -> list[dict[str, object]]:
        payload = self._read_json_object(self.paths.search_index_file, default={"index": {}})
        raw_index = payload.get("index", {})
        if not isinstance(raw_index, dict) or not raw_index:
            raw_index = self.rebuild_search_index()
        facets, terms = self._parse_search_facets(query.strip())
        if not facets and not terms:
            return []
        sessions = self.list_sessions()
        tool_filter = facets.get("tool", "").casefold()
        project_filter = facets.get("project", "").casefold()
        state_filter = facets.get("state", "").casefold()
        tag_filter = facets.get("tag", "").casefold()
        filtered = [
            session
            for session in sessions
            if (not tool_filter or session.tool.value.casefold() == tool_filter)
            and (not project_filter or session.project.casefold() == project_filter)
            and (
                not state_filter
                or session.task_state.value.casefold() == state_filter
                or session.runtime.value.casefold() == state_filter
            )
            and (not tag_filter or any(item.casefold() == tag_filter for item in session.tags))
        ]
        by_name = {session.name: session for session in sessions}
        normalized_terms = [term.casefold() for term in terms]
        results: list[dict[str, object]] = []
        for session in filtered:
            values = raw_index.get(session.name, [])
            lines = values if isinstance(values, list) else [str(values)]
            matched_line = ""
            if not normalized_terms:
                matched_line = str(lines[0]) if lines else ""
            else:
                for line in lines:
                    text = str(line)
                    lowered = text.casefold()
                    if all(term in lowered for term in normalized_terms):
                        matched_line = text
                        break
            if not matched_line:
                continue
            related: list[str] = []
            for candidate in sessions:
                if candidate.name == session.name:
                    continue
                shared_project = bool(session.project and session.project == candidate.project)
                shared_tags = bool(set(session.tags) & set(candidate.tags))
                if shared_project or shared_tags:
                    related.append(candidate.name)
            results.append(
                {
                    "session": session.name,
                    "match": matched_line[:240],
                    "tool": session.tool.value,
                    "project": session.project,
                    "state": session.task_state.value,
                    "related_sessions": related[:5],
                }
            )
            if len(results) >= limit:
                break
        ordered = sorted(
            results,
            key=lambda row: (
                len(row.get("related_sessions", [])),
                len(str(row.get("match", ""))),
                row.get("session", ""),
            ),
            reverse=True,
        )
        for row in ordered:
            name = str(row.get("session", ""))
            session = by_name.get(name)
            if session is not None and not row.get("related_sessions"):
                row["related_sessions"] = [
                    item.name
                    for item in sessions
                    if item.name != session.name
                    and item.project == session.project
                    and session.project
                ][:5]
        return ordered[:limit]

    def timeline_drill(self, session_name: str, *, action: str = "") -> dict[str, object]:
        events = self.timeline(session_name, limit=100)
        selected = [event for event in events if action in event.action] if action else events
        details = self.inspect(session_name)
        output_lines = details.preview.splitlines()
        return {
            "session": session_name,
            "events": [event.model_dump(mode="json") for event in selected[:20]],
            "output_line_count": len(output_lines),
            "output_tail": output_lines[-20:],
        }

    def list_playbooks(self) -> list[dict[str, object]]:
        configured = [
            {
                "name": item.name,
                "source": "config",
                "match_any": list(item.match_any),
                "commands": [list(command) for command in item.commands],
                "timeout_seconds": item.timeout_seconds,
            }
            for item in self.config.playbooks
        ]
        builtin = [
            {
                "name": name,
                "source": "builtin",
                "match_any": list(payload.get("match_any", ())),
                "commands": [list(command) for command in payload.get("commands", ())],
                "timeout_seconds": float(payload.get("timeout_seconds", 10.0)),
                "description": str(payload.get("description", "")),
            }
            for name, payload in sorted(BUILTIN_PLAYBOOKS.items())
            if not any(item["name"] == name for item in configured)
        ]
        return [*configured, *builtin]

    def run_playbook(
        self, name: str, *, session: str = "", preview: bool = False
    ) -> list[dict[str, object]]:
        if not preview:
            self._enforce_action_allowed("playbook-run")
        playbook = next((item for item in self.config.playbooks if item.name == name), None)
        source = "config"
        if playbook is None:
            builtin = BUILTIN_PLAYBOOKS.get(name)
            if builtin is None:
                raise WsError(f"playbook not found: {name}")
            source = "builtin"
            commands = tuple(
                tuple(str(part) for part in command) for command in builtin["commands"]
            )
            match_any = tuple(str(item) for item in builtin.get("match_any", ()))
            timeout_seconds = float(builtin.get("timeout_seconds", 10.0))
        else:
            commands = playbook.commands
            match_any = playbook.match_any
            timeout_seconds = playbook.timeout_seconds
        context_text = ""
        if session:
            with contextlib.suppress(WsError):
                context_text = self.inspect(session).preview
        if session and match_any and not any(token in context_text for token in match_any):
            raise WsError("playbook conditions not met for current session output")
        results: list[dict[str, object]] = []
        for command in commands:
            if preview:
                results.append(
                    {
                        "command": list(command),
                        "returncode": None,
                        "stdout": "",
                        "stderr": "",
                        "preview": True,
                    }
                )
                continue
            try:
                executed = self.runner(command, capture=True, timeout=timeout_seconds)
                results.append(
                    {
                        "command": list(command),
                        "returncode": executed.returncode,
                        "stdout": executed.stdout[:200],
                        "stderr": executed.stderr[:200],
                    }
                )
            except Exception as error:
                results.append(
                    {
                        "command": list(command),
                        "returncode": -1,
                        "stdout": "",
                        "stderr": str(error),
                    }
                )
        self.append_audit("playbook.preview" if preview else "playbook.run", f"{name} ({source})")
        return results

    def snapshot_diff(self, left: Path, right: Path) -> dict[str, object]:
        def load(path: Path) -> object:
            if not path.exists() or path.is_symlink():
                raise WsError(f"snapshot file not found: {path}")
            text = path.read_text(encoding="utf-8")
            try:
                return json.loads(text)
            except ValueError:
                return {"raw": text.splitlines()}

        left_data = load(left)
        right_data = load(right)
        if left_data == right_data:
            return {"equal": True, "changes": []}
        changes: list[str] = []
        if isinstance(left_data, dict) and isinstance(right_data, dict):
            keys = sorted(set(left_data) | set(right_data))
            for key in keys:
                if left_data.get(key) != right_data.get(key):
                    changes.append(key)
        else:
            changes.append("content")
        return {"equal": False, "changes": changes}

    def chaos_check(self, *, apply: bool = False) -> dict[str, object]:
        """Inject harmless synthetic artifacts to verify diagnostics surfaces."""
        artifacts: list[str] = []
        if apply:
            chaos_dir = self.paths.cache_dir / "chaos"
            chaos_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            marker = (
                chaos_dir / f"chaos-{datetime.now().astimezone().strftime('%Y%m%d-%H%M%S')}.txt"
            )
            marker.write_text("synthetic-chaos-artifact\n", encoding="utf-8")
            artifacts.append(str(marker))
        checks = self.refresh_health_alerts(force=True)
        failures = [
            check.model_dump(mode="json") for check in checks if check.status is HealthStatus.FAIL
        ]
        warnings = [
            check.model_dump(mode="json") for check in checks if check.status is HealthStatus.WARN
        ]
        return {
            "applied": apply,
            "artifacts": artifacts,
            "warnings": warnings,
            "failures": failures,
        }

    def run_remediation_chain(
        self,
        name: str,
        *,
        apply: bool = False,
        alerts: Sequence[HealthCheck] | None = None,
    ) -> dict[str, object]:
        chain = next((item for item in self.config.remediation_chains if item.name == name), None)
        if chain is None:
            raise WsError(f"remediation chain not found: {name}")
        selected_alerts = (
            list(alerts) if alerts is not None else self.refresh_health_alerts(force=True)
        )
        matched = [
            check
            for check in selected_alerts
            if check.name in set(chain.when_checks)
            and (
                check.status is HealthStatus.FAIL
                or (chain.min_status == "warn" and check.status is HealthStatus.WARN)
            )
        ]
        if chain.when_checks and not matched:
            return {
                "name": chain.name,
                "applied": apply,
                "triggered": False,
                "matched_checks": [],
                "steps": [],
                "rollback": None,
            }

        steps: list[dict[str, object]] = []
        rollback: dict[str, object] | None = None
        chain_ok = True
        for raw_step in chain.steps:
            step = str(raw_step).strip()
            row: dict[str, object] = {"step": step, "ok": True}
            try:
                if step.startswith("playbook:"):
                    playbook_name = step.split(":", 1)[1].strip()
                    row["result"] = self.run_playbook(playbook_name, preview=not apply)
                elif step == "archive":
                    row["result"] = self.archive_completed_sessions(dry_run=not apply)
                elif step == "recover-repair":
                    row["result"] = self.repair_integrity() if apply else self.integrity_report()
                else:
                    raise WsError(f"unsupported remediation step: {step}")
            except WsError as error:
                row["ok"] = False
                row["error"] = str(error)
                chain_ok = False
                steps.append(row)
                if apply and chain.rollback_playbook.strip():
                    with contextlib.suppress(WsError):
                        rollback = {
                            "playbook": chain.rollback_playbook,
                            "result": self.run_playbook(chain.rollback_playbook, preview=False),
                        }
                break
            steps.append(row)

        summary = (
            f"{name} apply={apply} triggered={bool(matched) or not chain.when_checks} ok={chain_ok}"
        )
        self.append_audit(
            "remediation-chain.run" if apply else "remediation-chain.preview", summary
        )
        return {
            "name": chain.name,
            "applied": apply,
            "triggered": bool(matched) or not chain.when_checks,
            "matched_checks": [check.name for check in matched],
            "steps": steps,
            "ok": chain_ok,
            "rollback": rollback,
        }

    def self_heal(self, *, apply: bool = False) -> dict[str, object]:
        config = self.config.self_heal
        checks = self.refresh_health_alerts(force=True)
        alerts = [
            check for check in checks if check.status in {HealthStatus.WARN, HealthStatus.FAIL}
        ]
        if not config.enabled:
            return {
                "enabled": False,
                "applied": apply,
                "alerts": [check.model_dump(mode="json") for check in alerts],
                "actions": [],
            }

        actions: list[dict[str, object]] = []
        for rule in config.rules:
            matched = [
                check
                for check in alerts
                if check.name in set(rule.when_checks)
                and (
                    check.status is HealthStatus.FAIL
                    or (rule.min_status == "warn" and check.status is HealthStatus.WARN)
                )
            ]
            if not matched:
                continue

            action_payload: dict[str, object] = {
                "rule": rule.name,
                "action": rule.action,
                "mode": "apply" if apply else "preview",
                "matched_checks": [check.name for check in matched],
                "ok": True,
            }
            if rule.action == "playbook":
                related_sessions: list[str] = []
                if rule.session_scope == "related":
                    for check in matched:
                        for name in self.related_sessions_for_health_check(check):
                            if name not in related_sessions:
                                related_sessions.append(name)
                related_sessions = related_sessions[: rule.max_sessions]
                targets = related_sessions or [""]
                run_rows: list[dict[str, object]] = []
                for name in targets:
                    try:
                        rows = self.run_playbook(rule.playbook, session=name, preview=not apply)
                    except WsError as error:
                        action_payload["ok"] = False
                        run_rows.append(
                            {
                                "session": name,
                                "error": str(error),
                                "rows": [],
                            }
                        )
                        continue
                    run_rows.append(
                        {
                            "session": name,
                            "error": "",
                            "rows": rows,
                        }
                    )
                action_payload["targets"] = related_sessions
                action_payload["results"] = run_rows
            elif rule.action == "archive":
                try:
                    names = self.archive_completed_sessions(dry_run=not apply)
                    action_payload["sessions"] = names
                except WsError as error:
                    action_payload["ok"] = False
                    action_payload["error"] = str(error)
            elif rule.action == "recover-repair":
                try:
                    action_payload["result"] = (
                        self.repair_integrity() if apply else self.integrity_report()
                    )
                except WsError as error:
                    action_payload["ok"] = False
                    action_payload["error"] = str(error)
            elif rule.action == "chain":
                try:
                    chain_result = self.run_remediation_chain(
                        rule.chain,
                        apply=apply,
                        alerts=matched,
                    )
                    action_payload["chain"] = chain_result
                    action_payload["ok"] = bool(chain_result.get("ok", True))
                except WsError as error:
                    action_payload["ok"] = False
                    action_payload["error"] = str(error)
            actions.append(action_payload)

        summary = f"apply={apply} alerts={len(alerts)} actions={len(actions)}"
        self.append_audit("self-heal.run" if apply else "self-heal.preview", summary)
        return {
            "enabled": True,
            "applied": apply,
            "alerts": [check.model_dump(mode="json") for check in alerts],
            "actions": actions,
        }

    def _health_check_specs(self) -> tuple[HealthCheckSpec, ...]:
        health = self.config.health
        disk_root = (
            self.paths.state_dir if self.paths.state_dir.exists() else self.paths.state_dir.parent
        )
        specs: list[HealthCheckSpec] = [
            HealthCheckSpec(
                name="disk-space",
                enabled=health.enabled and health.disk_space_enabled,
                ttl_seconds=health.disk_ttl_seconds,
                run=lambda: disk_space_check(
                    disk_root,
                    warn_percent=health.disk_warn_percent,
                    fail_percent=health.disk_fail_percent,
                ),
            ),
            HealthCheckSpec(
                name="reboot-required",
                enabled=health.enabled and health.reboot_required_enabled,
                ttl_seconds=health.reboot_required_ttl_seconds,
                run=reboot_required_check,
            ),
            HealthCheckSpec(
                name="apt-updates",
                enabled=health.enabled and health.apt_updates_enabled,
                ttl_seconds=health.apt_updates_ttl_seconds,
                run=lambda: apt_updates_check(self.runner, timeout=health.subprocess_timeout),
            ),
            HealthCheckSpec(
                name="docker-containers",
                enabled=health.enabled and health.docker_enabled,
                ttl_seconds=health.docker_ttl_seconds,
                run=lambda: docker_containers_check(self.runner, timeout=health.subprocess_timeout),
            ),
            HealthCheckSpec(
                name="git-dirty",
                enabled=health.enabled and health.git_dirty_enabled,
                ttl_seconds=health.git_dirty_ttl_seconds,
                run=lambda: git_dirty_repos_check(
                    health.project_scan_roots,
                    runner=self.runner,
                    budget=health.git_scan_budget,
                    timeout=health.subprocess_timeout,
                ),
            ),
            HealthCheckSpec(
                name="zombie-sessions",
                enabled=health.enabled and health.zombie_sessions_enabled,
                ttl_seconds=health.zombie_sessions_ttl_seconds,
                run=lambda: zombie_sessions_check(
                    self.store.load_all(),
                    {session.name for session in self.backend.list_sessions()},
                    now=utc_now(),
                    stale_after=timedelta(days=health.zombie_stale_after_days),
                ),
            ),
            HealthCheckSpec(
                name="idle-sessions",
                enabled=health.enabled and health.idle_sessions_enabled,
                ttl_seconds=health.idle_sessions_ttl_seconds,
                run=lambda: idle_live_sessions_check(
                    self.store.load_all(),
                    {session.name for session in self.backend.list_sessions()},
                    now=utc_now(),
                    idle_after=timedelta(days=health.idle_after_days),
                ),
            ),
            HealthCheckSpec(
                name="orphaned-logs",
                enabled=health.enabled and health.orphaned_logs_enabled,
                ttl_seconds=health.orphaned_logs_ttl_seconds,
                run=lambda: orphaned_logs_check(
                    self.paths.logs_dir,
                    {str(record.record_id) for record in self.store.load_all().values()},
                    now=utc_now(),
                    min_age=timedelta(hours=health.orphaned_logs_min_age_hours),
                ),
            ),
            HealthCheckSpec(
                name="missing-cwd",
                enabled=health.enabled and health.missing_cwd_enabled,
                ttl_seconds=health.missing_cwd_ttl_seconds,
                run=lambda: missing_cwd_check(
                    self.store.load_all(),
                    {session.name for session in self.backend.list_sessions()},
                ),
            ),
        ]
        for custom in health.custom_checks:
            specs.append(
                HealthCheckSpec(
                    name=f"custom:{custom.name}",
                    enabled=health.enabled,
                    ttl_seconds=custom.ttl_seconds,
                    run=lambda custom=custom: self._run_custom_health_check(custom),
                )
            )
        if self.config.sla_rules:
            specs.append(
                HealthCheckSpec(
                    name="sla-alerts",
                    enabled=health.enabled,
                    ttl_seconds=300.0,
                    run=self._sla_alerts_check,
                )
            )
        return tuple(specs)

    def _run_custom_health_check(self, custom: object) -> HealthCheck:
        name = getattr(custom, "name", "custom-check")
        command = tuple(getattr(custom, "command", ()))
        status_on_failure = str(getattr(custom, "status_on_failure", "warn"))
        corrective_action = str(getattr(custom, "corrective_action", "") or "")
        if not command:
            return HealthCheck(
                name=f"custom:{name}", status=HealthStatus.INFO, detail="no command configured"
            )
        try:
            result = self.runner(
                command, capture=True, timeout=self.config.health.subprocess_timeout
            )
        except (OSError, subprocess.SubprocessError) as error:
            mapped = (
                HealthStatus.FAIL
                if status_on_failure == "fail"
                else HealthStatus.INFO
                if status_on_failure == "info"
                else HealthStatus.WARN
            )
            return HealthCheck(
                name=f"custom:{name}",
                status=mapped,
                detail=f"custom check failed: {error}",
                corrective_action=corrective_action,
            )
        if result.returncode == 0:
            detail = result.stdout.strip() or "ok"
            return HealthCheck(name=f"custom:{name}", status=HealthStatus.PASS, detail=detail)
        mapped = (
            HealthStatus.FAIL
            if status_on_failure == "fail"
            else HealthStatus.INFO
            if status_on_failure == "info"
            else HealthStatus.WARN
        )
        detail = (result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}")[
            :200
        ]
        return HealthCheck(
            name=f"custom:{name}",
            status=mapped,
            detail=detail,
            corrective_action=corrective_action,
        )

    def _sla_alerts_check(self) -> HealthCheck:
        sessions = self.list_sessions()
        now = utc_now()
        violations: list[str] = []
        highest = HealthStatus.INFO
        for rule in self.config.sla_rules:
            for session in sessions:
                anchor = session.last_active_at or session.created_at
                if anchor is None:
                    continue
                age_hours = max(0.0, (now - anchor).total_seconds() / 3600.0)
                matched = False
                if session.runtime is RuntimeState.DETACHED and age_hours >= rule.detached_hours:
                    matched = True
                if session.task_state is TaskState.BLOCKED and age_hours >= rule.blocked_hours:
                    matched = True
                if (
                    session.input_state is InputState.REQUIRED
                    and age_hours >= rule.input_required_hours
                ):
                    matched = True
                if not matched:
                    continue
                violations.append(f"{session.name} ({rule.name})")
                if rule.severity == "fail":
                    highest = HealthStatus.FAIL
                elif rule.severity == "warn" and highest is not HealthStatus.FAIL:
                    highest = HealthStatus.WARN
        if not violations:
            return HealthCheck(
                name="sla-alerts", status=HealthStatus.PASS, detail="no SLA violations"
            )
        return HealthCheck(
            name="sla-alerts",
            status=highest,
            detail=f"{len(violations)} violation(s): {', '.join(violations[:5])}",
            corrective_action="Review blocked, detached, and input-required sessions.",
        )

    def _health_cache_path(self, name: str) -> Path:
        return self.paths.health_dir / f"{name}.json"

    def _read_health_cache(self, name: str) -> tuple[HealthCheck, datetime] | None:
        path = self._health_cache_path(name)
        try:
            if path.is_symlink():
                return None
            raw = json.loads(path.read_text(encoding="utf-8"))
            checked_at = datetime.fromisoformat(raw["checked_at"])
            check = HealthCheck.model_validate(raw["check"])
        except (OSError, ValueError, KeyError, TypeError):
            return None
        return check, checked_at

    def _write_health_cache(self, name: str, check: HealthCheck) -> None:
        self.paths.health_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.paths.health_dir, 0o700)
        path = self._health_cache_path(name)
        if path.is_symlink():
            return
        payload = json.dumps({"checked_at": utc_now().isoformat(), "check": check.model_dump()})
        temporary_name: str | None = None
        try:
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{path.name}.", dir=self.paths.health_dir
            )
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary_name, 0o600)
            os.replace(temporary_name, path)
        except OSError:
            if temporary_name:
                with contextlib.suppress(OSError):
                    Path(temporary_name).unlink()

    def cached_health_alerts(self) -> list[HealthCheck]:
        """Read-only, subprocess-free: safe to call synchronously on startup."""
        checks: list[HealthCheck] = []
        for spec in self._health_check_specs():
            if not spec.enabled:
                continue
            cached = self._read_health_cache(spec.name)
            if cached is None:
                checks.append(
                    HealthCheck(name=spec.name, status=HealthStatus.INFO, detail="not yet checked")
                )
            else:
                checks.append(cached[0])
        return checks

    def health_stale_names(self, now: datetime) -> frozenset[str]:
        """Cheap staleness check (small file reads, no subprocess) for a UI tick."""
        stale = set()
        for spec in self._health_check_specs():
            if not spec.enabled:
                continue
            cached = self._read_health_cache(spec.name)
            if cached is None or (now - cached[1]).total_seconds() >= spec.ttl_seconds:
                stale.add(spec.name)
        return frozenset(stale)

    def refresh_health_alerts(
        self, *, only: frozenset[str] | None = None, force: bool = False
    ) -> list[HealthCheck]:
        """The only method that shells out for health checks — never call this
        synchronously from a startup path; run it from a background worker."""
        results: list[HealthCheck] = []
        for spec in self._health_check_specs():
            if not spec.enabled:
                continue
            should_run = force or only is None or spec.name in only
            if not should_run:
                cached = self._read_health_cache(spec.name)
                if cached is not None:
                    results.append(cached[0])
                    continue
            try:
                check = spec.run()
            except Exception:
                check = HealthCheck(
                    name=spec.name,
                    status=HealthStatus.INFO,
                    detail="check failed unexpectedly",
                )
            self._write_health_cache(spec.name, check)
            results.append(check)
        return results

    def apply_health_fix(self, name: str) -> HealthCheck:
        specs = {spec.name: spec for spec in self._health_check_specs() if spec.enabled}
        if name not in specs:
            raise WsError(f"health check unknown or disabled: {name}")

        checks = self.refresh_health_alerts(only=frozenset({name}), force=True)
        if not checks:
            raise WsError(f"health check unknown or disabled: {name}")
        check = checks[0]
        if not check.fixable:
            raise WsError(f"health check has no automatic fix: {name}")
        if check.status is HealthStatus.PASS:
            raise WsError(f"health check has no automatic fix: {name}")

        with self._guarded_action("health-fix"):
            if name == "zombie-sessions":
                for session_name in check.affected:
                    record = self.store.load(session_name)
                    if record is None:
                        continue
                    self.store.delete(session_name)
                    with contextlib.suppress(StateError):
                        self._delete_log_path(self._log_path(record))
                self.append_audit("health.fix", f"{name} cleaned {len(check.affected)} session(s)")
            elif name == "orphaned-logs":
                logs_root = self.paths.logs_dir.resolve()
                cleaned = 0
                for raw_path in check.affected:
                    candidate = Path(raw_path).expanduser()
                    resolved = candidate.resolve(strict=False)
                    if resolved.parent != logs_root or resolved.suffix != ".log":
                        continue
                    if not resolved.exists():
                        continue
                    self._delete_log_path(resolved)
                    cleaned += 1
                self.append_audit("health.fix", f"{name} cleaned {cleaned} log file(s)")
            else:
                raise WsError(f"health check has no automatic fix: {name}")

        refreshed = self.refresh_health_alerts(only=frozenset({name}), force=True)
        if not refreshed:
            raise WsError(f"health check unknown or disabled: {name}")
        return refreshed[0]

    def related_sessions_for_health_check(self, check: HealthCheck) -> list[str]:
        sessions = self.list_sessions()
        if check.name == "zombie-sessions":
            return [session.name for session in sessions if session.runtime is RuntimeState.STOPPED]
        if check.name == "idle-sessions":
            threshold = timedelta(days=max(1, self.config.health.idle_after_days))
            now = utc_now()
            related: list[str] = []
            for session in sessions:
                if session.last_active_at is None:
                    continue
                activity = (
                    session.last_active_at
                    if session.last_active_at.tzinfo is not None
                    else session.last_active_at.replace(tzinfo=UTC)
                )
                if (now - activity) > threshold:
                    related.append(session.name)
            return related
        if check.name in {"git-dirty", "missing-cwd"}:
            related: list[str] = []
            detail = check.detail.casefold()
            for session in sessions:
                project = session.project.casefold()
                if project and project in detail:
                    related.append(session.name)
            return related
        return []

    def onboarding_seen(self) -> bool:
        return self.paths.onboarding_file.is_file() and not self.paths.onboarding_file.is_symlink()

    def mark_onboarding_seen(self) -> None:
        self.paths.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.paths.state_dir, 0o700)
        flags = os.O_WRONLY | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(self.paths.onboarding_file, flags, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as marker:
            marker.write("seen\n")
        os.chmod(self.paths.onboarding_file, 0o600)
