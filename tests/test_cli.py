import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from conftest import FakeBackend
from workspace_session_manager import cli
from workspace_session_manager.cli import Runtime
from workspace_session_manager.config import AppConfig, HealthConfig
from workspace_session_manager.legacy import LegacyMetadataReader
from workspace_session_manager.migration import MigrationManager
from workspace_session_manager.models import (
    CreateRequest,
    DoctorReport,
    HealthCheck,
    HealthStatus,
    InputState,
    TaskState,
    Tool,
)
from workspace_session_manager.paths import AppPaths
from workspace_session_manager.service import SessionService
from workspace_session_manager.store import MetadataStore


def test_version() -> None:
    result = CliRunner().invoke(cli.app, ["--version"])
    assert result.exit_code == 0
    assert result.stdout.startswith("ws ")


def test_no_animation_option_reaches_tui(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = AppPaths(tmp_path / "config", tmp_path / "state", tmp_path / "cache")
    runtime = Runtime(paths=paths, config=AppConfig())
    received: list[bool] = []
    monkeypatch.setattr(cli, "build_runtime", lambda config=None: runtime)
    monkeypatch.setattr(
        cli,
        "run_tui",
        lambda runtime, *, no_animation=False: received.append(no_animation),
    )
    result = CliRunner().invoke(cli.app, ["--no-animation"])
    assert result.exit_code == 0, result.output
    assert received == [True]


def test_classic_launcher_requires_owner_only_executable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    classic = tmp_path / ".local" / "libexec" / "wf-classic"
    classic.parent.mkdir(parents=True)
    classic.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    classic.chmod(0o700)
    executed: list[tuple[Path, list[str]]] = []
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(
        cli.os,
        "execv",
        lambda path, args: executed.append((Path(path), args)),
    )
    cli.run_classic()
    assert executed == [(classic, [str(classic)])]

    classic.chmod(0o755)
    with pytest.raises(cli.WsError, match="unsafe classic"):
        cli.run_classic()


def test_json_list(
    service: SessionService,
    fake_backend: FakeBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_backend.add("shell-one")
    monkeypatch.setattr(Runtime, "service", lambda self: service)
    result = CliRunner().invoke(cli.app, ["list", "--all", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload[0]["name"] == "shell-one"
    assert payload[0]["owned"] is False


def test_dry_run_does_not_create(
    service: SessionService,
    fake_backend: FakeBackend,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(Runtime, "service", lambda self: service)
    result = CliRunner().invoke(
        cli.app,
        [
            "create",
            "--tool",
            "shell",
            "--name",
            "preview",
            "--cwd",
            str(tmp_path),
            "--dry-run",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Would create: preview" in result.stdout
    assert fake_backend.sessions == {}


def test_preset_save_list_and_delete_round_trip(
    service: SessionService,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(Runtime, "service", lambda self: service)
    runner = CliRunner()

    save_result = runner.invoke(
        cli.app,
        [
            "preset",
            "save",
            "backend-dev",
            "--tool",
            "shell",
            "--cwd",
            str(tmp_path),
            "--tag",
            "backend",
        ],
    )
    assert save_result.exit_code == 0, save_result.output
    assert "Saved preset: backend-dev" in save_result.stdout

    list_result = runner.invoke(cli.app, ["preset", "list", "--json"])
    assert list_result.exit_code == 0, list_result.output
    payload = json.loads(list_result.stdout)
    assert payload[0]["name"] == "backend-dev"
    assert payload[0]["tags"] == ["backend"]

    delete_result = runner.invoke(cli.app, ["preset", "delete", "backend-dev"])
    assert delete_result.exit_code == 0, delete_result.output
    assert "Deleted preset: backend-dev" in delete_result.stdout
    assert service.list_presets() == []


def test_preset_save_resolves_relative_cwd_at_save_time(
    service: SessionService,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A relative --cwd must be anchored to where it was saved, not
    re-resolved against the directory `create --from-preset` is later run
    from -- otherwise the same preset silently creates sessions in different
    places depending on the caller's cwd."""
    nested = tmp_path / "project"
    nested.mkdir()
    monkeypatch.setattr(Runtime, "service", lambda self: service)
    monkeypatch.chdir(nested)
    runner = CliRunner()

    save_result = runner.invoke(
        cli.app,
        ["preset", "save", "here", "--tool", "shell", "--cwd", "."],
    )
    assert save_result.exit_code == 0, save_result.output
    assert service.get_preset("here").cwd == nested.resolve()


def test_preset_delete_missing_preset_errors(
    service: SessionService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(Runtime, "service", lambda self: service)
    result = CliRunner().invoke(cli.app, ["preset", "delete", "does-not-exist"])
    assert result.exit_code == 1
    assert "preset not found" in result.output


def test_create_from_preset_applies_preset_values(
    service: SessionService,
    fake_backend: FakeBackend,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(Runtime, "service", lambda self: service)
    service.save_preset(
        "backend-dev", tool=Tool.SHELL, cwd=tmp_path, project="api", tags=["backend"]
    )
    result = CliRunner().invoke(
        cli.app,
        ["create", "--name", "from-preset-test", "--from-preset", "backend-dev"],
    )
    assert result.exit_code == 0, result.output
    session = service.get("from-preset-test")
    assert session.tool is Tool.SHELL
    assert session.cwd == tmp_path
    assert session.project == "api"
    assert session.tags == ["backend"]


def test_create_from_preset_explicit_flags_override_preset(
    service: SessionService,
    fake_backend: FakeBackend,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(Runtime, "service", lambda self: service)
    service.save_preset("backend-dev", tool=Tool.SHELL, cwd=tmp_path, tags=["backend"])
    result = CliRunner().invoke(
        cli.app,
        [
            "create",
            "--name",
            "override-test",
            "--from-preset",
            "backend-dev",
            "--tool",
            "codex",
            "--tag",
            "frontend",
        ],
    )
    assert result.exit_code == 0, result.output
    session = service.get("codex-override-test")
    assert session.tool is Tool.CODEX
    assert session.tags == ["frontend"]


def test_create_without_tool_or_preset_errors_clearly(
    service: SessionService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(Runtime, "service", lambda self: service)
    result = CliRunner().invoke(cli.app, ["create", "--name", "no-tool"])
    assert result.exit_code == 1
    assert "--tool is required" in result.output


def test_create_from_missing_preset_errors_clearly(
    service: SessionService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(Runtime, "service", lambda self: service)
    result = CliRunner().invoke(
        cli.app, ["create", "--name", "x", "--from-preset", "does-not-exist"]
    )
    assert result.exit_code == 1
    assert "preset not found" in result.output


def test_create_from_session_applies_source_session_values(
    service: SessionService,
    fake_backend: FakeBackend,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(Runtime, "service", lambda self: service)
    source = service.create(
        CreateRequest(
            name="original",
            tool=Tool.SHELL,
            cwd=tmp_path,
            project="api",
            tags=["backend"],
            logging_enabled=False,
        )
    )
    result = CliRunner().invoke(
        cli.app,
        ["create", "--name", "cloned", "--from-session", source.name],
    )
    assert result.exit_code == 0, result.output
    cloned = service.get("cloned")
    assert cloned.tool is Tool.SHELL
    assert cloned.cwd == tmp_path
    assert cloned.project == "api"
    assert cloned.tags == ["backend"]
    assert cloned.logging_enabled is False


def test_create_from_session_explicit_flags_override_source(
    service: SessionService,
    fake_backend: FakeBackend,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(Runtime, "service", lambda self: service)
    source = service.create(
        CreateRequest(name="original", tool=Tool.SHELL, cwd=tmp_path, tags=["backend"])
    )
    result = CliRunner().invoke(
        cli.app,
        [
            "create",
            "--name",
            "override-test",
            "--from-session",
            source.name,
            "--tool",
            "codex",
            "--tag",
            "frontend",
        ],
    )
    assert result.exit_code == 0, result.output
    cloned = service.get("codex-override-test")
    assert cloned.tool is Tool.CODEX
    assert cloned.tags == ["frontend"]


def test_create_from_missing_session_errors_clearly(
    service: SessionService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(Runtime, "service", lambda self: service)
    result = CliRunner().invoke(
        cli.app, ["create", "--name", "x", "--from-session", "does-not-exist"]
    )
    assert result.exit_code == 1
    assert "session not found" in result.output


def test_create_combining_from_preset_and_from_session_errors_clearly(
    service: SessionService,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(Runtime, "service", lambda self: service)
    service.save_preset("backend-dev", tool=Tool.SHELL, cwd=tmp_path)
    source = service.create(CreateRequest(name="original", tool=Tool.SHELL, cwd=tmp_path))
    result = CliRunner().invoke(
        cli.app,
        [
            "create",
            "--name",
            "x",
            "--from-preset",
            "backend-dev",
            "--from-session",
            source.name,
        ],
    )
    assert result.exit_code == 1
    assert "cannot be combined" in result.output


def test_default_list_hides_unmanaged_session(
    service: SessionService,
    fake_backend: FakeBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_backend.add("shell-hidden")
    monkeypatch.setattr(Runtime, "service", lambda self: service)
    result = CliRunner().invoke(cli.app, ["list", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == []


def test_list_filters_by_tag_and_project(
    service: SessionService,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    backend_session = service.create(
        CreateRequest(name="backend-work", tool=Tool.SHELL, cwd=tmp_path)
    )
    frontend_session = service.create(
        CreateRequest(name="frontend-work", tool=Tool.SHELL, cwd=tmp_path)
    )
    service.organize(backend_session.name, tags=["backend"], project="api")
    service.organize(frontend_session.name, tags=["frontend"], project="web")
    monkeypatch.setattr(Runtime, "service", lambda self: service)

    tag_result = CliRunner().invoke(cli.app, ["list", "--json", "--tag", "backend"])
    assert tag_result.exit_code == 0, tag_result.output
    tag_payload = json.loads(tag_result.stdout)
    assert [item["name"] for item in tag_payload] == [backend_session.name]

    project_result = CliRunner().invoke(cli.app, ["list", "--json", "--project", "web"])
    assert project_result.exit_code == 0, project_result.output
    project_payload = json.loads(project_result.stdout)
    assert [item["name"] for item in project_payload] == [frontend_session.name]

    empty_result = CliRunner().invoke(cli.app, ["list", "--json", "--tag", "no-such-tag"])
    assert empty_result.exit_code == 0, empty_result.output
    assert json.loads(empty_result.stdout) == []


def test_explicit_edit_command_updates_task_input_and_project(
    service: SessionService,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    session = service.create(CreateRequest(name="edit", tool=Tool.SHELL, cwd=tmp_path))
    monkeypatch.setattr(Runtime, "service", lambda self: service)
    result = CliRunner().invoke(
        cli.app,
        [
            "edit",
            session.name,
            "--state",
            "waiting",
            "--input",
            "required",
            "--project",
            "workflow-core",
        ],
    )
    assert result.exit_code == 0, result.output
    updated = service.get(session.name)
    assert updated.task_state is TaskState.WAITING
    assert updated.input_state is InputState.REQUIRED
    assert updated.project == "workflow-core"


def test_legacy_organize_alias_is_hidden_from_help() -> None:
    commands = {command.name: command for command in cli.app.registered_commands if command.name}
    assert not commands["edit"].hidden
    assert commands["organize"].hidden


def test_migration_cli_preview_apply_status_and_rollback(
    tmp_path: Path,
    fake_backend: FakeBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    name = "claude-import"
    (legacy / f"{name}.tool").write_text("claude\n", encoding="utf-8")
    (legacy / f"{name}.cwd").write_text(f"{tmp_path}\n", encoding="utf-8")
    (legacy / f"{name}.note").write_text("private migration note\n", encoding="utf-8")
    fake_backend.add(name, session_id="$cli-import")
    paths = AppPaths(tmp_path / "config", tmp_path / "state", tmp_path / "cache")
    manager = MigrationManager(
        backend=fake_backend,
        store=MetadataStore(paths),
        legacy=LegacyMetadataReader((legacy,)),
        paths=paths,
    )
    monkeypatch.setattr(Runtime, "migration", lambda self: manager)
    plan_path = tmp_path / "plan.json"
    runner = CliRunner()

    preview = runner.invoke(
        cli.app,
        ["migrate", "preview", "--all", "--output", str(plan_path)],
    )
    assert preview.exit_code == 0, preview.output
    assert "Notes are included" in preview.stdout
    assert plan_path.is_file()

    validation = runner.invoke(cli.app, ["migrate", "validate", str(plan_path), "--json"])
    assert validation.exit_code == 0, validation.output
    validation_payload = json.loads(validation.stdout)
    assert validation_payload["valid"] is True
    assert validation_payload["sessions"][0]["tmux_session_id"] == "$cli-import"
    assert "private migration note" not in validation.stdout

    gate = runner.invoke(cli.app, ["migrate", "apply", str(plan_path)])
    assert gate.exit_code == 2
    apply = runner.invoke(cli.app, ["migrate", "apply", str(plan_path), "--approve"])
    assert apply.exit_code == 0, apply.output
    migration_id = manager.status()[0].migration_id

    status = runner.invoke(cli.app, ["migrate", "status", "--json"])
    assert status.exit_code == 0, status.output
    assert json.loads(status.stdout)[0]["status"] == "applied"

    rollback = runner.invoke(
        cli.app,
        ["migrate", "rollback", str(migration_id), "--approve"],
    )
    assert rollback.exit_code == 0, rollback.output
    assert fake_backend.session_exists(name)
    assert fake_backend.get_option(name, "@wf_owner") is None


def test_migration_preview_requires_explicit_selection() -> None:
    result = CliRunner().invoke(cli.app, ["migrate", "preview"])
    assert result.exit_code == 2
    assert "choose --all or at least one --session" in result.output


def test_inspect_survives_malicious_markup_in_note_without_crashing(
    service: SessionService,
    fake_backend: FakeBackend,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A stray closing tag like "[/bold]" in a note -- plausible in real
    text, no attacker required -- must not raise rich.errors.MarkupError
    and crash `ws inspect`."""
    monkeypatch.setattr(Runtime, "service", lambda self: service)
    session = service.create(CreateRequest(name="claude-markup", tool=Tool.CLAUDE, cwd=tmp_path))
    service.update_note(session.name, "stray tag [/bold] and [bold]styled[/bold] text")
    result = CliRunner().invoke(cli.app, ["inspect", session.name])
    assert result.exit_code == 0, result.output
    assert "stray tag [/bold] and [bold]styled[/bold] text" in result.output


def test_preset_list_rejects_malicious_markup_in_project_without_crashing(
    service: SessionService,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(Runtime, "service", lambda self: service)
    service.save_preset("backend-dev", tool=Tool.SHELL, cwd=tmp_path, project="[/bold] api")
    result = CliRunner().invoke(cli.app, ["preset", "list"])
    assert result.exit_code == 0, result.output
    assert "[/bold] api" in result.output


def test_preset_list_reports_corrupt_presets_file_cleanly(
    service: SessionService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(Runtime, "service", lambda self: service)
    service.paths.presets_file.parent.mkdir(parents=True, exist_ok=True)
    target = service.paths.presets_file.parent / "outside.json"
    target.write_text("{}", encoding="utf-8")
    service.paths.presets_file.symlink_to(target)
    result = CliRunner().invoke(cli.app, ["preset", "list"])
    assert result.exit_code == 1
    assert "Traceback" not in result.output


def test_redact_report_strips_ipv4_from_check_detail() -> None:
    report = DoctorReport(
        checks=[HealthCheck(name="probe", status=HealthStatus.WARN, detail="peer 10.0.0.5 seen")]
    )
    redacted = cli._redact_report(report)
    assert "10.0.0.5" not in redacted.checks[0].detail
    assert "[REDACTED_IP]" in redacted.checks[0].detail


def test_doctor_command_redacts_check_detail(
    service: SessionService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(Runtime, "service", lambda self: service)
    monkeypatch.setattr(
        service,
        "doctor",
        lambda: DoctorReport(
            checks=[HealthCheck(name="probe", status=HealthStatus.PASS, detail="peer 10.0.0.5")]
        ),
    )
    result = CliRunner().invoke(cli.app, ["doctor"])
    assert result.exit_code == 0, result.output
    assert "10.0.0.5" not in result.stdout
    assert "[REDACTED_IP]" in result.stdout


def test_health_command_reports_configured_checks(
    service: SessionService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service.config = service.config.model_copy(
        update={
            "health": HealthConfig(
                enabled=True,
                apt_updates_enabled=False,
                reboot_required_enabled=False,
                git_dirty_enabled=False,
                docker_enabled=False,
                disk_warn_percent=60,
                disk_fail_percent=40,
            )
        }
    )

    class FakeUsage:
        total = 100
        free = 50

    monkeypatch.setattr("shutil.disk_usage", lambda _root: FakeUsage())
    monkeypatch.setattr(Runtime, "service", lambda self: service)
    result = CliRunner().invoke(cli.app, ["health"])
    assert result.exit_code == 0, result.output
    assert "disk-space" in result.stdout
    assert "warn" in result.stdout


def test_health_command_fix_cleans_up_zombie_sessions(
    service: SessionService,
    fake_backend: FakeBackend,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    service.config = service.config.model_copy(
        update={
            "health": HealthConfig(
                enabled=True,
                apt_updates_enabled=False,
                reboot_required_enabled=False,
                git_dirty_enabled=False,
                docker_enabled=False,
                disk_space_enabled=False,
                idle_sessions_enabled=False,
                orphaned_logs_enabled=False,
                zombie_stale_after_days=1,
            )
        }
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

    monkeypatch.setattr(Runtime, "service", lambda self: service)
    result = CliRunner().invoke(cli.app, ["health", "--fix", "zombie-sessions"])
    assert result.exit_code == 0, result.output
    assert service.store.load(created.name) is None


def test_health_command_fix_unknown_check_errors_clearly(
    service: SessionService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(Runtime, "service", lambda self: service)
    result = CliRunner().invoke(cli.app, ["health", "--fix", "does-not-exist"])
    assert result.exit_code == 1
    assert "unknown or disabled" in result.output


def test_health_command_fix_non_fixable_check_errors_clearly(
    service: SessionService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service.config = service.config.model_copy(
        update={
            "health": HealthConfig(
                enabled=True,
                apt_updates_enabled=False,
                reboot_required_enabled=False,
                git_dirty_enabled=False,
                docker_enabled=False,
                disk_space_enabled=False,
                zombie_sessions_enabled=False,
                orphaned_logs_enabled=False,
            )
        }
    )
    monkeypatch.setattr(Runtime, "service", lambda self: service)
    result = CliRunner().invoke(cli.app, ["health", "--fix", "idle-sessions"])
    assert result.exit_code == 1
    assert "no automatic fix" in result.output


def test_health_command_json(
    service: SessionService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service.config = service.config.model_copy(
        update={
            "health": HealthConfig(
                enabled=True,
                apt_updates_enabled=False,
                reboot_required_enabled=False,
                git_dirty_enabled=False,
                docker_enabled=False,
            )
        }
    )
    monkeypatch.setattr(Runtime, "service", lambda self: service)
    result = CliRunner().invoke(cli.app, ["health", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert any(check["name"] == "disk-space" for check in payload["checks"])
