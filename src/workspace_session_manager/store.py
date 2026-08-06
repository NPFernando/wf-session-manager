"""Atomic, permission-restricted persistence for ws-owned session metadata."""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from pydantic import ValidationError

from workspace_session_manager.errors import StateError
from workspace_session_manager.models import (
    FilterPreset,
    SESSION_NAME_PATTERN,
    InterfacePreferences,
    Preset,
    SessionTimelineEvent,
    SessionTemplate,
    SessionMetadata,
    UndoEntry,
)
from workspace_session_manager.paths import AppPaths


class MetadataStore:
    def __init__(self, paths: AppPaths) -> None:
        self.paths = paths

    def _path(self, name: str) -> Path:
        if not SESSION_NAME_PATTERN.fullmatch(name):
            raise StateError(f"unsafe metadata name: {name!r}")
        return self.paths.sessions_dir / f"{name}.json"

    @contextmanager
    def _locked(self) -> Iterator[None]:
        self.paths.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.paths.state_dir, 0o700)
        descriptor = os.open(self.paths.lock_file, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            with os.fdopen(descriptor, "r+") as lock:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                yield
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        except OSError as error:
            raise StateError(f"unable to lock state: {error}") from error

    def _read(self, path: Path) -> SessionMetadata:
        try:
            if path.is_symlink():
                raise StateError(f"refusing symlinked metadata: {path.name}")
            return SessionMetadata.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValidationError) as error:
            raise StateError(f"invalid metadata file {path.name}: {error}") from error

    def load(self, name: str) -> SessionMetadata | None:
        path = self._path(name)
        if not path.exists():
            return None
        return self._read(path)

    def load_all(self) -> dict[str, SessionMetadata]:
        if not self.paths.sessions_dir.exists():
            return {}
        records: dict[str, SessionMetadata] = {}
        for path in sorted(self.paths.sessions_dir.glob("*.json")):
            try:
                record = self._read(path)
            except StateError:
                continue
            if path.stem == record.name:
                records[record.name] = record
        return records

    def validation_errors(self) -> list[str]:
        if not self.paths.sessions_dir.exists():
            return []
        errors: list[str] = []
        for path in sorted(self.paths.sessions_dir.glob("*.json")):
            try:
                record = self._read(path)
                if path.stem != record.name:
                    errors.append(f"{path.name}: filename does not match record name")
            except StateError as error:
                errors.append(str(error))
        return errors

    def save(self, record: SessionMetadata) -> None:
        with self._locked():
            self._write_unlocked(self._path(record.name), record)

    def save_new(self, record: SessionMetadata) -> None:
        path = self._path(record.name)
        with self._locked():
            if path.exists():
                raise StateError(f"metadata already exists: {record.name}")
            self._write_unlocked(path, record)

    def replace(self, old_name: str, record: SessionMetadata) -> None:
        old_path = self._path(old_name)
        new_path = self._path(record.name)
        with self._locked():
            if new_path.exists() and new_path != old_path:
                raise StateError(f"metadata already exists: {record.name}")
            self._write_unlocked(new_path, record)
            if old_path != new_path:
                try:
                    old_path.unlink(missing_ok=True)
                except OSError as error:
                    raise StateError(f"unable to remove old metadata: {error}") from error

    def delete(self, name: str) -> None:
        with self._locked():
            try:
                self._path(name).unlink(missing_ok=True)
            except OSError as error:
                raise StateError(f"unable to delete metadata for {name}: {error}") from error

    def _write_unlocked(self, path: Path, record: SessionMetadata) -> None:
        self.paths.sessions_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.paths.sessions_dir, 0o700)
        temporary_name: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.paths.sessions_dir,
                prefix=f".{path.name}.",
                delete=False,
            ) as temporary:
                temporary_name = temporary.name
                temporary.write(record.model_dump_json(indent=2))
                temporary.write("\n")
                temporary.flush()
                os.fsync(temporary.fileno())
            os.chmod(temporary_name, 0o600)
            os.replace(temporary_name, path)
        except OSError as error:
            if temporary_name:
                Path(temporary_name).unlink(missing_ok=True)
            raise StateError(f"unable to write metadata for {record.name}: {error}") from error


