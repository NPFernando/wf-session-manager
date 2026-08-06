import json
import os
import tarfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from conftest import FakeBackend
from workspace_session_manager.config import (
    ApprovalConfig,
    AppConfig,
    HealthConfig,
    OperatorProfileConfig,
    PlaybookConfig,
    RemediationChainConfig,
    SelfHealConfig,
    SelfHealRuleConfig,
)
from workspace_session_manager.errors import (
    OwnershipError,
    PresetNotFoundError,
    SessionNotFoundError,
    StateError,
    TmuxError,
    ToolUnavailableError,
    WsError,
)
from workspace_session_manager.legacy import LegacyMetadataReader
from workspace_session_manager.models import (
    CreateRequest,
    DoctorReport,
    HealthCheck,
    HealthStatus,
    InputState,
    OutputSource,
    RuntimeState,
    SessionMetadata,
    TaskState,
    Tool,
)
from workspace_session_manager.paths import AppPaths
from workspace_session_manager.service import LogSearchSummary, SessionService, TailResult
from workspace_session_manager.store import MetadataStore


def test_foreign_session_is_hidden_but_available_for_diagnostics(
    service: SessionService, fake_backend: FakeBackend
) -> None:
    fake_backend.add("claude-existing")
    assert service.list_sessions() == []
    session = service.get("claude-existing", include_unmanaged=True)
    assert not session.owned
    with pytest.raises(OwnershipError, match="not created"):
        service.delete(session.name)
    assert fake_backend.session_exists(session.name)


def test_unmanaged_session_infers_copilot_tool_from_command(
    service: SessionService, fake_backend: FakeBackend
) -> None:
    fake_backend.add("external-agent", command="copilot")
    session = service.get("external-agent", include_unmanaged=True)
    assert not session.owned
    assert session.tool is Tool.COPILOT


def test_managed_lifecycle(
    service: SessionService, fake_backend: FakeBackend, tmp_path: Path
) -> None:
    request = CreateRequest(
        name="API Refactor",
        tool=Tool.CLAUDE,
        cwd=tmp_path,
        note="Initial task",
        tags=["backend"],
    )
    created = service.create(request)
    assert created.name == "claude-api-refactor"
    assert created.display_name == "API Refactor"
    assert created.owned
    assert fake_backend.created_commands[-1] == (("/bin/bash", "-l"), ("/bin/true",))

    service.update_note(created.name, "Updated task")
    organized = service.organize(
        created.name,
        tags=["backend", "urgent"],
        state=TaskState.WAITING,
        input_state=InputState.REQUIRED,
        project="api-platform",
        pinned=True,
    )
    assert organized.note == "Updated task"
    assert organized.tags == ["backend", "urgent"]
    assert organized.pinned
    assert organized.task_state is TaskState.WAITING
    assert organized.input_state is InputState.REQUIRED
    assert organized.project == "api-platform"

    renamed = service.rename(created.name, "api-v2")
    assert renamed.name == "claude-api-v2"
    service.delete(renamed.name)
    assert not fake_backend.session_exists(renamed.name)


def test_stale_record_does_not_grant_ownership(
    service: SessionService, fake_backend: FakeBackend
) -> None:
    session = fake_backend.add("codex-stale", session_id="$live")
    service.store.save(
        SessionMetadata(
            tmux_session_id="$old",
            name=session.name,
            tool=Tool.CODEX,
            cwd=Path("/tmp"),
        )
    )
    assert not service.get(session.name, include_unmanaged=True).owned
    with pytest.raises(OwnershipError):
        service.update_note(session.name, "should fail")


