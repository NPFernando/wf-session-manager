"""Session lifecycle policy, ownership enforcement, and merged discovery."""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import stat
import tomllib
import unicodedata
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

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
    HealthCheck,
    HealthStatus,
    InputState,
    OutputSource,
    Preset,
    RuntimeState,
    SessionDetails,
    SessionMetadata,
    SessionView,
    TaskState,
    TmuxSession,
    Tool,
    normalize_task_state,
    utc_now,
)
from workspace_session_manager.paths import AppPaths
from workspace_session_manager.security import BoundedOutput, bounded_output, redact_text
from workspace_session_manager.store import MetadataStore, PresetStore
from workspace_session_manager.tmux import Runner, subprocess_runner

SEARCH_CONTEXT_LINES = 2
SEARCH_MAX_MATCHES_PER_SESSION = 20
SEARCH_READ_CAP_BYTES = 2_097_152


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
    for tool in (Tool.CLAUDE, Tool.CODEX, Tool.HERMES):
        if name == tool.value or name.startswith(f"{tool.value}-"):
            return tool
    command = Path(current_command).name.lower()
    if command in {tool.value for tool in Tool if tool is not Tool.SHELL}:
        return Tool(command)
    return Tool.SHELL


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
    ) -> None:
        self.backend = backend
        self.store = store
        self.config = config
        self.paths = paths
        self.legacy = legacy or LegacyMetadataReader(config.legacy_state_dirs)
        self.runner = runner
        self.preset_store = preset_store or PresetStore(paths)

    def list_sessions(self, *, include_unmanaged: bool = False) -> list[SessionView]:
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

    def create(self, request: CreateRequest, *, dry_run: bool = False) -> SessionView:
        validation = self.validate_create(
            request.tool,
            request.name,
            request.cwd,
            automatic_prefix=request.automatic_prefix,
        )
        if not validation.valid or validation.cwd is None:
            message = validation.errors[0] if validation.errors else "invalid session request"
            if message.startswith("session already exists"):
                raise SessionExistsError(message)
            if message.startswith("command not found") or "disabled in configuration" in message:
                raise ToolUnavailableError(message)
            raise WsError(message)
        name = validation.normalized_name
        cwd = validation.cwd
        profile = self.config.tools[request.tool]
        shell_profile = self.config.tools[Tool.SHELL]
        if not profile.enabled:
            raise ToolUnavailableError(f"{request.tool.value} is disabled in configuration")
        if not command_available(profile.command):
            raise ToolUnavailableError(f"command not found: {profile.command[0]}")
        if not command_available(shell_profile.command):
            raise ToolUnavailableError(f"shell command not found: {shell_profile.command[0]}")

        if dry_run:
            now = utc_now()
            return SessionView(
                name=name,
                display_name=request.display_name or request.name.strip(),
                session_id="dry-run",
                tool=request.tool,
                cwd=cwd,
                current_command=Path(profile.command[0]).name,
                runtime=RuntimeState.DETACHED,
                attached=False,
                attached_clients=0,
                windows=1,
                created_at=now,
                project=request.project,
                note=request.note,
                tags=request.tags,
                task_state=request.task_state,
                input_state=request.input_state,
                owned=True,
                logging_enabled=request.logging_enabled,
                last_active_at=now,
            )

        agent_command = None if request.tool is Tool.SHELL else profile.command
        session = self.backend.create_session(
            name=name,
            cwd=cwd,
            shell_command=shell_profile.command,
            agent_command=agent_command,
        )
        record = SessionMetadata(
            tmux_session_id=session.session_id,
            name=name,
            display_name=request.display_name or request.name.strip(),
            tool=request.tool,
            cwd=cwd,
            project=request.project,
            note=request.note,
            tags=request.tags,
            task_state=request.task_state,
            input_state=request.input_state,
        )
        log_path = self._log_path(record)
        try:
            if request.logging_enabled:
                self._prepare_log(log_path)
                self.backend.set_logging(name, log_path, expected_id=session.session_id)
            self.store.save_new(record)
        except Exception:
            self.backend.kill_session(name, expected_id=session.session_id)
            self._delete_log_path(log_path)
            raise
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
        self.get(name)
        record = self._owned_record(name)
        self.store.save(
            record.model_copy(update={"last_attached_at": utc_now(), "updated_at": utc_now()})
        )
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
        if len(note) > 2000:
            raise WsError("note cannot exceed 2000 characters")
        record = self._managed_record(name)
        self.store.save(record.model_copy(update={"note": note, "updated_at": utc_now()}))
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
    ) -> SessionView:
        record = self._managed_record(name)
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
        return self.get(name)

    def rename(self, old_name: str, requested_name: str) -> SessionView:
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
                self.backend.rename_session(new_name, old_name, expected_id=record.tmux_session_id)
            raise
        return self.get(new_name)

    def delete(self, name: str) -> None:
        record = self._managed_record(name)
        if self.backend.session_exists(name):
            if self.get(name).logging_enabled:
                self.backend.set_logging(name, None, expected_id=record.tmux_session_id)
            self.backend.kill_session(name, expected_id=record.tmux_session_id)
        self.store.delete(name)
        self._delete_log_path(self._log_path(record))

    def stop_command(self, name: str) -> None:
        record = self._owned_record(name)
        self.backend.send_interrupt(name, expected_id=record.tmux_session_id)

    def stop_session(self, name: str) -> SessionView:
        record = self._owned_record(name)
        session = self.get(name)
        if session.logging_enabled:
            self.backend.set_logging(name, None, expected_id=record.tmux_session_id)
        self.backend.kill_session(name, expected_id=record.tmux_session_id)
        self.store.save(record.model_copy(update={"updated_at": utc_now()}))
        return self.get(name)

    def restart(self, name: str) -> SessionView:
        record = self._managed_record(name)
        profile = self.config.tools[record.tool]
        shell_profile = self.config.tools[Tool.SHELL]
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
        return self.get(name)

    def set_logging(self, name: str, enabled: bool) -> SessionView:
        record = self._owned_record(name)
        path = self._log_path(record)
        if enabled:
            self._prepare_log(path)
            self.backend.set_logging(name, path, expected_id=record.tmux_session_id)
        else:
            self.backend.set_logging(name, None, expected_id=record.tmux_session_id)
        return self.get(name)

    def delete_logs(self, name: str) -> SessionView:
        record = self._managed_record(name)
        live = self.backend.session_exists(name)
        was_logging = live and self.get(name).logging_enabled
        if was_logging:
            self.backend.set_logging(name, None, expected_id=record.tmux_session_id)
        self._delete_log_path(self._log_path(record))
        if was_logging:
            path = self._log_path(record)
            self._prepare_log(path)
            self.backend.set_logging(name, path, expected_id=record.tmux_session_id)
        return self.get(name)

    def remove_metadata(self, name: str) -> None:
        record = self._managed_record(name)
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

    def _health_check_specs(self) -> tuple[HealthCheckSpec, ...]:
        health = self.config.health
        disk_root = (
            self.paths.state_dir if self.paths.state_dir.exists() else self.paths.state_dir.parent
        )
        return (
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
        )

    def _health_cache_path(self, name: str) -> Path:
        return self.paths.health_dir / f"{name}.json"

    def _read_health_cache(self, name: str) -> tuple[HealthCheck, datetime] | None:
        try:
            raw = json.loads(self._health_cache_path(name).read_text(encoding="utf-8"))
            checked_at = datetime.fromisoformat(raw["checked_at"])
            check = HealthCheck.model_validate(raw["check"])
        except (OSError, ValueError, KeyError, TypeError):
            return None
        return check, checked_at

    def _write_health_cache(self, name: str, check: HealthCheck) -> None:
        self.paths.health_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = self._health_cache_path(name)
        payload = json.dumps({"checked_at": utc_now().isoformat(), "check": check.model_dump()})
        temporary = path.with_suffix(".json.tmp")
        try:
            temporary.write_text(payload, encoding="utf-8")
            os.replace(temporary, path)
        except OSError:
            with contextlib.suppress(OSError):
                temporary.unlink()

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
        """Apply the corrective fix for one fixable health check, then re-run it.

        Only ws-owned resources are touched: zombie-sessions deletes stale
        metadata for tmux sessions long gone, orphaned-logs deletes log
        files with no matching session record. Other checks (including
        idle-sessions, which are still live/attached) have no automatic fix.
        """
        spec = next((s for s in self._health_check_specs() if s.name == name), None)
        if spec is None or not spec.enabled:
            raise WsError(f"unknown or disabled health check: {name}")
        check = spec.run()
        if not check.fixable:
            raise WsError(f"{name} has no automatic fix")
        if name == "zombie-sessions":
            for session_name in check.affected:
                with contextlib.suppress(WsError):
                    self.delete(session_name)
        else:
            for raw_path in check.affected:
                with contextlib.suppress(OSError, StateError):
                    self._delete_log_path(Path(raw_path))
        refreshed = spec.run()
        self._write_health_cache(name, refreshed)
        return refreshed

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