class PresetStore:
    """Single-file store for named create-session presets (small in number,
    unlike sessions, so all presets live in one JSON object rather than
    one file per record)."""

    def __init__(self, paths: AppPaths) -> None:
        self.paths = paths

    @contextmanager
    def _locked(self) -> Iterator[None]:
        self.paths.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.paths.state_dir, 0o700)
        descriptor = os.open(self.paths.lock_file, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            with os.fdopen(descriptor, "r+") as lock:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                yield
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        except OSError as error:
            raise StateError(f"unable to lock state: {error}") from error

    def _read_all_unlocked(self) -> dict[str, Preset]:
        path = self.paths.presets_file
        if not path.exists():
            return {}
        try:
            if path.is_symlink():
                raise StateError(f"refusing symlinked presets file: {path.name}")
            raw = json.loads(path.read_text(encoding="utf-8"))
            return {name: Preset.model_validate(value) for name, value in raw.items()}
        except (OSError, ValidationError, ValueError) as error:
            raise StateError(f"invalid presets file: {error}") from error

    def load_all(self) -> dict[str, Preset]:
        with self._locked():
            return self._read_all_unlocked()

    def load(self, name: str) -> Preset | None:
        return self.load_all().get(name)

    def save(self, preset: Preset) -> None:
        with self._locked():
            presets = self._read_all_unlocked()
            presets[preset.name] = preset
            self._write_unlocked(presets)

    def delete(self, name: str) -> None:
        with self._locked():
            presets = self._read_all_unlocked()
            if name not in presets:
                return
            del presets[name]
            self._write_unlocked(presets)

    def _write_unlocked(self, presets: dict[str, Preset]) -> None:
        self.paths.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.paths.state_dir, 0o700)
        path = self.paths.presets_file
        if path.is_symlink():
            raise StateError(f"refusing symlinked presets file: {path.name}")
        payload = json.dumps(
            {name: json.loads(preset.model_dump_json()) for name, preset in presets.items()},
            indent=2,
        )
        temporary_name: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.paths.state_dir,
                prefix=f".{path.name}.",
                delete=False,
            ) as temporary:
                temporary_name = temporary.name
                temporary.write(payload)
                temporary.write("\n")
                temporary.flush()
                os.fsync(temporary.fileno())
            os.chmod(temporary_name, 0o600)
            os.replace(temporary_name, path)
        except OSError as error:
            if temporary_name:
                Path(temporary_name).unlink(missing_ok=True)
            raise StateError(f"unable to write presets: {error}") from error


class FilterPresetStore:
    """Owner-only single-file store for dashboard filter presets."""

    def __init__(self, paths: AppPaths) -> None:
        self.paths = paths

    @contextmanager
    def _locked(self) -> Iterator[None]:
        self.paths.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.paths.state_dir, 0o700)
        descriptor = os.open(self.paths.lock_file, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            with os.fdopen(descriptor, "r+") as lock:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                yield
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        except OSError as error:
            raise StateError(f"unable to lock state: {error}") from error

    def _read_all_unlocked(self) -> dict[str, FilterPreset]:
        path = self.paths.filter_presets_file
        if not path.exists():
            return {}
        try:
            if path.is_symlink():
                raise StateError(f"refusing symlinked filter presets file: {path.name}")
            raw = json.loads(path.read_text(encoding="utf-8"))
            return {name: FilterPreset.model_validate(value) for name, value in raw.items()}
        except (OSError, ValidationError, ValueError) as error:
            raise StateError(f"invalid filter presets file: {error}") from error

    def load_all(self) -> dict[str, FilterPreset]:
        with self._locked():
            return self._read_all_unlocked()

    def load(self, name: str) -> FilterPreset | None:
        return self.load_all().get(name)

    def save(self, preset: FilterPreset) -> None:
        with self._locked():
            presets = self._read_all_unlocked()
            presets[preset.name] = preset
            self._write_unlocked(presets)

    def delete(self, name: str) -> None:
        with self._locked():
            presets = self._read_all_unlocked()
            if name not in presets:
                return
            del presets[name]
            self._write_unlocked(presets)

    def _write_unlocked(self, presets: dict[str, FilterPreset]) -> None:
        self.paths.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.paths.state_dir, 0o700)
        path = self.paths.filter_presets_file
        if path.is_symlink():
            raise StateError(f"refusing symlinked filter presets file: {path.name}")
        payload = json.dumps(
            {name: json.loads(preset.model_dump_json()) for name, preset in presets.items()},
            indent=2,
        )
        temporary_name: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.paths.state_dir,
                prefix=f".{path.name}.",
                delete=False,
            ) as temporary:
                temporary_name = temporary.name
                temporary.write(payload)
                temporary.write("\n")
                temporary.flush()
                os.fsync(temporary.fileno())
            os.chmod(temporary_name, 0o600)
            os.replace(temporary_name, path)
        except OSError as error:
            if temporary_name:
                Path(temporary_name).unlink(missing_ok=True)
            raise StateError(f"unable to write filter presets: {error}") from error