def test_state_failure_rolls_back_only_new_session(
    service: SessionService,
    fake_backend: FakeBackend,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_save(record: SessionMetadata) -> None:
        raise StateError(f"cannot save {record.name}")

    monkeypatch.setattr(service.store, "save_new", fail_save)
    with pytest.raises(StateError):
        service.create(CreateRequest(name="rollback", tool=Tool.SHELL, cwd=tmp_path))
    assert not fake_backend.session_exists("rollback")


def test_missing_tool_fails_before_tmux_creation(
    service: SessionService,
    fake_backend: FakeBackend,
    tmp_path: Path,
) -> None:
    broken = service.config.model_copy(
        update={
            "tools": {
                **service.config.tools,
                Tool.CLAUDE: service.config.tools[Tool.CLAUDE].model_copy(
                    update={"command": ("/definitely/missing",)}
                ),
            }
        }
    )
    service.config = broken
    with pytest.raises(ToolUnavailableError):
        service.create(CreateRequest(name="missing", tool=Tool.CLAUDE, cwd=tmp_path))
    assert fake_backend.sessions == {}


def test_restart_refuses_when_tool_disabled_in_configuration(
    service: SessionService,
    fake_backend: FakeBackend,
    tmp_path: Path,
) -> None:
    created = service.create(CreateRequest(name="disable-me", tool=Tool.CLAUDE, cwd=tmp_path))
    disabled = service.config.model_copy(
        update={
            "tools": {
                **service.config.tools,
                Tool.CLAUDE: service.config.tools[Tool.CLAUDE].model_copy(
                    update={"enabled": False}
                ),
            }
        }
    )
    service.config = disabled
    with pytest.raises(ToolUnavailableError):
        service.restart(created.name)


def test_attach_refuses_name_reused_after_ownership_check(
    service: SessionService,
    fake_backend: FakeBackend,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created = service.create(CreateRequest(name="race", tool=Tool.SHELL, cwd=tmp_path))
    original_get_option = fake_backend.get_option

    def replace_after_check(name: str, option: str, expected_id: str | None = None) -> str | None:
        value = original_get_option(name, option, expected_id=expected_id)
        fake_backend.sessions[name] = fake_backend.sessions[name].model_copy(
            update={"session_id": "$replacement"}
        )
        return value

    monkeypatch.setattr(fake_backend, "get_option", replace_after_check)
    with pytest.raises(TmuxError, match="ID mismatch"):
        service.attach(created.name)
    assert fake_backend.attached == []


def test_runtime_and_activity_come_from_tmux_snapshot(
    service: SessionService,
    fake_backend: FakeBackend,
    tmp_path: Path,
) -> None:
    created = service.create(CreateRequest(name="runtime", tool=Tool.SHELL, cwd=tmp_path))
    activity = created.created_at.replace(year=2025)
    fake_backend.sessions[created.name] = fake_backend.sessions[created.name].model_copy(
        update={"attached_clients": 1, "last_activity_at": activity}
    )
    attached = service.get(created.name)
    assert attached.runtime is RuntimeState.ATTACHED
    assert attached.last_active_at is not None

    fake_backend.sessions[created.name] = fake_backend.sessions[created.name].model_copy(
        update={"attached_clients": 0, "pane_dead": True, "pane_dead_status": 7}
    )
    assert service.get(created.name).runtime is RuntimeState.FAILED


def test_logs_are_sanitized_and_report_truncation(
    service: SessionService,
    fake_backend: FakeBackend,
    tmp_path: Path,
) -> None:
    created = service.create(
        CreateRequest(name="logs", tool=Tool.SHELL, cwd=tmp_path, logging_enabled=False)
    )
    fake_backend.previews[created.name] = "\n".join(
        ["password=private", *[f"line-{index}" for index in range(600)]]
    )
    details = service.logs(created.name)
    assert details.preview_truncated
    assert details.output_source is OutputSource.PANE
    assert details.available_sources == (OutputSource.PANE,)
    assert "private" not in details.preview
    assert len(details.preview.splitlines()) == service.config.log_lines


def test_inspect_snapshot_is_bounded_and_exact_id_guarded(
    service: SessionService,
    fake_backend: FakeBackend,
    tmp_path: Path,
) -> None:
    created = service.create(CreateRequest(name="attention", tool=Tool.CLAUDE, cwd=tmp_path))
    snapshot = service.get(created.name)
    fake_backend.previews[created.name] = "password=private\nfirst\nsecond\nthird"

    details = service.inspect_snapshot(snapshot, preview_lines=2, preview_bytes=64)
    assert details.preview == "second\nthird"
    assert details.preview_truncated
    assert "private" not in details.preview

    fake_backend.sessions[created.name] = fake_backend.sessions[created.name].model_copy(
        update={"session_id": "$replacement"}
    )
    with pytest.raises(TmuxError, match="ID mismatch"):
        service.inspect_snapshot(snapshot, preview_lines=20, preview_bytes=8_192)


def test_logs_expose_live_and_saved_sources(
    service: SessionService,
    fake_backend: FakeBackend,
    tmp_path: Path,
) -> None:
    created = service.create(
        CreateRequest(name="sources", tool=Tool.SHELL, cwd=tmp_path, logging_enabled=True)
    )
    record = service.store.load(created.name)
    assert record is not None
    log_path = service.paths.logs_dir / f"{record.record_id}.log"
    log_path.write_text("saved line\npassword=private\n", encoding="utf-8")
    fake_backend.previews[created.name] = "live line"

    automatic = service.logs(created.name)
    assert automatic.output_source is OutputSource.SAVED
    assert automatic.available_sources == (OutputSource.PANE, OutputSource.SAVED)
    assert "saved line" in automatic.preview
    assert "private" not in automatic.preview

    live = service.logs(created.name, source=OutputSource.PANE)
    assert live.output_source is OutputSource.PANE
    assert live.preview == "live line"

    saved = service.logs(created.name, source=OutputSource.SAVED)
    assert saved.output_source is OutputSource.SAVED
    assert "saved line" in saved.preview


def test_logs_reject_unavailable_sources_and_use_saved_output_when_stopped(
    service: SessionService,
    fake_backend: FakeBackend,
    tmp_path: Path,
) -> None:
    created = service.create(
        CreateRequest(name="stopped-logs", tool=Tool.SHELL, cwd=tmp_path, logging_enabled=False)
    )
    with pytest.raises(StateError, match="saved log unavailable"):
        service.logs(created.name, source=OutputSource.SAVED)

    record = service.store.load(created.name)
    assert record is not None
    service.paths.logs_dir.mkdir(parents=True, exist_ok=True)
    log_path = service.paths.logs_dir / f"{record.record_id}.log"
    log_path.write_text("retained output\n", encoding="utf-8")
    service.stop_session(created.name)

    stopped = service.logs(created.name)
    assert stopped.output_source is OutputSource.SAVED
    assert stopped.available_sources == (OutputSource.SAVED,)
    assert stopped.preview == "retained output"
    with pytest.raises(SessionNotFoundError, match="live pane unavailable"):
        service.logs(created.name, source=OutputSource.PANE)


def test_live_logs_ignore_an_unsafe_saved_source(
    service: SessionService,
    fake_backend: FakeBackend,
    tmp_path: Path,
) -> None:
    created = service.create(
        CreateRequest(name="unsafe-log", tool=Tool.SHELL, cwd=tmp_path, logging_enabled=False)
    )
    record = service.store.load(created.name)
    assert record is not None
    service.paths.logs_dir.mkdir(parents=True, exist_ok=True)
    log_path = service.paths.logs_dir / f"{record.record_id}.log"
    log_path.symlink_to(tmp_path / "elsewhere.log")
    fake_backend.previews[created.name] = "safe pane"

    live = service.logs(created.name, source=OutputSource.PANE)
    assert live.preview == "safe pane"
    assert live.available_sources == (OutputSource.PANE,)
    with pytest.raises(StateError, match="saved log unavailable"):
        service.logs(created.name, source=OutputSource.SAVED)


def test_tail_log_reads_only_newly_appended_bytes(
    service: SessionService,
    fake_backend: FakeBackend,
    tmp_path: Path,
) -> None:
    created = service.create(
        CreateRequest(name="tail-basic", tool=Tool.SHELL, cwd=tmp_path, logging_enabled=True)
    )
    record = service.store.load(created.name)
    assert record is not None
    log_path = service.paths.logs_dir / f"{record.record_id}.log"
    log_path.write_text("first line\n", encoding="utf-8")

    first = service.tail_log(created.name, 0)
    assert first.text == "first line\n"
    assert not first.rotated
    assert first.offset == log_path.stat().st_size

    unchanged = service.tail_log(created.name, first.offset)
    assert unchanged.text == ""
    assert unchanged.offset == first.offset
    assert not unchanged.rotated

    with log_path.open("a", encoding="utf-8") as handle:
        handle.write("second line\n")

    second = service.tail_log(created.name, first.offset)
    assert second.text == "second line\n"
    assert not second.rotated
    assert second.offset == log_path.stat().st_size


def test_tail_log_redacts_newly_appended_secrets(
    service: SessionService,
    fake_backend: FakeBackend,
    tmp_path: Path,
) -> None:
    created = service.create(
        CreateRequest(name="tail-redact", tool=Tool.SHELL, cwd=tmp_path, logging_enabled=True)
    )
    record = service.store.load(created.name)
    assert record is not None
    log_path = service.paths.logs_dir / f"{record.record_id}.log"

    result = service.tail_log(created.name, 0)
    assert result.text == ""

    with log_path.open("a", encoding="utf-8") as handle:
        handle.write("password=hunter2\n")

    tailed = service.tail_log(created.name, result.offset)
    assert "hunter2" not in tailed.text
    assert "[REDACTED]" in tailed.text


def test_tail_log_detects_rotation_and_resyncs(
    service: SessionService,
    fake_backend: FakeBackend,
    tmp_path: Path,
) -> None:
    created = service.create(
        CreateRequest(name="tail-rotate", tool=Tool.SHELL, cwd=tmp_path, logging_enabled=True)
    )
    record = service.store.load(created.name)
    assert record is not None
    log_path = service.paths.logs_dir / f"{record.record_id}.log"
    log_path.write_text("a" * 500, encoding="utf-8")
    stale_offset = log_path.stat().st_size

    log_path.write_text("rotated content\n", encoding="utf-8")

    result = service.tail_log(created.name, stale_offset)
    assert result.rotated
    assert result.text == "rotated content"
    assert result.offset == log_path.stat().st_size


def test_tail_log_missing_session_or_log_returns_empty(
    service: SessionService,
    fake_backend: FakeBackend,
    tmp_path: Path,
) -> None:
    assert service.tail_log("does-not-exist", 0) == TailResult(text="", offset=0, rotated=False)

    created = service.create(
        CreateRequest(name="tail-no-log", tool=Tool.SHELL, cwd=tmp_path, logging_enabled=False)
    )
    assert service.tail_log(created.name, 0) == TailResult(text="", offset=0, rotated=False)


def test_search_logs_finds_matches_with_surrounding_context(
    service: SessionService,
    fake_backend: FakeBackend,
    tmp_path: Path,
) -> None:
    created = service.create(
        CreateRequest(name="search-one", tool=Tool.SHELL, cwd=tmp_path, logging_enabled=True)
    )
    record = service.store.load(created.name)
    assert record is not None
    log_path = service.paths.logs_dir / f"{record.record_id}.log"
    log_path.write_text(
        "before one\nbefore two\nthe needle is here\nafter one\nafter two\n", encoding="utf-8"
    )

    summary = service.search_logs("needle", service.list_sessions())
    assert summary.skipped_no_log == 0
    assert len(summary.results) == 1
    result = summary.results[0]
    assert result.name == created.name
    assert len(result.matches) == 1
    match = result.matches[0]
    assert match.line == "the needle is here"
    assert match.line_number == 3
    assert match.context_before == ("before one", "before two")
    assert match.context_after == ("after one", "after two")


def test_search_logs_is_case_insensitive(
    service: SessionService,
    fake_backend: FakeBackend,
    tmp_path: Path,
) -> None:
    created = service.create(
        CreateRequest(name="search-case", tool=Tool.SHELL, cwd=tmp_path, logging_enabled=True)
    )
    record = service.store.load(created.name)
    assert record is not None
    log_path = service.paths.logs_dir / f"{record.record_id}.log"
    log_path.write_text("Something NEEDLE-like appears\n", encoding="utf-8")

    summary = service.search_logs("needle", service.list_sessions())
    assert len(summary.results) == 1
    assert summary.results[0].matches[0].line == "Something NEEDLE-like appears"


def test_search_logs_skips_sessions_without_captured_output(
    service: SessionService,
    fake_backend: FakeBackend,
    tmp_path: Path,
) -> None:
    logged = service.create(
        CreateRequest(name="search-logged", tool=Tool.SHELL, cwd=tmp_path, logging_enabled=True)
    )
    service.create(
        CreateRequest(name="search-unlogged", tool=Tool.SHELL, cwd=tmp_path, logging_enabled=False)
    )
    record = service.store.load(logged.name)
    assert record is not None
    log_path = service.paths.logs_dir / f"{record.record_id}.log"
    log_path.write_text("needle appears here\n", encoding="utf-8")

    summary = service.search_logs("needle", service.list_sessions())
    assert len(summary.results) == 1
    assert summary.results[0].name == logged.name
    assert summary.skipped_no_log == 1


def test_search_logs_redacts_secrets_in_matched_context(
    service: SessionService,
    fake_backend: FakeBackend,
    tmp_path: Path,
) -> None:
    created = service.create(
        CreateRequest(name="search-secret", tool=Tool.SHELL, cwd=tmp_path, logging_enabled=True)
    )
    record = service.store.load(created.name)
    assert record is not None
    log_path = service.paths.logs_dir / f"{record.record_id}.log"
    log_path.write_text("password=hunter2\nneedle here\n", encoding="utf-8")

    summary = service.search_logs("needle", service.list_sessions())
    match = summary.results[0].matches[0]
    assert "hunter2" not in match.context_before[0]
    assert "[REDACTED]" in match.context_before[0]


def test_search_logs_caps_matches_per_session(
    service: SessionService,
    fake_backend: FakeBackend,
    tmp_path: Path,
) -> None:
    created = service.create(
        CreateRequest(name="search-many", tool=Tool.SHELL, cwd=tmp_path, logging_enabled=True)
    )
    record = service.store.load(created.name)
    assert record is not None
    log_path = service.paths.logs_dir / f"{record.record_id}.log"
    log_path.write_text("\n".join(f"needle {index}" for index in range(10)), encoding="utf-8")

    summary = service.search_logs("needle", service.list_sessions(), max_matches=3)
    assert len(summary.results[0].matches) == 3


def test_search_logs_empty_query_returns_empty_summary(
    service: SessionService,
    fake_backend: FakeBackend,
    tmp_path: Path,
) -> None:
    service.create(
        CreateRequest(name="search-empty", tool=Tool.SHELL, cwd=tmp_path, logging_enabled=True)
    )
    summary = service.search_logs("   ", service.list_sessions())
    assert summary == LogSearchSummary(results=(), skipped_no_log=0)


def test_create_validation_detects_duplicate_directory_and_git_project(
    service: SessionService,
    tmp_path: Path,
) -> None:
    project = tmp_path / "api-platform"
    nested = project / "src"
    nested.mkdir(parents=True)
    (project / ".git").mkdir()
    validation = service.validate_create(Tool.CLAUDE, "api", nested)
    assert validation.valid
    assert validation.normalized_name == "claude-api"
    assert validation.cwd == nested
    assert validation.detected_project == "api-platform"

    service.create(CreateRequest(name="api", tool=Tool.CLAUDE, cwd=nested))
    duplicate = service.validate_create(Tool.CLAUDE, "api", nested)
    assert not duplicate.valid
    assert duplicate.name_error
    assert not duplicate.cwd_error
    assert "already exists" in duplicate.errors[0]

    missing = service.validate_create(Tool.CLAUDE, "api-refactor", nested / "missing")
    assert not missing.valid
    assert not missing.name_error
    assert missing.cwd_error


def test_create_collision_uses_final_normalized_name(
    service: SessionService,
    tmp_path: Path,
) -> None:
    service.create(CreateRequest(name="api_refactor", tool=Tool.CLAUDE, cwd=tmp_path))
    duplicate = service.validate_create(Tool.CLAUDE, "api-refactor", tmp_path)
    assert duplicate.normalized_name == "claude-api-refactor"
    assert "already exists" in duplicate.name_error

    without_prefix = service.validate_create(
        Tool.CLAUDE, "api-refactor", tmp_path, automatic_prefix=False
    )
    assert without_prefix.valid
    assert without_prefix.normalized_name == "api-refactor"


def test_rename_validation_and_display_name_update(
    service: SessionService,
    tmp_path: Path,
) -> None:
    current = service.create(CreateRequest(name="current", tool=Tool.CLAUDE, cwd=tmp_path))
    service.create(CreateRequest(name="existing", tool=Tool.CLAUDE, cwd=tmp_path))

    valid = service.validate_rename(current.name, "api_refactor")
    assert valid.valid
    assert valid.normalized_name == "claude-api-refactor"

    duplicate = service.validate_rename(current.name, "existing")
    assert not duplicate.valid
    assert duplicate.normalized_name == "claude-existing"
    assert "already exists" in duplicate.name_error

    unchanged = service.validate_rename(current.name, current.name)
    assert unchanged.valid
    assert unchanged.normalized_name == current.name

    updated = service.organize(current.name, display_name="API Refactor")
    assert updated.display_name == "API Refactor"


def test_project_detection_uses_metadata_and_never_names_home_ubuntu(
    service: SessionService,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "metadata-project"
    project.mkdir()
    (project / "pyproject.toml").write_text(
        '[project]\nname = "metadata-project"\n', encoding="utf-8"
    )
    inherited_git_marker = next(
        (candidate / ".git" for candidate in project.parents if (candidate / ".git").exists()),
        None,
    )
    if inherited_git_marker is not None:
        original_is_dir = Path.is_dir

        def is_dir_without_inherited_marker(path: Path) -> bool:
            return path != inherited_git_marker and original_is_dir(path)

        monkeypatch.setattr(Path, "is_dir", is_dir_without_inherited_marker)
    assert service.detect_project(project) == "metadata-project"
    assert service.detect_project(Path.home()) == ""


def test_diagnostics_classifies_environment_facts_as_information(
    service: SessionService,
) -> None:
    report = service.doctor()
    statuses = {check.name: check.status for check in report.checks}
    assert statuses["unmanaged-sessions"] is HealthStatus.INFO
    assert statuses["legacy-readonly"] is HealthStatus.INFO


def test_logging_stop_restart_and_metadata_removal_are_explicit(
    service: SessionService,
    fake_backend: FakeBackend,
    tmp_path: Path,
) -> None:
    created = service.create(
        CreateRequest(
            name="protected",
            tool=Tool.CODEX,
            cwd=tmp_path,
            logging_enabled=True,
        )
    )
    assert created.logging_enabled
    record = service.store.load(created.name)
    assert record is not None
    log_path = service.paths.logs_dir / f"{record.record_id}.log"
    assert log_path.is_file()
    assert log_path.stat().st_mode & 0o777 == 0o600

    service.stop_command(created.name)
    assert fake_backend.interrupted == [created.name]

    stopped = service.stop_session(created.name)
    assert stopped.runtime is RuntimeState.STOPPED
    assert not fake_backend.session_exists(created.name)
    organized = service.organize(created.name, state=TaskState.WAITING)
    assert organized.task_state is TaskState.WAITING

    restarted = service.restart(created.name)
    assert restarted.runtime is RuntimeState.DETACHED
    assert restarted.session_id != created.session_id
    service.remove_metadata(created.name)
    assert fake_backend.session_exists(created.name)
    assert fake_backend.get_option(created.name, "@wf_owner") is None
    assert service.list_sessions() == []


def test_delete_logs_preserves_enabled_logging(
    service: SessionService,
    tmp_path: Path,
) -> None:
    created = service.create(
        CreateRequest(name="logged", tool=Tool.SHELL, cwd=tmp_path, logging_enabled=True)
    )
    record = service.store.load(created.name)
    assert record is not None
    path = service.paths.logs_dir / f"{record.record_id}.log"
    path.write_text("sanitized output\n", encoding="utf-8")
    updated = service.delete_logs(created.name)
    assert updated.logging_enabled
    assert path.read_text(encoding="utf-8") == ""


def test_undo_restores_pin_status_logging_and_interrupt_runtime(
    service: SessionService,
    fake_backend: FakeBackend,
    tmp_path: Path,
) -> None:
    created = service.create(
        CreateRequest(name="undo-stack", tool=Tool.SHELL, cwd=tmp_path, logging_enabled=True)
    )
    service.organize(created.name, pinned=True)
    assert service.get(created.name).pinned
    assert "restored pin state" in service.undo_last()
    assert not service.get(created.name).pinned

    service.organize(created.name, state=TaskState.BLOCKED, input_state=InputState.REQUIRED)
    blocked = service.get(created.name)
    assert blocked.task_state is TaskState.BLOCKED
    assert blocked.input_state is InputState.REQUIRED
    assert "restored status state" in service.undo_last()
    reverted = service.get(created.name)
    assert reverted.task_state is TaskState.IN_PROGRESS
    assert reverted.input_state is InputState.NONE

    service.set_logging(created.name, False)
    assert not service.get(created.name).logging_enabled
    assert "restored logging mode" in service.undo_last()
    assert service.get(created.name).logging_enabled

    service.stop_command(created.name)
    assert fake_backend.interrupted == [created.name]
    assert "restored runtime after interrupt" in service.undo_last()


def test_live_restart_reestablishes_logging(
    service: SessionService,
    fake_backend: FakeBackend,
    tmp_path: Path,
) -> None:
    created = service.create(
        CreateRequest(name="restart-logging", tool=Tool.SHELL, cwd=tmp_path, logging_enabled=True)
    )
    restarted = service.restart(created.name)
    assert restarted.logging_enabled
    assert fake_backend.restarted == [created.name]
    assert created.name in fake_backend.logging_paths


def test_stopped_restart_rolls_back_new_tmux_session_when_state_save_fails(
    service: SessionService,
    fake_backend: FakeBackend,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created = service.create(CreateRequest(name="restart-rollback", tool=Tool.SHELL, cwd=tmp_path))
    service.stop_session(created.name)
    original = service.store.load(created.name)
    assert original is not None

    def fail_save(record: SessionMetadata) -> None:
        del record
        raise StateError("simulated save failure")

    monkeypatch.setattr(service.store, "save", fail_save)
    with pytest.raises(StateError, match="simulated save failure"):
        service.restart(created.name)
    assert not fake_backend.session_exists(created.name)
    assert service.store.load(created.name) == original


def test_privacy_safe_diagnostic_export_redacts_sensitive_values(
    service: SessionService,
) -> None:
    report = DoctorReport(
        checks=[
            HealthCheck(
                name="state",
                status=HealthStatus.FAIL,
                detail=f"{Path.home()}/private password=not-safe 192.168.1.1",
                corrective_action="Repair state.",
            )
        ]
    )
    destination = service.export_doctor_report(report)
    exported = destination.read_text(encoding="utf-8")
    assert str(Path.home()) not in exported
    assert "not-safe" not in exported
    assert "192.168.1.1" not in exported
    assert destination.stat().st_mode & 0o777 == 0o600


def _disk_only_service(tmp_path: Path, fake_backend: FakeBackend) -> SessionService:
    """A service with every health check but disk-space disabled, so tests
    exercise the shared cache/TTL machinery without faking subprocesses."""
    config = AppConfig(
        legacy_state_dirs=(),
        health=HealthConfig(
            apt_updates_enabled=False,
            reboot_required_enabled=False,
            git_dirty_enabled=False,
            docker_enabled=False,
            zombie_sessions_enabled=False,
            idle_sessions_enabled=False,
            orphaned_logs_enabled=False,
            missing_cwd_enabled=False,
            disk_ttl_seconds=5.0,
        ),
    )
    paths = AppPaths(tmp_path / "config", tmp_path / "state", tmp_path / "cache")
    return SessionService(
        backend=fake_backend,
        store=MetadataStore(paths),
        config=config,
        paths=paths,
        legacy=LegacyMetadataReader(()),
    )


def test_cached_health_alerts_reports_not_yet_checked_when_uncached(
    tmp_path: Path, fake_backend: FakeBackend
) -> None:
    service = _disk_only_service(tmp_path, fake_backend)
    checks = service.cached_health_alerts()
    assert [check.name for check in checks] == ["disk-space"]
    assert checks[0].status is HealthStatus.INFO
    assert checks[0].detail == "not yet checked"


def test_refresh_health_alerts_writes_cache_that_cached_health_alerts_then_reads(
    tmp_path: Path, fake_backend: FakeBackend
) -> None:
    service = _disk_only_service(tmp_path, fake_backend)
    refreshed = service.refresh_health_alerts(force=True)
    assert refreshed[0].name == "disk-space"
    assert refreshed[0].detail.endswith("% available")

    cached = service.cached_health_alerts()
    assert cached[0].detail == refreshed[0].detail
    assert cached[0].status == refreshed[0].status


def test_refresh_health_alerts_skips_disabled_checks(
    tmp_path: Path, fake_backend: FakeBackend
) -> None:
    config = AppConfig(
        legacy_state_dirs=(),
        health=HealthConfig(enabled=True, disk_space_enabled=False, docker_enabled=False),
    )
    paths = AppPaths(tmp_path / "config", tmp_path / "state", tmp_path / "cache")
    service = SessionService(
        backend=fake_backend,
        store=MetadataStore(paths),
        config=config,
        paths=paths,
        legacy=LegacyMetadataReader(()),
    )
    names = {check.name for check in service.refresh_health_alerts(force=True)}
    assert "disk-space" not in names
    assert "docker-containers" not in names


def test_health_enabled_false_is_a_master_switch_over_every_check(
    tmp_path: Path, fake_backend: FakeBackend
) -> None:
    config = AppConfig(legacy_state_dirs=(), health=HealthConfig(enabled=False))
    paths = AppPaths(tmp_path / "config", tmp_path / "state", tmp_path / "cache")
    service = SessionService(
        backend=fake_backend,
        store=MetadataStore(paths),
        config=config,
        paths=paths,
        legacy=LegacyMetadataReader(()),
    )
    assert service.cached_health_alerts() == []
    assert service.refresh_health_alerts(force=True) == []
    assert service.health_stale_names(datetime.now(UTC)) == frozenset()


def test_health_stale_names_respects_ttl(tmp_path: Path, fake_backend: FakeBackend) -> None:
    service = _disk_only_service(tmp_path, fake_backend)
    now = datetime.now(UTC)
    assert service.health_stale_names(now) == frozenset({"disk-space"})

    service.refresh_health_alerts(force=True)
    assert service.health_stale_names(now) == frozenset()

    later = now + timedelta(seconds=6)
    assert service.health_stale_names(later) == frozenset({"disk-space"})


def test_cached_health_alerts_treats_corrupt_cache_file_as_uncached(
    tmp_path: Path, fake_backend: FakeBackend
) -> None:
    service = _disk_only_service(tmp_path, fake_backend)
    service.paths.health_dir.mkdir(parents=True)
    (service.paths.health_dir / "disk-space.json").write_text("not json", encoding="utf-8")
    checks = service.cached_health_alerts()
    assert checks[0].detail == "not yet checked"


def test_write_health_cache_refuses_to_follow_a_symlinked_cache_file(
    tmp_path: Path, fake_backend: FakeBackend
) -> None:
    service = _disk_only_service(tmp_path, fake_backend)
    service.paths.health_dir.mkdir(parents=True)
    outside_target = tmp_path / "outside.json"
    outside_target.write_text("not touched", encoding="utf-8")
    (service.paths.health_dir / "disk-space.json").symlink_to(outside_target)

    service.refresh_health_alerts(force=True)

    assert outside_target.read_text(encoding="utf-8") == "not touched"


def test_read_health_cache_ignores_a_symlinked_cache_file(
    tmp_path: Path, fake_backend: FakeBackend
) -> None:
    service = _disk_only_service(tmp_path, fake_backend)
    service.paths.health_dir.mkdir(parents=True)
    outside_target = tmp_path / "outside.json"
    outside_target.write_text(
        json.dumps({"checked_at": "2020-01-01T00:00:00+00:00", "check": {}}), encoding="utf-8"
    )
    (service.paths.health_dir / "disk-space.json").symlink_to(outside_target)

    checks = service.cached_health_alerts()

    assert checks[0].detail == "not yet checked"


def test_refresh_health_alerts_isolates_check_that_raises_unexpectedly(
    tmp_path: Path, fake_backend: FakeBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _disk_only_service(tmp_path, fake_backend)

    def explode(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr("workspace_session_manager.service.disk_space_check", explode)
    checks = service.refresh_health_alerts(force=True)
    assert checks[0].name == "disk-space"
    assert checks[0].status is HealthStatus.INFO
    assert checks[0].detail == "check failed unexpectedly"


def _hygiene_only_service(
    tmp_path: Path, fake_backend: FakeBackend, **health_overrides: object
) -> SessionService:
    """A service with every VM-level check disabled, isolating the three
    session-hygiene checks (which don't shell out, unlike disk/apt/docker/git)."""
    config = AppConfig(
        legacy_state_dirs=(),
        health=HealthConfig(
            disk_space_enabled=False,
            apt_updates_enabled=False,
            reboot_required_enabled=False,
            git_dirty_enabled=False,
            docker_enabled=False,
            missing_cwd_enabled=False,
            **health_overrides,
        ),
    )
    paths = AppPaths(tmp_path / "config", tmp_path / "state", tmp_path / "cache")
    return SessionService(
        backend=fake_backend,
        store=MetadataStore(paths),
        config=config,
        paths=paths,
        legacy=LegacyMetadataReader(()),
    )


def test_zombie_sessions_check_is_wired_and_disableable(
    tmp_path: Path, fake_backend: FakeBackend
) -> None:
    service = _hygiene_only_service(
        tmp_path, fake_backend, idle_sessions_enabled=False, orphaned_logs_enabled=False
    )
    checks = service.refresh_health_alerts(force=True)
    assert [check.name for check in checks] == ["zombie-sessions"]
    assert checks[0].status is HealthStatus.PASS

    disabled = _hygiene_only_service(
        tmp_path,
        fake_backend,
        zombie_sessions_enabled=False,
        idle_sessions_enabled=False,
        orphaned_logs_enabled=False,
    )
    assert disabled.refresh_health_alerts(force=True) == []


def test_zombie_sessions_check_warns_on_stale_stopped_session(
    tmp_path: Path, fake_backend: FakeBackend
) -> None:
    service = _hygiene_only_service(
        tmp_path,
        fake_backend,
        idle_sessions_enabled=False,
        orphaned_logs_enabled=False,
        zombie_stale_after_days=1,
    )
    created = service.create(CreateRequest(name="stale", tool=Tool.SHELL, cwd=tmp_path))
    record = service.store.load(created.name)
    assert record is not None
    stale_record = record.model_copy(
        update={
            "last_attached_at": datetime.now(UTC) - timedelta(days=2),
            "updated_at": datetime.now(UTC) - timedelta(days=2),
        }
    )
    service.store.save(stale_record)
    fake_backend.sessions.pop(created.name, None)

    checks = service.refresh_health_alerts(force=True)
    assert checks[0].name == "zombie-sessions"
    assert checks[0].status is HealthStatus.WARN
    assert created.name in checks[0].detail


def test_idle_sessions_check_is_wired_and_disableable(
    tmp_path: Path, fake_backend: FakeBackend
) -> None:
    service = _hygiene_only_service(
        tmp_path, fake_backend, zombie_sessions_enabled=False, orphaned_logs_enabled=False
    )
    checks = service.refresh_health_alerts(force=True)
    assert [check.name for check in checks] == ["idle-sessions"]
    assert checks[0].status is HealthStatus.PASS

    disabled = _hygiene_only_service(
        tmp_path,
        fake_backend,
        zombie_sessions_enabled=False,
        idle_sessions_enabled=False,
        orphaned_logs_enabled=False,
    )
    assert disabled.refresh_health_alerts(force=True) == []


def test_idle_sessions_check_warns_on_idle_live_session(
    tmp_path: Path, fake_backend: FakeBackend
) -> None:
    service = _hygiene_only_service(
        tmp_path,
        fake_backend,
        zombie_sessions_enabled=False,
        orphaned_logs_enabled=False,
        idle_after_days=1,
    )
    created = service.create(CreateRequest(name="idle", tool=Tool.SHELL, cwd=tmp_path))
    record = service.store.load(created.name)
    assert record is not None
    idle_record = record.model_copy(
        update={
            "last_attached_at": datetime.now(UTC) - timedelta(days=2),
            "updated_at": datetime.now(UTC) - timedelta(days=2),
        }
    )
    service.store.save(idle_record)

    checks = service.refresh_health_alerts(force=True)
    assert checks[0].name == "idle-sessions"
    assert checks[0].status is HealthStatus.WARN
    assert created.name in checks[0].detail


def test_orphaned_logs_check_is_wired_and_disableable(
    tmp_path: Path, fake_backend: FakeBackend
) -> None:
    service = _hygiene_only_service(
        tmp_path, fake_backend, zombie_sessions_enabled=False, idle_sessions_enabled=False
    )
    checks = service.refresh_health_alerts(force=True)
    assert [check.name for check in checks] == ["orphaned-logs"]
    assert checks[0].status is HealthStatus.PASS

    disabled = _hygiene_only_service(
        tmp_path,
        fake_backend,
        zombie_sessions_enabled=False,
        idle_sessions_enabled=False,
        orphaned_logs_enabled=False,
    )
    assert disabled.refresh_health_alerts(force=True) == []


def test_orphaned_logs_check_warns_on_unknown_log_file(
    tmp_path: Path, fake_backend: FakeBackend
) -> None:
    service = _hygiene_only_service(
        tmp_path,
        fake_backend,
        zombie_sessions_enabled=False,
        idle_sessions_enabled=False,
        orphaned_logs_min_age_hours=1,
    )
    service.paths.logs_dir.mkdir(parents=True, exist_ok=True)
    orphan = service.paths.logs_dir / "00000000-0000-0000-0000-000000000000.log"
    orphan.write_text("leftover output", encoding="utf-8")
    old_time = (datetime.now(UTC) - timedelta(days=2)).timestamp()
    os.utime(orphan, (old_time, old_time))

    checks = service.refresh_health_alerts(force=True)
    assert checks[0].name == "orphaned-logs"
    assert checks[0].status is HealthStatus.WARN


def test_apply_health_fix_deletes_stale_zombie_sessions(
    tmp_path: Path, fake_backend: FakeBackend
) -> None:
    service = _hygiene_only_service(
        tmp_path,
        fake_backend,
        idle_sessions_enabled=False,
        orphaned_logs_enabled=False,
        zombie_stale_after_days=1,
    )
    created = service.create(CreateRequest(name="stale", tool=Tool.SHELL, cwd=tmp_path))
    record = service.store.load(created.name)
    assert record is not None
    stale_record = record.model_copy(
        update={
            "last_attached_at": datetime.now(UTC) - timedelta(days=2),
            "updated_at": datetime.now(UTC) - timedelta(days=2),
        }
    )
    service.store.save(stale_record)
    fake_backend.sessions.pop(created.name, None)

    refreshed = service.apply_health_fix("zombie-sessions")
    assert refreshed.status is HealthStatus.PASS
    assert service.store.load(created.name) is None


def test_apply_health_fix_deletes_orphaned_log_files(
    tmp_path: Path, fake_backend: FakeBackend
) -> None:
    service = _hygiene_only_service(
        tmp_path,
        fake_backend,
        zombie_sessions_enabled=False,
        idle_sessions_enabled=False,
        orphaned_logs_min_age_hours=1,
    )
    service.paths.logs_dir.mkdir(parents=True, exist_ok=True)
    orphan = service.paths.logs_dir / "00000000-0000-0000-0000-000000000000.log"
    orphan.write_text("leftover output", encoding="utf-8")
    old_time = (datetime.now(UTC) - timedelta(days=2)).timestamp()
    os.utime(orphan, (old_time, old_time))

    refreshed = service.apply_health_fix("orphaned-logs")
    assert refreshed.status is HealthStatus.PASS
    assert not orphan.exists()


def test_apply_health_fix_on_already_clean_check_raises(
    tmp_path: Path, fake_backend: FakeBackend
) -> None:
    service = _hygiene_only_service(
        tmp_path, fake_backend, idle_sessions_enabled=False, orphaned_logs_enabled=False
    )
    with pytest.raises(WsError, match="no automatic fix"):
        service.apply_health_fix("zombie-sessions")


def test_apply_health_fix_rejects_unknown_check(tmp_path: Path, fake_backend: FakeBackend) -> None:
    service = _hygiene_only_service(tmp_path, fake_backend)
    with pytest.raises(WsError, match="unknown or disabled"):
        service.apply_health_fix("does-not-exist")


def test_apply_health_fix_rejects_disabled_check(tmp_path: Path, fake_backend: FakeBackend) -> None:
    service = _hygiene_only_service(tmp_path, fake_backend, zombie_sessions_enabled=False)
    with pytest.raises(WsError, match="unknown or disabled"):
        service.apply_health_fix("zombie-sessions")


def test_apply_health_fix_rejects_non_fixable_check(
    tmp_path: Path, fake_backend: FakeBackend
) -> None:
    service = _hygiene_only_service(
        tmp_path, fake_backend, zombie_sessions_enabled=False, orphaned_logs_enabled=False
    )
    with pytest.raises(WsError, match="no automatic fix"):
        service.apply_health_fix("idle-sessions")


def test_session_hygiene_checks_isolate_unexpected_failures(
    tmp_path: Path, fake_backend: FakeBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _hygiene_only_service(
        tmp_path, fake_backend, idle_sessions_enabled=False, orphaned_logs_enabled=False
    )

    def explode(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr("workspace_session_manager.service.zombie_sessions_check", explode)
    checks = service.refresh_health_alerts(force=True)
    assert checks[0].name == "zombie-sessions"
    assert checks[0].status is HealthStatus.INFO
    assert checks[0].detail == "check failed unexpectedly"


def test_doctor_disk_space_thresholds_are_configurable(
    tmp_path: Path, fake_backend: FakeBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = AppConfig(
        legacy_state_dirs=(),
        health=HealthConfig(disk_warn_percent=60, disk_fail_percent=40),
    )
    paths = AppPaths(tmp_path / "config", tmp_path / "state", tmp_path / "cache")
    service = SessionService(
        backend=fake_backend,
        store=MetadataStore(paths),
        config=config,
        paths=paths,
        legacy=LegacyMetadataReader(()),
    )

    class FakeUsage:
        total = 100
        free = 50
        used = 50

    monkeypatch.setattr("shutil.disk_usage", lambda _root: FakeUsage())
    report = service.doctor()
    disk_check = next(check for check in report.checks if check.name == "disk-space")
    assert disk_check.status is HealthStatus.WARN


def test_save_preset_then_get_preset_round_trips(service: SessionService, tmp_path: Path) -> None:
    saved = service.save_preset(
        "Backend Dev",
        tool=Tool.SHELL,
        cwd=tmp_path,
        project="api",
        tags=["backend", "urgent"],
        logging_enabled=False,
    )
    assert saved.name == "backend-dev"
    fetched = service.get_preset("backend-dev")
    assert fetched == saved
    assert fetched.project == "api"
    assert fetched.tags == ["backend", "urgent"]
    assert fetched.logging_enabled is False


def test_save_preset_rejects_names_that_normalize_empty(
    service: SessionService, tmp_path: Path
) -> None:
    with pytest.raises(WsError, match="invalid preset name"):
        service.save_preset("!!!", tool=Tool.SHELL, cwd=tmp_path)


def test_save_preset_overwrites_existing_preset_by_name(
    service: SessionService, tmp_path: Path
) -> None:
    service.save_preset("backend-dev", tool=Tool.SHELL, cwd=tmp_path)
    updated = service.save_preset("backend-dev", tool=Tool.CODEX, cwd=tmp_path)
    assert service.get_preset("backend-dev").tool is Tool.CODEX
    assert updated.tool is Tool.CODEX
    assert len(service.list_presets()) == 1


def test_get_preset_raises_when_missing(service: SessionService) -> None:
    with pytest.raises(PresetNotFoundError, match="not-here"):
        service.get_preset("not-here")


def test_list_presets_returns_sorted_by_name(service: SessionService, tmp_path: Path) -> None:
    service.save_preset("zeta", tool=Tool.SHELL, cwd=tmp_path)
    service.save_preset("alpha", tool=Tool.SHELL, cwd=tmp_path)
    names = [preset.name for preset in service.list_presets()]
    assert names == ["alpha", "zeta"]


def test_delete_preset_removes_it(service: SessionService, tmp_path: Path) -> None:
    service.save_preset("backend-dev", tool=Tool.SHELL, cwd=tmp_path)
    service.delete_preset("backend-dev")
    assert service.list_presets() == []


def test_delete_preset_raises_when_missing(service: SessionService) -> None:
    with pytest.raises(PresetNotFoundError, match="not-here"):
        service.delete_preset("not-here")


def test_operator_profile_selection_and_approval(
    service: SessionService,
) -> None:
    service.config = service.config.model_copy(
        update={
            "operator_profiles": (
                OperatorProfileConfig(
                    name="ops",
                    default_project="platform",
                    require_approvals=True,
                    approval_code="1234",
                ),
            )
        }
    )
    selected = service.select_operator_profile("ops")
    assert selected["name"] == "ops"
    assert service.active_operator_profile().get("name") == "ops"
    assert service.evaluate_approval("delete", code="nope") is False
    assert service.evaluate_approval("delete", code="1234") is True


def test_signed_approval_token_passes_guarded_action(
    service: SessionService,
) -> None:
    service.config = service.config.model_copy(
        update={
            "approvals": ApprovalConfig(
                enabled=True,
                code="1234",
                guarded_actions=("delete",),
                token_signing_secret="secret",
            )
        }
    )
    token_payload = service.issue_approval_token(action="delete", operator="alice", ttl_seconds=600)
    token = str(token_payload["token"])
    assert service.evaluate_approval("delete", code="") is False
    assert service.evaluate_approval("delete", code="", approval_tokens=(token,)) is True


def test_dual_control_requires_distinct_token_operators(
    service: SessionService,
) -> None:
    service.config = service.config.model_copy(
        update={
            "approvals": ApprovalConfig(
                enabled=True,
                guarded_actions=("federation-action:resume:vm-a",),
                token_signing_secret="secret",
                dual_control_enabled=True,
                dual_control_actions=("federation-action:resume:vm-a",),
            )
        }
    )
    token_a = str(
        service.issue_approval_token(
            action="federation-action:resume:vm-a",
            operator="alice",
            ttl_seconds=600,
        )["token"]
    )
    token_b = str(
        service.issue_approval_token(
            action="federation-action:resume:vm-a",
            operator="bob",
            ttl_seconds=600,
        )["token"]
    )
    assert (
        service.evaluate_approval(
            "federation-action:resume:vm-a",
            code="",
            approval_tokens=(token_a,),
        )
        is False
    )
    assert (
        service.evaluate_approval(
            "federation-action:resume:vm-a",
            code="",
            approval_tokens=(token_a, token_b),
        )
        is True
    )


def test_operator_profile_allowed_actions_guardrails(
    service: SessionService, tmp_path: Path
) -> None:
    service.config = service.config.model_copy(
        update={
            "operator_profiles": (
                OperatorProfileConfig(
                    name="restricted",
                    allowed_actions=("create",),
                ),
            )
        }
    )
    service.select_operator_profile("restricted")
    created = service.create(CreateRequest(name="guarded", tool=Tool.SHELL, cwd=tmp_path))
    with pytest.raises(WsError, match="action blocked by operator profile"):
        service.update_note(created.name, "should fail")


def test_federated_action_enforces_per_host_approval_for_resume(
    service: SessionService, monkeypatch: pytest.MonkeyPatch
) -> None:
    service.config = service.config.model_copy(
        update={
            "approvals": ApprovalConfig(
                enabled=True,
                code="1234",
                guarded_actions=("federation-action:resume:vm-a",),
            )
        }
    )
    called = False

    class FakeResult:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(*_args, **_kwargs):
        nonlocal called
        called = True
        return FakeResult()

    monkeypatch.setattr("workspace_session_manager.service.subprocess.run", fake_run)

    with pytest.raises(WsError, match="approval required for remote action resume on host vm-a"):
        service.federated_action("resume", hosts=("vm-a",))
    assert called is False

    rows = service.federated_action("resume", hosts=("vm-a",), approval_code="1234")
    assert rows[0]["host"] == "vm-a"
    assert rows[0]["ok"] is True
    assert rows[0]["attempt_count"] == 1
    assert rows[0]["retried"] is False


def test_federated_action_retries_and_succeeds_after_transient_failure(
    service: SessionService, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0

    class FakeResult:
        def __init__(self, returncode: int, stderr: str = "", stdout: str = "") -> None:
            self.returncode = returncode
            self.stderr = stderr
            self.stdout = stdout

    def fake_run(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return FakeResult(returncode=1, stderr="ssh timeout")
        return FakeResult(returncode=0, stdout="ok")

    monkeypatch.setattr("workspace_session_manager.service.subprocess.run", fake_run)
    rows = service.federated_action(
        "report",
        hosts=("vm-a",),
        retry_attempts=2,
        retry_delay_seconds=0.0,
    )
    assert calls == 2
    assert rows[0]["ok"] is True
    assert rows[0]["attempt_count"] == 2
    assert rows[0]["retried"] is True
    attempts = rows[0]["attempts"]
    assert isinstance(attempts, list)
    assert "attempt 1: ssh timeout" in attempts[0]
    assert "attempt 2: ok" in attempts[1]


def test_federated_action_failure_summary_after_retry_exhaustion(
    service: SessionService, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FakeResult:
        returncode = 255
        stderr = "permission denied"
        stdout = ""

    monkeypatch.setattr(
        "workspace_session_manager.service.subprocess.run",
        lambda *_args, **_kwargs: FakeResult(),
    )
    rows = service.federated_action(
        "health",
        hosts=("vm-a",),
        retry_attempts=2,
        retry_delay_seconds=0.0,
    )
    assert rows[0]["ok"] is False
    assert rows[0]["attempt_count"] == 2
    assert rows[0]["retried"] is True
    assert rows[0]["failure_summary"] == "failed after 2 attempt(s): permission denied"


def test_federation_action_plan_reports_blast_radius_and_missing_approval(
    service: SessionService, monkeypatch: pytest.MonkeyPatch
) -> None:
    service.config = service.config.model_copy(
        update={
            "approvals": ApprovalConfig(
                enabled=True,
                guarded_actions=("federation-action:resume:vm-a",),
                code="1234",
            )
        }
    )
    monkeypatch.setattr(
        service,
        "federated_sessions",
        lambda hosts=None: [
            {
                "host": "vm-a",
                "error": "",
                "sessions": [
                    {"name": "a1", "runtime": "detached", "task_state": "blocked", "input_state": "required"}
                ],
            }
        ],
    )
    payload = service.federation_action_plan("resume", hosts=("vm-a",))
    assert payload["blast_radius"]["session_count"] == 1
    assert payload["blast_radius"]["risk_level"] in {"medium", "high"}
    assert payload["approval"]["required"] is True
    assert payload["approval"]["satisfied"] is False


def test_federation_capability_matrix_reports_command_and_tool_support(
    service: SessionService, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        service,
        "federated_sessions",
        lambda hosts=None: [{"host": "vm-a", "error": "ssh flaky", "sessions": [{"name": "one"}]}],
    )

    def fake_probe(host: str, *args: str) -> dict[str, object]:
        assert host == "vm-a"
        if args == ("--help",):
            return {
                "ok": True,
                "stdout": "Commands:\n  list\n  health\n  report\n",
                "stderr": "",
                "returncode": 0,
                "error": "",
            }
        if args == ("doctor", "--json"):
            return {
                "ok": True,
                "stdout": json.dumps(
                    {
                        "checks": [
                            {"name": "tool:claude", "status": "pass", "detail": "/bin/claude"},
                            {"name": "tool:copilot", "status": "warn", "detail": "not found"},
                        ]
                    }
                ),
                "stderr": "",
                "returncode": 0,
                "error": "",
            }
        raise AssertionError(f"unexpected probe args: {args}")

    monkeypatch.setattr(service, "_remote_ws_probe", fake_probe)
    rows = service.federation_capability_matrix(hosts=("vm-a",))
    assert len(rows) == 1
    row = rows[0]
    assert row["session_error"] == "ssh flaky"
    commands = row["commands"]
    assert isinstance(commands, dict)
    assert commands["list"]["supported"] is True
    assert commands["resume"]["supported"] is False
    assert "not listed" in str(commands["resume"]["reason"])
    tools = row["tools"]
    assert isinstance(tools, dict)
    assert tools["claude"]["supported"] is True
    assert tools["copilot"]["supported"] is False
    assert "not found" in str(tools["copilot"]["reason"])


def test_federation_dashboard_save_get_list_delete(service: SessionService) -> None:
    saved = service.save_federation_dashboard(
        name="night-ops",
        hosts=("vm-b", "vm-a"),
        host_filter="vm",
        scope_all_hosts=True,
        safe_mode=True,
    )
    assert saved["name"] == "night-ops"
    listed = service.list_federation_dashboards()
    assert listed and listed[0]["name"] == "night-ops"
    loaded = service.get_federation_dashboard("night-ops")
    assert loaded["hosts"] == ["vm-a", "vm-b"]
    assert loaded["scope_all_hosts"] is True
    assert loaded["safe_mode"] is True
    service.delete_federation_dashboard("night-ops")
    assert service.list_federation_dashboards() == []


def test_fleet_snapshot_and_live_diff_detects_operational_drift(
    service: SessionService, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = {"phase": "baseline"}

    def fake_federated_sessions(hosts=None):
        if state["phase"] == "baseline":
            return [
                {
                    "host": "vm-a",
                    "error": "",
                    "sessions": [
                        {"name": "a1", "runtime": "attached", "task_state": "in_progress", "tool": "claude"}
                    ],
                }
            ]
        return [
            {
                "host": "vm-a",
                "error": "",
                "sessions": [
                    {"name": "a1", "runtime": "detached", "task_state": "blocked", "tool": "copilot"},
                    {"name": "a2", "runtime": "stopped", "task_state": "blocked", "tool": "copilot"},
                ],
            }
        ]

    monkeypatch.setattr(service, "federated_sessions", fake_federated_sessions)
    snapshot = service.save_fleet_snapshot(name="fleet-baseline")
    assert snapshot["name"] == "fleet-baseline"
    state["phase"] = "drifted"
    payload = service.fleet_diff(left="fleet-baseline")
    drifts = payload["drifts"]
    assert any(row["field"] == "session_count" and row["host"] == "vm-a" for row in drifts)
    assert any(row["field"] == "blocked" and row["host"] == "vm-a" for row in drifts)
    assert any(row["field"] == "tool:copilot" and row["host"] == "vm-a" for row in drifts)


def test_fleet_diff_host_spread_signals_detect_cross_host_skew(
    service: SessionService, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        service,
        "federated_sessions",
        lambda hosts=None: [
            {"host": "vm-a", "error": "", "sessions": [{"name": "a1", "runtime": "attached"}]},
            {
                "host": "vm-b",
                "error": "",
                "sessions": [
                    {"name": "b1", "runtime": "detached"},
                    {"name": "b2", "runtime": "detached"},
                ],
            },
        ],
    )
    service.save_fleet_snapshot(name="fleet-a")
    payload = service.fleet_diff(left="fleet-a")
    spread = payload["host_spread"]
    assert any(item["field"] == "session_count" for item in spread)


def test_fleet_diff_anomalies_detect_high_spread_and_large_deltas(
    service: SessionService, monkeypatch: pytest.MonkeyPatch
) -> None:
    service.config = service.config.model_copy(
        update={
            "federation": service.config.federation.model_copy(
                update={"anomaly_spread_threshold": 2, "anomaly_delta_threshold": 2}
            )
        }
    )
    state = {"phase": "baseline"}

    def fake_federated_sessions(hosts=None):
        if state["phase"] == "baseline":
            return [
                {"host": "vm-a", "error": "", "sessions": [{"name": "a1", "runtime": "attached"}]},
                {"host": "vm-b", "error": "", "sessions": [{"name": "b1", "runtime": "attached"}]},
            ]
        return [
            {"host": "vm-a", "error": "", "sessions": []},
            {
                "host": "vm-b",
                "error": "",
                "sessions": [
                    {"name": "b1", "runtime": "detached"},
                    {"name": "b2", "runtime": "detached"},
                    {"name": "b3", "runtime": "detached"},
                    {"name": "b4", "runtime": "detached"},
                ],
            },
        ]

    monkeypatch.setattr(service, "federated_sessions", fake_federated_sessions)
    service.save_fleet_snapshot(name="fleet-baseline")
    state["phase"] = "drifted"
    payload = service.fleet_diff(left="fleet-baseline")
    anomalies = payload["anomalies"]
    assert anomalies
    assert any(item["type"] == "spread" for item in anomalies)


def test_incident_lifecycle_start_update_close(service: SessionService) -> None:
    opened = service.start_incident(
        title="api-timeout",
        severity="fail",
        owner="ops",
        summary="upstream timeout",
        sessions=("claude-api",),
        hosts=("vm-a",),
    )
    incident_id = str(opened["id"])
    assert opened["status"] == "open"
    updated = service.update_incident(incident_id, note="triage started", owner="oncall")
    assert updated["owner"] == "oncall"
    closed = service.close_incident(incident_id, resolution="restarted worker")
    assert closed["status"] == "closed"
    listed = service.list_incidents(status="closed")
    assert any(str(item["id"]) == incident_id for item in listed)


def test_dependency_graph_round_trip(service: SessionService) -> None:
    service.add_dependency("api-worker", "api-db")
    service.add_dependency("api-worker", "api-cache")
    service.add_dependency("api-ingest", "api-worker")
    graph = service.dependency_graph()
    assert graph["api-worker"] == ["api-cache", "api-db"]
    assert service.dependency_critical_path() == ["api-cache", "api-worker", "api-ingest"]
    service.remove_dependency("api-worker", "api-cache")
    assert service.dependency_graph()["api-worker"] == ["api-db"]


def test_unified_search_uses_rebuilt_index(
    service: SessionService, fake_backend: FakeBackend, tmp_path: Path
) -> None:
    created = service.create(
        CreateRequest(name="searchable", tool=Tool.SHELL, cwd=tmp_path, note="auth timeout")
    )
    fake_backend.previews[created.name] = "retry after timeout"
    service.rebuild_search_index()
    matches = service.unified_search("timeout")
    assert matches
    assert matches[0]["session"] == created.name


def test_unified_search_supports_facets_and_related_sessions(
    service: SessionService, fake_backend: FakeBackend, tmp_path: Path
) -> None:
    api = service.create(
        CreateRequest(
            name="api-timeout",
            tool=Tool.SHELL,
            cwd=tmp_path,
            project="api",
            note="timeout in upstream call",
            tags=["backend"],
        )
    )
    service.organize(api.name, state=TaskState.BLOCKED)
    peer = service.create(
        CreateRequest(
            name="api-helper",
            tool=Tool.SHELL,
            cwd=tmp_path,
            project="api",
            note="healthy helper",
            tags=["backend"],
        )
    )
    ui = service.create(
        CreateRequest(
            name="ui-timeout",
            tool=Tool.SHELL,
            cwd=tmp_path,
            project="ui",
            note="timeout in frontend",
            tags=["frontend"],
        )
    )
    fake_backend.previews[api.name] = "timeout from db"
    fake_backend.previews[ui.name] = "timeout from cdn"
    service.rebuild_search_index()
    matches = service.unified_search("project:api state:blocked timeout")
    assert matches
    assert matches[0]["session"] == api.name
    assert peer.name in matches[0]["related_sessions"]
    assert all(row["session"] != ui.name for row in matches)


def test_search_query_save_list_delete_roundtrip(service: SessionService) -> None:
    saved = service.save_search_query("Blocked API", "project:api state:blocked timeout")
    assert saved["name"] == "blocked-api"
    assert saved["query"] == "project:api state:blocked timeout"
    listed = service.list_search_queries()
    assert any(row["name"] == "blocked-api" for row in listed)
    assert service.get_search_query("blocked-api") == "project:api state:blocked timeout"
    service.delete_search_query("blocked-api")
    assert not any(row["name"] == "blocked-api" for row in service.list_search_queries())


def test_run_playbook_and_write_audit(
    service: SessionService, monkeypatch: pytest.MonkeyPatch
) -> None:
    service.config = service.config.model_copy(
        update={
            "playbooks": (
                PlaybookConfig(
                    name="diag",
                    commands=(("echo", "ok"),),
                ),
            )
        }
    )

    class FakeResult:
        returncode = 0
        stdout = "ok\n"
        stderr = ""

    monkeypatch.setattr(service, "runner", lambda *_args, **_kwargs: FakeResult())
    rows = service.run_playbook("diag")
    assert rows[0]["returncode"] == 0
    assert any("playbook.run" in line for line in service.read_audit(limit=20))


def test_snapshot_diff_reports_changed_keys(service: SessionService, tmp_path: Path) -> None:
    left = tmp_path / "a.json"
    right = tmp_path / "b.json"
    left.write_text(json.dumps({"a": 1, "b": 2}), encoding="utf-8")
    right.write_text(json.dumps({"a": 1, "b": 3, "c": 4}), encoding="utf-8")
    diff = service.snapshot_diff(left, right)
    assert diff["equal"] is False
    assert sorted(diff["changes"]) == ["b", "c"]


def test_builtin_playbooks_are_listed_and_support_preview(service: SessionService) -> None:
    rows = service.list_playbooks()
    names = {str(row["name"]) for row in rows}
    assert "quota-hit" in names
    preview = service.run_playbook("quota-hit", preview=True)
    assert preview
    assert preview[0]["preview"] is True
    assert preview[0]["returncode"] is None


def test_export_incident_bundle_contains_expected_artifacts(
    service: SessionService, fake_backend: FakeBackend, tmp_path: Path
) -> None:
    created = service.create(CreateRequest(name="incident-api", tool=Tool.SHELL, cwd=tmp_path))
    fake_backend.previews[created.name] = "error: timeout while contacting upstream"
    service.append_timeline(created.name, "restart", "manual recovery")
    service.append_audit("incident.note", "captured timeout regression")

    bundle = service.export_incident_bundle(
        sessions=(created.name,),
        timeline_limit=5,
        audit_limit=20,
        destination=tmp_path / "incident-bundle.tar.gz",
    )

    assert bundle.is_file()
    with tarfile.open(bundle, "r:gz") as archive:
        names = set(archive.getnames())
        assert "manifest.json" in names
        assert "health.json" in names
        assert "audit.log" in names
        assert f"sessions/{created.name}.json" in names
        session_blob = archive.extractfile(f"sessions/{created.name}.json")
        assert session_blob is not None
        payload = json.loads(session_blob.read().decode("utf-8"))
        assert payload["session"]["name"] == created.name
        assert payload["recent_output"]
        assert payload["timeline"]


def test_export_incident_bundle_includes_crosshost_federation_evidence(
    service: SessionService, fake_backend: FakeBackend, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    created = service.create(CreateRequest(name="incident-fed", tool=Tool.SHELL, cwd=tmp_path))
    fake_backend.previews[created.name] = "federated incident detail"
    monkeypatch.setattr(
        service,
        "federated_sessions",
        lambda hosts=None: [{"host": "vm-a", "error": "", "sessions": [{"name": "remote-a"}]}],
    )
    monkeypatch.setattr(
        service,
        "federated_action",
        lambda action, hosts=None, args=(): [
            {"host": "vm-a", "ok": True, "error": "", "stdout": json.dumps({"checks": []})}
        ]
        if action == "health"
        else [{"host": "vm-a", "ok": True, "error": "", "stdout": json.dumps({"sessions": 1})}],
    )

    bundle = service.export_incident_bundle(
        sessions=(created.name,),
        include_federation=True,
        federation_hosts=("vm-a",),
        destination=tmp_path / "incident-federation-bundle.tar.gz",
    )

    with tarfile.open(bundle, "r:gz") as archive:
        names = set(archive.getnames())
        assert "federation/sessions.json" in names
        assert "federation/health.json" in names
        assert "federation/report.json" in names
        manifest_blob = archive.extractfile("manifest.json")
        assert manifest_blob is not None
        manifest = json.loads(manifest_blob.read().decode("utf-8"))
        assert manifest["federation_enabled"] is True
        assert manifest["federation_hosts"] == ["vm-a"]


def test_self_heal_preview_runs_matching_policies_and_writes_audit(
    service: SessionService, monkeypatch: pytest.MonkeyPatch
) -> None:
    service.config = service.config.model_copy(
        update={
            "playbooks": (
                PlaybookConfig(name="diag", commands=(("echo", "ok"),)),
            ),
            "self_heal": SelfHealConfig(
                enabled=True,
                rules=(
                    SelfHealRuleConfig(
                        name="disk-heal",
                        when_checks=("disk-space",),
                        min_status="warn",
                        action="playbook",
                        playbook="diag",
                        session_scope="none",
                    ),
                ),
            ),
        }
    )

    class FakeResult:
        returncode = 0
        stdout = "ok\n"
        stderr = ""

    monkeypatch.setattr(service, "runner", lambda *_args, **_kwargs: FakeResult())
    monkeypatch.setattr(
        service,
        "refresh_health_alerts",
        lambda force=False, only=None: [
            HealthCheck(name="disk-space", status=HealthStatus.WARN, detail="low disk")
        ],
    )
    payload = service.self_heal(apply=False)
    assert payload["enabled"] is True
    assert payload["actions"]
    first = payload["actions"][0]
    assert first["rule"] == "disk-heal"
    assert first["mode"] == "preview"
    assert any("self-heal.preview" in line for line in service.read_audit(limit=50))


def test_remediation_chain_runs_and_supports_rollback_playbook(
    service: SessionService, monkeypatch: pytest.MonkeyPatch
) -> None:
    service.config = service.config.model_copy(
        update={
            "playbooks": (
                PlaybookConfig(name="diag", commands=(("echo", "ok"),)),
                PlaybookConfig(name="rollback", commands=(("echo", "rollback"),)),
            ),
            "remediation_chains": (
                RemediationChainConfig(
                    name="chain-a",
                    when_checks=("disk-space",),
                    steps=("playbook:diag", "recover-repair"),
                    rollback_playbook="rollback",
                ),
            ),
        }
    )

    class FakeResult:
        returncode = 0
        stdout = "ok\n"
        stderr = ""

    monkeypatch.setattr(service, "runner", lambda *_args, **_kwargs: FakeResult())
    monkeypatch.setattr(
        service,
        "refresh_health_alerts",
        lambda force=False, only=None: [
            HealthCheck(name="disk-space", status=HealthStatus.WARN, detail="low disk")
        ],
    )
    monkeypatch.setattr(service, "integrity_report", lambda: {"presets": [], "filter_presets": [], "templates": [], "timeline": []})
    payload = service.run_remediation_chain("chain-a", apply=False)
    assert payload["triggered"] is True
    assert payload["steps"]


def test_self_heal_chain_action_delegates_to_remediation_chain(
    service: SessionService, monkeypatch: pytest.MonkeyPatch
) -> None:
    service.config = service.config.model_copy(
        update={
            "self_heal": SelfHealConfig(
                enabled=True,
                rules=(
                    SelfHealRuleConfig(
                        name="run-chain",
                        when_checks=("disk-space",),
                        action="chain",
                        chain="chain-a",
                        session_scope="none",
                    ),
                ),
            ),
        }
    )
    monkeypatch.setattr(
        service,
        "refresh_health_alerts",
        lambda force=False, only=None: [
            HealthCheck(name="disk-space", status=HealthStatus.WARN, detail="warn")
        ],
    )
    monkeypatch.setattr(
        service,
        "run_remediation_chain",
        lambda name, apply=False, alerts=None: {
            "name": name,
            "applied": apply,
            "triggered": True,
            "ok": True,
            "steps": [],
            "matched_checks": ["disk-space"],
            "rollback": None,
        },
    )
    payload = service.self_heal(apply=False)
    assert payload["actions"]
    first = payload["actions"][0]
    assert first["action"] == "chain"
    assert first["ok"] is True
