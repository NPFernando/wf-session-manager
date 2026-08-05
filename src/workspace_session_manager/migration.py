"""Previewable, exact-ID adoption of existing tmux sessions."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from workspace_session_manager.errors import MigrationError, WsError
from workspace_session_manager.legacy import MAX_SIDECAR_BYTES, LegacyMetadataReader
from workspace_session_manager.models import (
    SESSION_NAME_PATTERN,
    LegacyMetadata,
    SessionMetadata,
    SessionState,
    TmuxSession,
    Tool,
    normalize_task_state,
    utc_now,
)
from workspace_session_manager.paths import AppPaths
from workspace_session_manager.service import SessionBackend, infer_tool
from workspace_session_manager.store import MetadataStore

OWNER_OPTION = "@wf_owner"
OWNER_VALUE = "workspace-session-manager"
MAX_PLAN_BYTES = 1024 * 1024
SIDECAR_SUFFIXES = ("tool", "cwd", "project", "note", "tags", "state", "last", "pinned")


def _canonical_digest(value: object) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _record_digest(record: SessionMetadata) -> str:
    return _canonical_digest(record.model_dump(mode="json"))


def _reject_symlink_components(path: Path) -> None:
    absolute = path.absolute()
    for component in (*reversed(absolute.parents), absolute):
        if component.is_symlink():
            raise MigrationError(f"refusing path with symlink component: {component}")


def _write_private_json(path: Path, payload: str) -> None:
    _reject_symlink_components(path)
    parent = path.parent
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(parent, 0o700)
    if path.is_symlink():
        raise MigrationError(f"refusing symlinked migration file: {path}")
    temporary_name: str | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=parent)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(payload)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary_name, 0o600)
        os.replace(temporary_name, path)
    except OSError as error:
        if temporary_name:
            Path(temporary_name).unlink(missing_ok=True)
        raise MigrationError(f"unable to write migration file {path}: {error}") from error


def _read_private_json(path: Path) -> str:
    try:
        _reject_symlink_components(path)
        if path.is_symlink() or not path.is_file():
            raise MigrationError(f"refusing unsafe migration file: {path}")
        details = path.stat()
        if details.st_mode & 0o077:
            raise MigrationError(f"migration file must have owner-only permissions: {path}")
        if details.st_size > MAX_PLAN_BYTES:
            raise MigrationError(f"migration file is too large: {path}")
        return path.read_text(encoding="utf-8")
    except OSError as error:
        raise MigrationError(f"unable to read migration file {path}: {error}") from error


class MigrationSourceSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    root: Path
    hashes: dict[str, str]


class MigrationItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str
    tmux_session_id: str
    tool: Tool
    cwd: Path
    project: str = ""
    note: Annotated[str, Field(max_length=2000)] = ""
    tags: Annotated[list[str], Field(max_length=12)] = Field(default_factory=list)
    state: SessionState = SessionState.ACTIVE
    pinned: bool = False
    created_at: datetime
    legacy_last_used: datetime | None = None
    previous_owner: str | None = None
    sources: list[MigrationSourceSnapshot]
    warnings: list[str] = Field(default_factory=list)


def _snapshot_digest(items: list[MigrationItem]) -> str:
    return _canonical_digest([item.model_dump(mode="json") for item in items])


class MigrationPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1] = 1
    plan_id: UUID = Field(default_factory=uuid4)
    created_at: datetime = Field(default_factory=utc_now)
    items: list[MigrationItem]
    snapshot_digest: str

    @model_validator(mode="after")
    def valid_snapshot(self) -> MigrationPlan:
        if not self.items:
            raise ValueError("migration plan has no sessions")
        names = [item.name for item in self.items]
        if len(names) != len(set(names)):
            raise ValueError("migration plan contains duplicate session names")
        if self.snapshot_digest != _snapshot_digest(self.items):
            raise ValueError("migration plan snapshot digest is invalid")
        return self

    @classmethod
    def create(cls, items: list[MigrationItem]) -> MigrationPlan:
        ordered = sorted(items, key=lambda item: item.name)
        return cls(items=ordered, snapshot_digest=_snapshot_digest(ordered))


class MigrationJournalItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    record: SessionMetadata
    record_digest: str
    previous_owner: str | None = None


class MigrationJournal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1] = 1
    migration_id: UUID
    snapshot_digest: str
    status: Literal["applying", "applied", "rolled_back", "failed"]
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    items: list[MigrationJournalItem]
    error: str = ""

    @model_validator(mode="after")
    def valid_items(self) -> MigrationJournal:
        if not self.items:
            raise ValueError("migration journal has no sessions")
        names = [item.record.name for item in self.items]
        if len(names) != len(set(names)):
            raise ValueError("migration journal contains duplicate session names")
        if any(item.record_digest != _record_digest(item.record) for item in self.items):
            raise ValueError("migration journal record digest is invalid")
        return self


class MigrationManager:
    def __init__(
        self,
        *,
        backend: SessionBackend,
        store: MetadataStore,
        legacy: LegacyMetadataReader,
        paths: AppPaths,
    ) -> None:
        self.backend = backend
        self.store = store
        self.legacy = legacy
        self.paths = paths

    @contextmanager
    def _locked(self) -> Iterator[None]:
        state_dir = self.paths.state_dir
        if state_dir.is_symlink():
            raise MigrationError(f"refusing symlinked state directory: {state_dir}")
        descriptor: int | None = None
        try:
            state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(state_dir, 0o700)
            flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(self.paths.migration_lock_file, flags, 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX)
        except OSError as error:
            if descriptor is not None:
                os.close(descriptor)
            raise MigrationError(f"unable to lock migration state: {error}") from error
        if descriptor is None:  # pragma: no cover - os.open either returns or raises
            raise MigrationError("migration lock descriptor was not created")
        try:
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def _source_snapshot(self, root: Path, name: str) -> MigrationSourceSnapshot:
        hashes: dict[str, str] = {}
        for suffix in SIDECAR_SUFFIXES:
            path = root / f"{name}.{suffix}"
            if not path.exists() and not path.is_symlink():
                continue
            try:
                if path.is_symlink() or not path.is_file():
                    raise MigrationError(f"refusing unsafe sidecar: {path}")
                if path.stat().st_size > MAX_SIDECAR_BYTES:
                    raise MigrationError(f"sidecar exceeds size limit: {path}")
                hashes[suffix] = hashlib.sha256(path.read_bytes()).hexdigest()
            except OSError as error:
                raise MigrationError(f"unable to hash sidecar {path}: {error}") from error
        return MigrationSourceSnapshot(root=root, hashes=hashes)

    @staticmethod
    def _legacy_payload(metadata: LegacyMetadata) -> dict[str, object]:
        return metadata.model_dump(mode="json", exclude={"source"})

    def _legacy_for(self, name: str) -> tuple[LegacyMetadata, list[MigrationSourceSnapshot]]:
        metadata = self.legacy.read_all(name)
        if not metadata:
            raise MigrationError(f"no legacy metadata found for {name}")
        expected = self._legacy_payload(metadata[0])
        if any(self._legacy_payload(item) != expected for item in metadata[1:]):
            roots = ", ".join(str(item.source) for item in metadata)
            raise MigrationError(f"conflicting legacy metadata for {name}: {roots}")
        sources = [
            self._source_snapshot(item.source, name) for item in metadata if item.source is not None
        ]
        return metadata[0], sources

    def _build_item(self, session: TmuxSession) -> MigrationItem:
        if not SESSION_NAME_PATTERN.fullmatch(session.name):
            raise MigrationError(f"unsafe tmux session name: {session.name!r}")
        if self.store.load(session.name) is not None:
            raise MigrationError(f"metadata already exists for {session.name}")
        previous_owner = self.backend.get_option(
            session.name, OWNER_OPTION, expected_id=session.session_id
        )
        if previous_owner is not None:
            raise MigrationError(
                f"tmux owner option already exists for {session.name}: {previous_owner}"
            )

        legacy, sources = self._legacy_for(session.name)
        warnings: list[str] = []
        cwd = legacy.cwd
        if cwd is None or not cwd.is_absolute():
            cwd = session.cwd
            warnings.append("legacy cwd missing or invalid; using live tmux cwd")
        if not cwd.is_absolute():
            raise MigrationError(f"no absolute working directory available for {session.name}")
        if len(legacy.note) > 2000:
            raise MigrationError(f"legacy note exceeds 2000 characters for {session.name}")
        if legacy.project and len(legacy.project.name) > 200:
            raise MigrationError(f"legacy project name exceeds 200 characters for {session.name}")
        if len(legacy.tags) > 12 or any(
            not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,31}", tag) for tag in legacy.tags
        ):
            raise MigrationError(f"unsupported legacy tags for {session.name}")
        if legacy.state:
            try:
                state = SessionState(legacy.state)
            except ValueError as error:
                raise MigrationError(
                    f"unsupported legacy state for {session.name}: {legacy.state}"
                ) from error
        else:
            state = SessionState.ACTIVE
            warnings.append("legacy state missing; using active")

        return MigrationItem(
            name=session.name,
            tmux_session_id=session.session_id,
            tool=legacy.tool or infer_tool(session.name, session.current_command),
            cwd=cwd,
            project=legacy.project.name if legacy.project else "",
            note=legacy.note,
            tags=legacy.tags,
            state=state,
            pinned=legacy.pinned,
            created_at=session.created_at,
            legacy_last_used=legacy.last_used,
            previous_owner=previous_owner,
            sources=sources,
            warnings=warnings,
        )

    def preview(self, names: list[str] | None = None) -> MigrationPlan:
        sessions = {session.name: session for session in self.backend.list_sessions()}
        if names is None:
            selected = [
                session
                for session in sessions.values()
                if self.store.load(session.name) is None
                and self.legacy.read(session.name) is not None
            ]
        else:
            unique_names = list(dict.fromkeys(names))
            missing = [name for name in unique_names if name not in sessions]
            if missing:
                raise MigrationError(f"tmux sessions not found: {', '.join(missing)}")
            selected = [sessions[name] for name in unique_names]
        if not selected:
            raise MigrationError("no eligible unmanaged sessions found")
        return MigrationPlan.create([self._build_item(session) for session in selected])

    def write_plan(self, plan: MigrationPlan, path: Path) -> None:
        _write_private_json(path, plan.model_dump_json(indent=2))

    def load_plan(self, path: Path) -> MigrationPlan:
        try:
            return MigrationPlan.model_validate_json(_read_private_json(path))
        except ValueError as error:
            raise MigrationError(f"invalid migration plan {path}: {error}") from error

    def validate_plan(self, path: Path) -> MigrationPlan:
        plan = self.load_plan(path)
        current = self.preview([item.name for item in plan.items])
        if current.snapshot_digest != plan.snapshot_digest:
            raise MigrationError("migration snapshot changed; generate and review a new plan")
        journal_path = self._journal_path(plan.plan_id)
        if journal_path.exists() or journal_path.is_symlink():
            raise MigrationError(f"migration journal already exists: {plan.plan_id}")
        return plan

    def _journal_path(self, migration_id: UUID) -> Path:
        return self.paths.migrations_dir / f"{migration_id}.json"

    def _write_journal(self, journal: MigrationJournal) -> None:
        _write_private_json(
            self._journal_path(journal.migration_id), journal.model_dump_json(indent=2)
        )

    def _restore_owner(self, name: str, previous_owner: str | None, expected_id: str) -> None:
        if previous_owner is None:
            self.backend.unset_option(name, OWNER_OPTION, expected_id=expected_id)
        else:
            self.backend.set_option(name, OWNER_OPTION, previous_owner, expected_id=expected_id)

    def apply(self, path: Path) -> MigrationJournal:
        with self._locked():
            return self._apply(path)

    def _rollback_claims(self, claimed: list[MigrationJournalItem]) -> list[str]:
        cleanup_errors: list[str] = []
        for entry in reversed(claimed):
            try:
                current_record = self.store.load(entry.record.name)
                if current_record and current_record.record_id == entry.record.record_id:
                    self.store.delete(entry.record.name)
                self._restore_owner(
                    entry.record.name,
                    entry.previous_owner,
                    entry.record.tmux_session_id,
                )
            except Exception as cleanup_error:  # pragma: no cover - exceptional recovery path
                cleanup_errors.append(f"{entry.record.name}: {cleanup_error}")
        return cleanup_errors

    def _reapply_claims(self, entries: list[MigrationJournalItem]) -> list[str]:
        recovery_errors: list[str] = []
        for entry in entries:
            try:
                self.backend.set_option(
                    entry.record.name,
                    OWNER_OPTION,
                    OWNER_VALUE,
                    expected_id=entry.record.tmux_session_id,
                )
                if self.store.load(entry.record.name) is None:
                    self.store.save_new(entry.record)
            except Exception as recovery_error:  # pragma: no cover - exceptional recovery path
                recovery_errors.append(f"{entry.record.name}: {recovery_error}")
        return recovery_errors

    def _apply(self, path: Path) -> MigrationJournal:
        plan = self.validate_plan(path)

        now = utc_now()
        entries: list[MigrationJournalItem] = []
        for item in plan.items:
            record = SessionMetadata(
                tmux_session_id=item.tmux_session_id,
                name=item.name,
                tool=item.tool,
                cwd=item.cwd,
                project=item.project,
                note=item.note,
                tags=item.tags,
                task_state=normalize_task_state(item.state),
                pinned=item.pinned,
                created_at=item.created_at,
                updated_at=now,
                last_attached_at=item.legacy_last_used,
            )
            entries.append(
                MigrationJournalItem(
                    record=record,
                    record_digest=_record_digest(record),
                    previous_owner=item.previous_owner,
                )
            )
        journal = MigrationJournal(
            migration_id=plan.plan_id,
            snapshot_digest=plan.snapshot_digest,
            status="applying",
            items=entries,
        )
        self._write_journal(journal)

        claimed: list[MigrationJournalItem] = []
        try:
            for entry in entries:
                record = entry.record
                session = self.backend.get_session(record.name)
                if session.session_id != record.tmux_session_id:
                    raise MigrationError(f"tmux ID changed for {record.name}")
                self.backend.set_option(
                    record.name,
                    OWNER_OPTION,
                    OWNER_VALUE,
                    expected_id=record.tmux_session_id,
                )
                claimed.append(entry)
                self.store.save_new(record)
        except Exception as error:
            cleanup_errors = self._rollback_claims(claimed)
            cleanup_detail = (
                f"; rollback failed: {'; '.join(cleanup_errors)}" if cleanup_errors else ""
            )
            failed = journal.model_copy(
                update={
                    "status": "failed" if cleanup_errors else "rolled_back",
                    "updated_at": utc_now(),
                    "error": f"{error}{cleanup_detail}",
                }
            )
            self._write_journal(failed)
            if isinstance(error, WsError) and not cleanup_errors:
                raise
            raise MigrationError(f"migration failed: {error}{cleanup_detail}") from error

        applied = journal.model_copy(update={"status": "applied", "updated_at": utc_now()})
        try:
            self._write_journal(applied)
        except Exception as error:
            cleanup_errors = self._rollback_claims(claimed)
            detail = (
                f"; adoption rollback failed: {'; '.join(cleanup_errors)}"
                if cleanup_errors
                else "; adoption was rolled back"
            )
            if not cleanup_errors:
                recovered = journal.model_copy(
                    update={
                        "status": "rolled_back",
                        "updated_at": utc_now(),
                        "error": f"unable to finalize applied journal: {error}",
                    }
                )
                with suppress(Exception):
                    self._write_journal(recovered)
            raise MigrationError(
                f"unable to finalize migration journal: {error}{detail}"
            ) from error
        return applied

    def load_journal(self, migration_id: UUID) -> MigrationJournal:
        path = self._journal_path(migration_id)
        try:
            return MigrationJournal.model_validate_json(_read_private_json(path))
        except ValueError as error:
            raise MigrationError(f"invalid migration journal {path}: {error}") from error

    def status(self) -> list[MigrationJournal]:
        if not self.paths.migrations_dir.exists():
            return []
        journals: list[MigrationJournal] = []
        for path in sorted(self.paths.migrations_dir.glob("*.json")):
            try:
                journals.append(MigrationJournal.model_validate_json(_read_private_json(path)))
            except MigrationError:
                raise
            except ValueError as error:
                raise MigrationError(f"invalid migration journal {path}: {error}") from error
        return journals

    def rollback(self, migration_id: UUID) -> MigrationJournal:
        with self._locked():
            return self._rollback(migration_id)

    def _rollback(self, migration_id: UUID) -> MigrationJournal:
        journal = self.load_journal(migration_id)
        if journal.status != "applied":
            raise MigrationError(f"migration {migration_id} is not applied")

        for entry in journal.items:
            record = self.store.load(entry.record.name)
            session = self.backend.get_session(entry.record.name)
            marker = self.backend.get_option(
                entry.record.name,
                OWNER_OPTION,
                expected_id=entry.record.tmux_session_id,
            )
            if session.session_id != entry.record.tmux_session_id:
                raise MigrationError(f"tmux ID changed for {entry.record.name}")
            if record is None or record.record_id != entry.record.record_id:
                raise MigrationError(f"migration record changed for {entry.record.name}")
            if _record_digest(record) != entry.record_digest:
                raise MigrationError(f"migration record was modified for {entry.record.name}")
            if marker != OWNER_VALUE:
                raise MigrationError(f"tmux owner marker changed for {entry.record.name}")

        restored: list[MigrationJournalItem] = []
        try:
            for entry in reversed(journal.items):
                self.store.delete(entry.record.name)
                self._restore_owner(
                    entry.record.name,
                    entry.previous_owner,
                    entry.record.tmux_session_id,
                )
                restored.append(entry)
        except Exception as error:
            to_reapply = [entry, *reversed(restored)]
            recovery_errors = self._reapply_claims(to_reapply)
            recovery_detail = (
                f"; adoption recovery failed: {'; '.join(recovery_errors)}"
                if recovery_errors
                else "; adoption was restored"
            )
            failed = journal.model_copy(
                update={
                    "status": "failed" if recovery_errors else "applied",
                    "updated_at": utc_now(),
                    "error": f"rollback failed: {error}{recovery_detail}",
                }
            )
            self._write_journal(failed)
            raise MigrationError(f"rollback failed: {error}{recovery_detail}") from error

        rolled_back = journal.model_copy(
            update={"status": "rolled_back", "updated_at": utc_now(), "error": ""}
        )
        try:
            self._write_journal(rolled_back)
        except Exception as error:
            recovery_errors = self._reapply_claims(list(journal.items))
            detail = (
                f"; adoption recovery failed: {'; '.join(recovery_errors)}"
                if recovery_errors
                else "; adoption was restored"
            )
            if not recovery_errors:
                recovered = journal.model_copy(
                    update={
                        "status": "applied",
                        "updated_at": utc_now(),
                        "error": f"unable to finalize rollback journal: {error}",
                    }
                )
                with suppress(Exception):
                    self._write_journal(recovered)
            raise MigrationError(f"unable to finalize rollback journal: {error}{detail}") from error
        return rolled_back