class TemplateStore:
    """Owner-only store for parameterized session templates."""

    def __init__(self, paths: AppPaths) -> None:
        self.paths = paths

    @contextmanager
    def _locked(self) -> Iterator[None]:
        self.paths.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.paths.state_dir, 0o700)
        descriptor = os.open(self.paths.lock_file, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            with os.fdopen(descriptor, "r+") as lock:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                yield
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        except OSError as error:
            raise StateError(f"unable to lock state: {error}") from error

    def _read_all_unlocked(self) -> dict[str, SessionTemplate]:
        path = self.paths.templates_file
        if not path.exists():
            return {}
        try:
            if path.is_symlink():
                raise StateError(f"refusing symlinked templates file: {path.name}")
            raw = json.loads(path.read_text(encoding="utf-8"))
            return {name: SessionTemplate.model_validate(value) for name, value in raw.items()}
        except (OSError, ValidationError, ValueError) as error:
            raise StateError(f"invalid templates file: {error}") from error

    def load_all(self) -> dict[str, SessionTemplate]:
        with self._locked():
            return self._read_all_unlocked()

    def load(self, name: str) -> SessionTemplate | None:
        return self.load_all().get(name)

    def save(self, template: SessionTemplate) -> None:
        with self._locked():
            templates = self._read_all_unlocked()
            templates[template.name] = template
            self._write_unlocked(templates)

    def delete(self, name: str) -> None:
        with self._locked():
            templates = self._read_all_unlocked()
            if name not in templates:
                return
            del templates[name]
            self._write_unlocked(templates)

    def _write_unlocked(self, templates: dict[str, SessionTemplate]) -> None:
        self.paths.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.paths.state_dir, 0o700)
        path = self.paths.templates_file
        if path.is_symlink():
            raise StateError(f"refusing symlinked templates file: {path.name}")
        payload = json.dumps(
            {name: json.loads(template.model_dump_json()) for name, template in templates.items()},
            indent=2,
        )
        temporary_name: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.paths.state_dir,
                prefix=f".{path.name}.",
                delete=False,
            ) as temporary:
                temporary_name = temporary.name
                temporary.write(payload)
                temporary.write("\n")
                temporary.flush()
                os.fsync(temporary.fileno())
            os.chmod(temporary_name, 0o600)
            os.replace(temporary_name, path)
        except OSError as error:
            if temporary_name:
                Path(temporary_name).unlink(missing_ok=True)
            raise StateError(f"unable to write templates: {error}") from error


class UndoStore:
    """Short-lived rollback stack persisted across one ws invocation."""

    def __init__(self, paths: AppPaths) -> None:
        self.paths = paths

    @contextmanager
    def _locked(self) -> Iterator[None]:
        self.paths.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.paths.state_dir, 0o700)
        descriptor = os.open(self.paths.lock_file, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            with os.fdopen(descriptor, "r+") as lock:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                yield
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        except OSError as error:
            raise StateError(f"unable to lock state: {error}") from error

    def _read_all_unlocked(self) -> list[UndoEntry]:
        path = self.paths.undo_file
        if not path.exists():
            return []
        try:
            if path.is_symlink():
                raise StateError(f"refusing symlinked undo file: {path.name}")
            raw = json.loads(path.read_text(encoding="utf-8"))
            return [UndoEntry.model_validate(item) for item in raw]
        except (OSError, ValidationError, ValueError) as error:
            raise StateError(f"invalid undo file: {error}") from error

    def push(self, entry: UndoEntry, *, max_items: int = 40) -> None:
        with self._locked():
            entries = self._read_all_unlocked()
            entries.append(entry)
            self._write_unlocked(entries[-max_items:])

    def pop(self, *, now: datetime | None = None) -> UndoEntry | None:
        now_value = now or datetime.now().astimezone()
        with self._locked():
            entries = [
                item
                for item in self._read_all_unlocked()
                if item.expires_at.astimezone() >= now_value
            ]
            if not entries:
                self._write_unlocked([])
                return None
            entry = entries.pop()
            self._write_unlocked(entries)
            return entry

    def _write_unlocked(self, entries: list[UndoEntry]) -> None:
        self.paths.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.paths.state_dir, 0o700)
        path = self.paths.undo_file
        if path.is_symlink():
            raise StateError(f"refusing symlinked undo file: {path.name}")
        payload = json.dumps([item.model_dump(mode="json") for item in entries], indent=2)
        temporary_name: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.paths.state_dir,
                prefix=f".{path.name}.",
                delete=False,
            ) as temporary:
                temporary_name = temporary.name
                temporary.write(payload)
                temporary.write("\n")
                temporary.flush()
                os.fsync(temporary.fileno())
            os.chmod(temporary_name, 0o600)
            os.replace(temporary_name, path)
        except OSError as error:
            if temporary_name:
                Path(temporary_name).unlink(missing_ok=True)
            raise StateError(f"unable to write undo stack: {error}") from error


