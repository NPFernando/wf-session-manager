import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from conftest import FakeBackend
from workspace_session_manager import cli
from workspace_session_manager.cli import Runtime
from workspace_session_manager.config import (
    AppConfig,
    HealthConfig,
    OperatorProfileConfig,
    PlaybookConfig,
    SelfHealConfig,
    SelfHealRuleConfig,
    load_config,
)
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


def test_setup_yes_writes_detected_tool_profiles(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WS_DEV_ROOT", str(tmp_path))

    detected = {
        "claude": "/usr/bin/claude",
        "copilot": "/usr/bin/copilot",
        "codex": None,
        "hermes": "/usr/bin/hermes",
    }
    monkeypatch.setattr(cli.shutil, "which", lambda name: detected.get(name))

    result = CliRunner().invoke(cli.app, ["setup", "--yes"])
    assert result.exit_code == 0, result.output

    paths = AppPaths.discover()
    config = load_config(paths)
    assert config.tools[Tool.CLAUDE].enabled is True
    assert config.tools[Tool.CLAUDE].command == ("/usr/bin/claude",)
    assert config.tools[Tool.COPILOT].enabled is True
    assert config.tools[Tool.COPILOT].command == ("/usr/bin/copilot",)
    assert config.tools[Tool.CODEX].enabled is False
    assert config.tools[Tool.HERMES].enabled is True
    assert config.tools[Tool.HERMES].command == ("/usr/bin/hermes", "chat")
    assert config.tools[Tool.SHELL].enabled is True
    assert paths.onboarding_file.is_file()


def test_setup_refuses_to_overwrite_existing_config_without_force(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WS_DEV_ROOT", str(tmp_path))
    paths = AppPaths.discover()
    paths.config_dir.mkdir(parents=True)
    paths.config_file.write_text(
        '[tools.claude]\ncommand = ["claude"]\nenabled = true\n',
        encoding="utf-8",
    )

    result = CliRunner().invoke(cli.app, ["setup", "--yes"])
    assert result.exit_code == 1
    assert "configuration already exists" in result.output


def test_quickstart_bootstraps_config_and_attaches(
    service: SessionService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = Runtime(paths=service.paths, config=AppConfig())
    monkeypatch.setattr(cli, "build_runtime", lambda config=None: runtime)
    monkeypatch.setattr(Runtime, "service", lambda self: service)
    detected = {
        "claude": None,
        "copilot": "/usr/bin/copilot",
        "codex": None,
        "hermes": None,
    }
    monkeypatch.setattr(cli.shutil, "which", lambda name: detected.get(name))

    result = CliRunner().invoke(cli.app, ["quickstart", "--name", "hello", "--cwd", "/tmp"])
    assert result.exit_code == 0, result.output
    created_name = next(iter(service.backend.sessions))
    created = service.get(created_name)
    assert created.tool is Tool.COPILOT
    assert created.name.endswith("hello")
    assert service.paths.config_file.exists()
    assert service.paths.onboarding_file.exists()
    assert created.name in service.backend.attached


def test_quickstart_no_attach_respected(
    service: SessionService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = Runtime(paths=service.paths, config=AppConfig())
    monkeypatch.setattr(cli, "build_runtime", lambda config=None: runtime)
    monkeypatch.setattr(Runtime, "service", lambda self: service)

    result = CliRunner().invoke(
        cli.app,
        ["quickstart", "--name", "shell-start", "--tool", "shell", "--cwd", "/tmp", "--no-attach"],
    )
    assert result.exit_code == 0, result.output
    created = service.get("shell-start")
    assert created.tool is Tool.SHELL
    assert service.backend.attached == []


def test_quickstart_can_create_from_preset(
    service: SessionService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = Runtime(paths=service.paths, config=AppConfig())
    monkeypatch.setattr(cli, "build_runtime", lambda config=None: runtime)
    monkeypatch.setattr(Runtime, "service", lambda self: service)
    service.save_preset(
        "backend-dev",
        tool=Tool.SHELL,
        cwd=Path("/tmp"),
        project="api",
        tags=["backend"],
        logging_enabled=False,
    )
    result = CliRunner().invoke(
        cli.app,
        ["quickstart", "--name", "preset-start", "--preset", "backend-dev", "--no-attach"],
    )
    assert result.exit_code == 0, result.output
    created = service.get("preset-start")
    assert created.tool is Tool.SHELL
    assert created.project == "api"
    assert created.tags == ["backend"]
    assert created.logging_enabled is False


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


def test_onboarding_reset_removes_marker(
    service: SessionService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(Runtime, "service", lambda self: service)
    service.mark_onboarding_seen()
    assert service.onboarding_seen()
    result = CliRunner().invoke(cli.app, ["onboarding", "reset"])
    assert result.exit_code == 0, result.output
    assert not service.onboarding_seen()


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


def test_preset_validate_reports_readiness_and_failures(
    service: SessionService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(Runtime, "service", lambda self: service)
    service.save_preset("ready", tool=Tool.SHELL, cwd=Path("/tmp"))
    service.save_preset("missing-cwd", tool=Tool.SHELL, cwd=Path("/tmp/does-not-exist-xyz"))
    service.save_preset("missing-cmd", tool=Tool.CLAUDE, cwd=Path("/tmp"))
    service.config = service.config.model_copy(
        update={
            "tools": {
                **service.config.tools,
                Tool.CLAUDE: service.config.tools[Tool.CLAUDE].model_copy(
                    update={"command": ("/definitely/missing",)}
                ),
            }
        }
    )
    result = CliRunner().invoke(cli.app, ["preset", "validate", "--json"])
    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    by_name = {item["name"]: item for item in payload}
    assert by_name["ready"]["status"] == "pass"
    assert by_name["missing-cwd"]["status"] == "warn"
    assert by_name["missing-cmd"]["status"] == "fail"


def test_preset_validate_actionable_filters_pass(
    service: SessionService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(Runtime, "service", lambda self: service)
    service.save_preset("ready", tool=Tool.SHELL, cwd=Path("/tmp"))
    result = CliRunner().invoke(cli.app, ["preset", "validate", "--actionable", "--json"])
    assert result.exit_code == 0
    assert json.loads(result.stdout) == []


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


def test_create_without_tool_or_preset_uses_default_enabled_tool(
    service: SessionService,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(Runtime, "service", lambda self: service)
    result = CliRunner().invoke(
        cli.app,
        ["create", "--name", "no-tool", "--cwd", str(tmp_path)],
    )
    assert result.exit_code == 0, result.output
    created = service.get("claude-no-tool")
    assert created.tool is Tool.CLAUDE


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


def test_create_without_tool_errors_when_all_profiles_disabled(
    service: SessionService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    disabled_tools = {
        tool: profile.model_copy(update={"enabled": False})
        for tool, profile in service.config.tools.items()
    }
    service.config = service.config.model_copy(update={"tools": disabled_tools})
    monkeypatch.setattr(Runtime, "service", lambda self: service)
    result = CliRunner().invoke(cli.app, ["create", "--name", "no-tool"])
    assert result.exit_code == 1
    assert "no enabled tool profiles are configured" in result.output


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
        checks=[
            HealthCheck(
                name="probe",
                status=HealthStatus.WARN,
                detail="peer 10.0.0.5 seen",
                corrective_action="run check against 10.0.0.5",
            )
        ]
    )
    redacted = cli._redact_report(report)
    assert "10.0.0.5" not in redacted.checks[0].detail
    assert "[REDACTED_IP]" in redacted.checks[0].detail
    assert "10.0.0.5" not in redacted.checks[0].corrective_action
    assert "[REDACTED_IP]" in redacted.checks[0].corrective_action


def test_doctor_command_redacts_check_detail(
    service: SessionService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(Runtime, "service", lambda self: service)
    monkeypatch.setattr(
        service,
        "doctor",
        lambda: DoctorReport(
            checks=[
                HealthCheck(
                    name="probe",
                    status=HealthStatus.PASS,
                    detail="peer 10.0.0.5",
                    corrective_action="ping 10.0.0.5",
                )
            ]
        ),
    )
    result = CliRunner().invoke(cli.app, ["doctor"])
    assert result.exit_code == 0, result.output
    assert "10.0.0.5" not in result.stdout
    assert "[REDACTED_IP]" in result.stdout
    assert "Fix" in result.stdout
    assert "ping [REDACTED_IP]" in result.stdout


def test_doctor_actionable_filters_pass_checks(
    service: SessionService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(Runtime, "service", lambda self: service)
    monkeypatch.setattr(
        service,
        "doctor",
        lambda: DoctorReport(
            checks=[
                HealthCheck(name="ok", status=HealthStatus.PASS, detail="healthy"),
                HealthCheck(
                    name="warn",
                    status=HealthStatus.WARN,
                    detail="tool missing",
                    corrective_action="install tool",
                ),
            ]
        ),
    )
    result = CliRunner().invoke(cli.app, ["doctor", "--actionable", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert len(payload["checks"]) == 1
    assert payload["checks"][0]["name"] == "warn"


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


def test_ux_audit_command_passes_with_json_output() -> None:
    result = CliRunner().invoke(cli.app, ["ux-audit", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["checks"]
    assert all(item["status"] == "pass" for item in payload["checks"])


def test_ux_a11y_audit_command_passes_with_json_output() -> None:
    result = CliRunner().invoke(cli.app, ["ux-a11y-audit", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["checks"]
    assert all(item["status"] == "pass" for item in payload["checks"])


def test_filter_preset_save_supports_grouping_and_density(
    service: SessionService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(Runtime, "service", lambda self: service)
    result = CliRunner().invoke(
        cli.app,
        [
            "filter-preset",
            "save",
            "ops-view",
            "--quick-filter",
            "warnings",
            "--grouping",
            "warning",
            "--density",
            "compact",
        ],
    )
    assert result.exit_code == 0, result.output
    saved = service.get_filter_preset("ops-view")
    assert saved.grouping == "warning"
    assert saved.density == "compact"


def test_report_output_writes_plaintext_snapshot(
    service: SessionService,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(Runtime, "service", lambda self: service)
    destination = tmp_path / "ops.txt"
    result = CliRunner().invoke(cli.app, ["report", "--output", str(destination)])
    assert result.exit_code == 0, result.output
    assert destination.is_file()
    text = destination.read_text(encoding="utf-8")
    assert "WORKSPACE OPERATIONS REPORT" in text


def test_template_save_and_create_from_template(
    service: SessionService,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(Runtime, "service", lambda self: service)
    save = CliRunner().invoke(
        cli.app,
        [
            "template",
            "save",
            "feature",
            "--tool",
            "shell",
            "--name-template",
            "{project}-{ticket}",
            "--cwd-template",
            str(tmp_path / "{project}"),
            "--project-template",
            "{project}",
        ],
    )
    assert save.exit_code == 0, save.output
    (tmp_path / "api").mkdir(parents=True, exist_ok=True)
    create = CliRunner().invoke(
        cli.app,
        [
            "create",
            "--from-template",
            "feature",
            "--var",
            "project=api",
            "--var",
            "ticket=123",
        ],
    )
    assert create.exit_code == 0, create.output
    created = service.get("api-123")
    assert created.project == "api"


def test_undo_restores_removed_metadata(
    service: SessionService,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(Runtime, "service", lambda self: service)
    session = service.create(CreateRequest(name="undo-meta", tool=Tool.SHELL, cwd=tmp_path))
    service.remove_metadata(session.name)
    assert service.store.load(session.name) is None
    undo = CliRunner().invoke(cli.app, ["undo"])
    assert undo.exit_code == 0, undo.output
    assert service.store.load(session.name) is not None


def test_backup_command_creates_archive(
    service: SessionService,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(Runtime, "service", lambda self: service)
    destination = tmp_path / "backup.tar.gz"
    result = CliRunner().invoke(cli.app, ["backup", "--output", str(destination)])
    assert result.exit_code == 0, result.output
    assert destination.is_file()


def test_board_command_outputs_lanes(
    service: SessionService,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(Runtime, "service", lambda self: service)
    service.create(CreateRequest(name="board-one", tool=Tool.SHELL, cwd=tmp_path))
    result = CliRunner().invoke(cli.app, ["board"])
    assert result.exit_code == 0, result.output
    assert "todo" in result.output


def test_suggest_command_works_for_known_pattern(
    service: SessionService,
    fake_backend: FakeBackend,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(Runtime, "service", lambda self: service)
    created = service.create(CreateRequest(name="limited", tool=Tool.CLAUDE, cwd=tmp_path))
    fake_backend.previews[created.name] = "You've hit your session limit"
    result = CliRunner().invoke(cli.app, ["suggest", created.name])
    assert result.exit_code == 0, result.output
    assert "quota" in result.output.lower() or "resume" in result.output.lower()


def test_recover_command_runs(
    service: SessionService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(Runtime, "service", lambda self: service)
    result = CliRunner().invoke(cli.app, ["recover"])
    assert result.exit_code == 0, result.output
    assert "sessions" in result.output


def test_profile_select_via_root_option(
    service: SessionService, monkeypatch: pytest.MonkeyPatch
) -> None:
    service.config = service.config.model_copy(
        update={
            "operator_profiles": (OperatorProfileConfig(name="ops", default_project="platform"),)
        }
    )
    monkeypatch.setattr(Runtime, "service", lambda self: service)
    result = CliRunner().invoke(cli.app, ["--profile", "ops", "profile", "active", "--json"])
    assert result.exit_code == 0, result.output
    assert service.active_operator_profile().get("name") == "ops"


def test_delete_command_honors_operator_profile_allowed_actions(
    service: SessionService,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
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
    created = service.create(CreateRequest(name="locked-delete", tool=Tool.SHELL, cwd=tmp_path))
    service.select_operator_profile("restricted")
    monkeypatch.setattr(Runtime, "service", lambda self: service)
    result = CliRunner().invoke(cli.app, ["delete", created.name, "--yes"])
    assert result.exit_code == 1
    assert "action blocked by operator profile" in result.output


def test_dependency_commands_round_trip(
    service: SessionService, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Runtime, "service", lambda self: service)
    add = CliRunner().invoke(cli.app, ["dependency", "add", "api-worker", "api-db"])
    assert add.exit_code == 0, add.output
    listing = CliRunner().invoke(cli.app, ["dependency", "list", "--json"])
    assert listing.exit_code == 0, listing.output
    payload = json.loads(listing.stdout)
    assert payload["api-worker"] == ["api-db"]


def test_policy_simulate_and_search_commands(
    service: SessionService,
    fake_backend: FakeBackend,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(Runtime, "service", lambda self: service)
    created = service.create(
        CreateRequest(name="queryable", tool=Tool.SHELL, cwd=tmp_path, note="auth timeout")
    )
    fake_backend.previews[created.name] = "auth timeout"
    simulate = CliRunner().invoke(cli.app, ["policy-simulate", "--json"])
    assert simulate.exit_code == 0, simulate.output
    assert "sessions_total" in json.loads(simulate.stdout)
    search = CliRunner().invoke(cli.app, ["search", "timeout", "--json"])
    assert search.exit_code == 0, search.output
    assert json.loads(search.stdout)


def test_search_v2_facets_and_saved_queries(
    service: SessionService,
    fake_backend: FakeBackend,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(Runtime, "service", lambda self: service)
    blocked = service.create(
        CreateRequest(
            name="api-blocked",
            tool=Tool.SHELL,
            cwd=tmp_path,
            project="api",
            note="timeout in worker",
            tags=["backend"],
        )
    )
    service.organize(blocked.name, state=TaskState.BLOCKED)
    fake_backend.previews[blocked.name] = "timeout raised"
    service.rebuild_search_index()

    save = CliRunner().invoke(
        cli.app,
        ["search-save", "blocked-api", "project:api state:blocked timeout"],
    )
    assert save.exit_code == 0, save.output

    listed = CliRunner().invoke(cli.app, ["search-saved", "--json"])
    assert listed.exit_code == 0, listed.output
    assert any(row["name"] == "blocked-api" for row in json.loads(listed.stdout))

    search = CliRunner().invoke(cli.app, ["search", "--saved", "blocked-api", "--json"])
    assert search.exit_code == 0, search.output
    payload = json.loads(search.stdout)
    assert payload
    assert payload[0]["session"] == blocked.name

    deleted = CliRunner().invoke(cli.app, ["search-delete", "blocked-api"])
    assert deleted.exit_code == 0, deleted.output


def test_playbook_snapshot_diff_and_audit_commands(
    service: SessionService,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    service.config = service.config.model_copy(
        update={"playbooks": (PlaybookConfig(name="diag", commands=(("echo", "ok"),)),)}
    )

    class FakeResult:
        returncode = 0
        stdout = "ok\n"
        stderr = ""

    monkeypatch.setattr(service, "runner", lambda *_args, **_kwargs: FakeResult())
    monkeypatch.setattr(Runtime, "service", lambda self: service)
    playbook = CliRunner().invoke(cli.app, ["playbook", "diag", "--yes", "--json"])
    assert playbook.exit_code == 0, playbook.output
    left = tmp_path / "left.json"
    right = tmp_path / "right.json"
    left.write_text('{"a":1}', encoding="utf-8")
    right.write_text('{"a":2}', encoding="utf-8")
    diff = CliRunner().invoke(cli.app, ["snapshot-diff", str(left), str(right), "--json"])
    assert diff.exit_code == 0, diff.output
    assert json.loads(diff.stdout)["equal"] is False
    audit = CliRunner().invoke(cli.app, ["audit", "--json"])
    assert audit.exit_code == 0, audit.output
    assert any("playbook.run" in line for line in json.loads(audit.stdout))


def test_playbook_list_includes_builtin_and_preview_runs(
    service: SessionService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(Runtime, "service", lambda self: service)
    listed = CliRunner().invoke(cli.app, ["playbook-list", "--json"])
    assert listed.exit_code == 0, listed.output
    rows = json.loads(listed.stdout)
    assert any(row["name"] == "quota-hit" for row in rows)
    preview = CliRunner().invoke(cli.app, ["playbook", "quota-hit", "--preview", "--json"])
    assert preview.exit_code == 0, preview.output
    payload = json.loads(preview.stdout)
    assert payload[0]["preview"] is True


def test_incident_bundle_command_exports_archive(
    service: SessionService,
    fake_backend: FakeBackend,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(Runtime, "service", lambda self: service)
    created = service.create(CreateRequest(name="incident-cli", tool=Tool.SHELL, cwd=tmp_path))
    fake_backend.previews[created.name] = "incident reproduced"
    destination = tmp_path / "incident-cli-bundle.tar.gz"

    result = CliRunner().invoke(
        cli.app,
        [
            "incident-bundle",
            "--session",
            created.name,
            "--output",
            str(destination),
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["path"] == str(destination)
    assert destination.is_file()


def test_incident_bundle_command_includes_federation_evidence(
    service: SessionService,
    fake_backend: FakeBackend,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(Runtime, "service", lambda self: service)
    created = service.create(CreateRequest(name="incident-fed-cli", tool=Tool.SHELL, cwd=tmp_path))
    fake_backend.previews[created.name] = "federation bundle"
    monkeypatch.setattr(
        service,
        "federated_sessions",
        lambda hosts=None: [{"host": "vm-a", "error": "", "sessions": []}],
    )
    monkeypatch.setattr(
        service,
        "federated_action",
        lambda action, hosts=None, args=(): [
            {"host": "vm-a", "ok": True, "error": "", "stdout": "{}"}
        ],
    )
    destination = tmp_path / "incident-fed-cli-bundle.tar.gz"

    result = CliRunner().invoke(
        cli.app,
        [
            "incident-bundle",
            "--session",
            created.name,
            "--include-federation",
            "--host",
            "vm-a",
            "--output",
            str(destination),
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["federation_included"] is True
    assert payload["federation_hosts"] == ["vm-a"]
    assert destination.is_file()


def test_federation_action_command_forwards_approval_code(
    service: SessionService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(Runtime, "service", lambda self: service)
    captured: dict[str, object] = {}

    def fake_federated_action(
        action,
        *,
        hosts=None,
        args=(),
        approval_code="",
        approval_tokens=(),
        retry_attempts=None,
        retry_delay_seconds=None,
    ):
        captured["action"] = action
        captured["hosts"] = list(hosts or [])
        captured["args"] = list(args)
        captured["approval_code"] = approval_code
        captured["approval_tokens"] = list(approval_tokens)
        captured["retry_attempts"] = retry_attempts
        captured["retry_delay_seconds"] = retry_delay_seconds
        return []

    monkeypatch.setattr(service, "federated_action", fake_federated_action)
    result = CliRunner().invoke(
        cli.app,
        [
            "federation-action",
            "resume",
            "--host",
            "vm-a",
            "--approval",
            "2468",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    assert captured == {
        "action": "resume",
        "hosts": ["vm-a"],
        "args": [],
        "approval_code": "2468",
        "approval_tokens": [],
        "retry_attempts": None,
        "retry_delay_seconds": None,
    }


def test_federation_action_command_forwards_approval_tokens(
    service: SessionService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(Runtime, "service", lambda self: service)
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        service,
        "federated_action",
        lambda action, **kwargs: captured.update({"action": action, **kwargs}) or [],
    )
    result = CliRunner().invoke(
        cli.app,
        [
            "federation-action",
            "resume",
            "--host",
            "vm-a",
            "--approval-token",
            "token-a",
            "--approval-token",
            "token-b",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    assert captured["approval_tokens"] == ("token-a", "token-b")


def test_federation_action_command_reports_partial_success(
    service: SessionService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(Runtime, "service", lambda self: service)
    monkeypatch.setattr(
        service,
        "federated_action",
        lambda action, **_kwargs: [
            {"host": "vm-a", "ok": True, "attempt_count": 1, "stdout": "ok", "error": ""},
            {
                "host": "vm-b",
                "ok": False,
                "attempt_count": 2,
                "stdout": "",
                "error": "timeout",
                "failure_summary": "failed after 2 attempt(s): timeout",
            },
        ],
    )
    result = CliRunner().invoke(
        cli.app,
        ["federation-action", "health", "--host", "vm-a", "--host", "vm-b"],
    )
    assert result.exit_code == 0, result.output
    assert "Partial success: 1/2 hosts succeeded." in result.stdout
    assert "vm-b" in result.stdout


def test_federation_capabilities_command_outputs_json_matrix(
    service: SessionService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(Runtime, "service", lambda self: service)
    monkeypatch.setattr(
        service,
        "federation_capability_matrix",
        lambda hosts=None: [
            {
                "host": "vm-a",
                "session_error": "",
                "session_count": 2,
                "commands": {"list": {"supported": True, "reason": ""}},
                "tools": {"copilot": {"supported": False, "status": "warn", "reason": "not found"}},
            }
        ],
    )
    result = CliRunner().invoke(
        cli.app,
        ["federation-capabilities", "--host", "vm-a", "--json"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload[0]["host"] == "vm-a"
    assert payload[0]["session_count"] == 2
    assert payload[0]["tools"]["copilot"]["supported"] is False


def test_federation_action_plan_command_outputs_json(
    service: SessionService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(Runtime, "service", lambda self: service)
    monkeypatch.setattr(
        service,
        "federation_action_plan",
        lambda action, hosts=None, args=(), approval_code="", approval_tokens=(): {
            "action": action,
            "targets": {"host_count": 1, "reachable_hosts": 1},
            "blast_radius": {
                "session_count": 2,
                "blocked_sessions": 1,
                "needs_input_sessions": 0,
                "risk_level": "high",
            },
            "approval": {
                "required": True,
                "satisfied": False,
                "contexts": [],
                "missing_contexts": ["federation-action:resume:vm-a"],
            },
            "hosts": [{"host": "vm-a"}],
        },
    )
    result = CliRunner().invoke(
        cli.app,
        ["federation-action-plan", "resume", "--host", "vm-a", "--json"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["action"] == "resume"
    assert payload["approval"]["required"] is True


def test_federation_dashboard_commands_save_list_and_open(
    service: SessionService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(Runtime, "service", lambda self: service)
    save = CliRunner().invoke(
        cli.app,
        [
            "federation-dashboard",
            "save",
            "ops-view",
            "--host",
            "vm-a",
            "--host",
            "vm-b",
            "--host-filter",
            "vm",
            "--scope-all-hosts",
            "--safe-mode",
        ],
    )
    assert save.exit_code == 0, save.output
    listed = CliRunner().invoke(cli.app, ["federation-dashboard", "list", "--json"])
    assert listed.exit_code == 0, listed.output
    payload = json.loads(listed.stdout)
    assert payload and payload[0]["name"] == "ops-view"
    assert payload[0]["safe_mode"] is True
    monkeypatch.setattr(
        service,
        "federated_sessions",
        lambda hosts=None: [{"host": "vm-a", "error": "", "sessions": []}],
    )
    opened = CliRunner().invoke(
        cli.app,
        ["federation-dashboard", "open", "ops-view", "--json"],
    )
    assert opened.exit_code == 0, opened.output
    open_payload = json.loads(opened.stdout)
    assert open_payload["dashboard"]["name"] == "ops-view"


def test_fleet_snapshot_and_fleet_diff_commands_output_json(
    service: SessionService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(Runtime, "service", lambda self: service)
    monkeypatch.setattr(
        service,
        "save_fleet_snapshot",
        lambda name, hosts=(): {
            "name": name,
            "created_at": "2026-08-06T00:00:00+00:00",
            "hosts": ["vm-a"],
            "summary": {"vm-a": {"session_count": 1}},
        },
    )
    saved = CliRunner().invoke(cli.app, ["fleet-snapshot", "save", "baseline", "--json"])
    assert saved.exit_code == 0, saved.output
    saved_payload = json.loads(saved.stdout)
    assert saved_payload["name"] == "baseline"

    monkeypatch.setattr(
        service,
        "fleet_diff",
        lambda left, right="", hosts=(): {
            "left": {"name": left, "created_at": "2026-08-06T00:00:00+00:00"},
            "right": {"name": right or "live", "created_at": "2026-08-06T01:00:00+00:00"},
            "hosts": ["vm-a"],
            "drifts": [
                {"host": "vm-a", "field": "session_count", "left": 1, "right": 2, "delta": 1}
            ],
            "host_spread": [],
        },
    )
    diff = CliRunner().invoke(cli.app, ["fleet-diff", "--left", "baseline", "--json"])
    assert diff.exit_code == 0, diff.output
    payload = json.loads(diff.stdout)
    assert payload["drifts"][0]["field"] == "session_count"


def test_self_heal_command_preview_outputs_policy_results(
    service: SessionService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service.config = service.config.model_copy(
        update={
            "self_heal": SelfHealConfig(
                enabled=True,
                rules=(
                    SelfHealRuleConfig(
                        name="heal-disk",
                        when_checks=("disk-space",),
                        action="recover-repair",
                    ),
                ),
            ),
        }
    )
    monkeypatch.setattr(
        service,
        "refresh_health_alerts",
        lambda force=False, only=None: [
            HealthCheck(name="disk-space", status=HealthStatus.WARN, detail="disk warning")
        ],
    )
    monkeypatch.setattr(Runtime, "service", lambda self: service)
    result = CliRunner().invoke(cli.app, ["self-heal", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["enabled"] is True
    assert payload["actions"]
    assert payload["actions"][0]["rule"] == "heal-disk"


def test_remediation_chain_command_outputs_json(
    service: SessionService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(Runtime, "service", lambda self: service)
    monkeypatch.setattr(
        service,
        "run_remediation_chain",
        lambda name, apply=False: {
            "name": name,
            "applied": apply,
            "triggered": True,
            "ok": True,
            "steps": [{"step": "archive", "ok": True}],
            "matched_checks": [],
            "rollback": None,
        },
    )
    result = CliRunner().invoke(cli.app, ["remediation-chain", "stable", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["name"] == "stable"
    assert payload["triggered"] is True


def test_incident_and_approval_commands_round_trip(
    service: SessionService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(Runtime, "service", lambda self: service)
    monkeypatch.setattr(
        service,
        "issue_approval_token",
        lambda action, operator="", ttl_seconds=None: {
            "token": "token-value",
            "action": action,
            "operator": operator or "ops",
            "issued_at": "2026-08-06T00:00:00+00:00",
            "expires_at": "2026-08-06T00:10:00+00:00",
            "ttl_seconds": 600,
        },
    )
    issue = CliRunner().invoke(
        cli.app,
        ["approval", "issue", "delete", "--operator", "ops", "--json"],
    )
    assert issue.exit_code == 0, issue.output
    expected_token = "token-value"  # noqa: S105
    assert json.loads(issue.stdout)["token"] == expected_token

    opened = service.start_incident(title="api-timeout", severity="warn")
    status = CliRunner().invoke(cli.app, ["incident", "status", str(opened["id"]), "--json"])
    assert status.exit_code == 0, status.output
    assert json.loads(status.stdout)["id"] == opened["id"]


def test_ssh_profiler_recommends_safe_mode_for_high_latency(
    service: SessionService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(Runtime, "service", lambda self: service)
    monkeypatch.setenv("SSH_CONNECTION", "1 2 3 4")
    monkeypatch.setattr(cli.os, "getloadavg", lambda: (9.5, 5.0, 2.0))
    result = CliRunner().invoke(
        cli.app,
        ["ssh-profiler", "--latency-ms", "1200", "--width", "90", "--height", "24", "--json"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["recommended_profile"] == "ssh-safe"
    assert payload["auto_safe_mode"] is True
    assert payload["signals"]["score"] >= 4