class TimelineStore:
    """Per-session append-only timeline for user-visible operational history."""

    def __init__(self, paths: AppPaths) -> None:
        self.paths = paths

    def _path(self, session_name: str) -> Path:
        if not SESSION_NAME_PATTERN.fullmatch(session_name):
            raise StateError(f"unsafe timeline session name: {session_name!r}")
        return self.paths.timeline_dir / f"{session_name}.json"

    def list(self, session_name: str, *, limit: int = 50) -> list[SessionTimelineEvent]:
        path = self._path(session_name)
        if not path.exists():
            return []
        try:
            if path.is_symlink():
                raise StateError(f"refusing symlinked timeline file: {path.name}")
            raw = json.loads(path.read_text(encoding="utf-8"))
            events = [SessionTimelineEvent.model_validate(item) for item in raw]
            return events[-max(1, limit) :]
        except (OSError, ValidationError, ValueError) as error:
            raise StateError(f"invalid timeline file: {error}") from error

    def append(self, session_name: str, event: SessionTimelineEvent, *, max_events: int = 200) -> None:
        self.paths.timeline_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.paths.timeline_dir, 0o700)
        path = self._path(session_name)
        events = self.list(session_name, limit=max_events) if path.exists() else []
        events.append(event)
        events = events[-max_events:]
        temporary_name: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.paths.timeline_dir,
                prefix=f".{path.name}.",
                delete=False,
            ) as temporary:
                temporary_name = temporary.name
                temporary.write(json.dumps([item.model_dump(mode="json") for item in events], indent=2))
                temporary.write("\n")
                temporary.flush()
                os.fsync(temporary.fileno())
            os.chmod(temporary_name, 0o600)
            os.replace(temporary_name, path)
        except OSError as error:
            if temporary_name:
                Path(temporary_name).unlink(missing_ok=True)
            raise StateError(f"unable to write timeline: {error}") from error


class InterfacePreferencesStore:
    """Owner-only atomic store for interface choices, isolated from session state."""

    def __init__(self, paths: AppPaths) -> None:
        self.paths = paths

    @contextmanager
    def _locked(self) -> Iterator[None]:
        self.paths.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.paths.state_dir, 0o700)
        descriptor = os.open(self.paths.lock_file, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            with os.fdopen(descriptor, "r+") as lock:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                yield
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        except OSError as error:
            raise StateError(f"unable to lock state: {error}") from error

    def _read_unlocked(self) -> InterfacePreferences:
        path = self.paths.interface_preferences_file
        if path.is_symlink():
            raise StateError(f"refusing symlinked interface preferences file: {path.name}")
        if not path.exists():
            return InterfacePreferences()
        try:
            return InterfacePreferences.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValidationError) as error:
            raise StateError(f"invalid interface preferences file: {error}") from error

    def load(self) -> InterfacePreferences:
        """Return persisted preferences, or in-memory defaults when absent."""
        with self._locked():
            return self._read_unlocked()

    def save(self, preferences: InterfacePreferences) -> None:
        with self._locked():
            self._write_unlocked(preferences)

    def _write_unlocked(self, preferences: InterfacePreferences) -> None:
        self.paths.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.paths.state_dir, 0o700)
        path = self.paths.interface_preferences_file
        if path.is_symlink():
            raise StateError(f"refusing symlinked interface preferences file: {path.name}")
        temporary_name: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.paths.state_dir,
                prefix=f".{path.name}.",
                delete=False,
            ) as temporary:
                temporary_name = temporary.name
                temporary.write(preferences.model_dump_json(indent=2))
                temporary.write("\n")
                temporary.flush()
                os.fsync(temporary.fileno())
            os.chmod(temporary_name, 0o600)
            os.replace(temporary_name, path)
        except OSError as error:
            if temporary_name:
                Path(temporary_name).unlink(missing_ok=True)
            raise StateError(f"unable to write interface preferences: {error}") from error
