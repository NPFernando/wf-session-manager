from __future__ import annotations

import os
import re
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest
from rich.console import Console
from textual.pilot import Pilot
from textual.widgets import Button, Input, LoadingIndicator, Static

from conftest import FakeBackend
from workspace_session_manager.config import HealthConfig
from workspace_session_manager.errors import TmuxError
from workspace_session_manager.models import CreateRequest, InputState, TaskState, Tool
from workspace_session_manager.service import SessionService
from workspace_session_manager.tui import (
    ConfirmActionScreen,
    CreateSessionScreen,
    DiagnosticsScreen,
    IdentityOrganizationScreen,
    LogScreen,
    StatusScreen,
    WsApp,
)

SnapCompare = Callable[..., bool]
FUTURE_ACTIVITY = datetime(2099, 1, 1, tzinfo=UTC)
pytestmark = [pytest.mark.layout_snapshot, pytest.mark.integration]
CI_FLAKY_LOG_SNAPSHOTS = pytest.mark.skipif(
    os.environ.get("GITHUB_ACTIONS") == "true",
    reason="Log SVG snapshots are currently flaky in GitHub Actions across Python versions.",
)
CI_FLAKY_LAYOUT_SNAPSHOTS = pytest.mark.skipif(
    os.environ.get("GITHUB_ACTIONS") == "true",
    reason=(
        "Some layout SVG snapshots are currently flaky in GitHub Actions across Python versions."
    ),
)

_ORIGINAL_EXPORT_SVG = Console.export_svg


def _export_svg_stable(self: Console, *args: object, **kwargs: object) -> str:
    rendered = _ORIGINAL_EXPORT_SVG(self, *args, **kwargs)
    rendered = re.sub(r"terminal-\d+", "terminal", rendered)
    rendered = re.sub(r"terminal-r\d+", "terminal-r", rendered)
    return re.sub(r"<style>.*?</style>", "<style></style>", rendered, flags=re.DOTALL)


Console.export_svg = _export_svg_stable


@pytest.fixture(autouse=True)
def deterministic_color(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("WS_SNAPSHOT_MODE", "1")
    monkeypatch.setenv("WS_SNAPSHOT_NOW", "2099-01-01T00:00:00+00:00")
    frozen = datetime(2099, 1, 1, tzinfo=UTC)

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz: object | None = None) -> datetime:
            return frozen if tz is not None else frozen.replace(tzinfo=None)

    monkeypatch.setattr("workspace_session_manager.service.datetime", FrozenDateTime)
    monkeypatch.setattr("workspace_session_manager.models.datetime", FrozenDateTime)
    monkeypatch.setattr("workspace_session_manager.store.datetime", FrozenDateTime)
    monkeypatch.setattr("workspace_session_manager.tui.datetime", FrozenDateTime)


def add_session(
    service: SessionService,
    backend: FakeBackend,
    name: str,
    tool: Tool,
    *,
    note: str = "",
    project: str = "",
    task_state: TaskState = TaskState.IN_PROGRESS,
    input_state: InputState = InputState.NONE,
    pinned: bool = False,
    attached: bool = False,
    failed: bool = False,
) -> str:
    view = service.create(
        CreateRequest(
            name=name,
            tool=tool,
            cwd=Path("/tmp"),
            project=project,
            note=note,
        )
    )
    service.organize(
        view.name,
        state=task_state,
        input_state=input_state,
        pinned=pinned,
    )
    backend.sessions[view.name] = backend.sessions[view.name].model_copy(
        update={
            "attached_clients": 1 if attached else 0,
            "last_activity_at": FUTURE_ACTIVITY,
            "pane_dead": failed,
            "pane_dead_status": 1 if failed else None,
        }
    )
    backend.previews[view.name] = (
        "Loading project context\n"
        "Validated configuration\n"
        "Reviewing implementation details\n"
        "Ready for the next workflow action"
    )
    return view.name


def populated_app(
    service: SessionService,
    backend: FakeBackend,
    *,
    monochrome: bool = False,
) -> WsApp:
    add_session(
        service,
        backend,
        "sample-release-workflow",
        Tool.CLAUDE,
        note="Build a fictional workflow prototype",
        project="example-team-release-hub",
        input_state=InputState.REQUIRED,
        attached=True,
    )
    add_session(
        service,
        backend,
        "sample-web-portal",
        Tool.CODEX,
        note="Refine responsive demo portal pages",
        project="sample-web-ui",
    )
    add_session(
        service,
        backend,
        "task-001",
        Tool.CLAUDE,
        task_state=TaskState.WAITING,
        pinned=True,
    )
    add_session(
        service,
        backend,
        "maintenance-shell",
        Tool.SHELL,
        task_state=TaskState.UNSPECIFIED,
    )
    return WsApp(
        service,
        monochrome=monochrome,
        hostname="wf-test-host",
        onboarding=False,
        default_cwd=Path("/"),
        no_animation=True,
    )


def logs_app(
    service: SessionService,
    backend: FakeBackend,
    *,
    output: str = "",
    tool: Tool = Tool.SHELL,
) -> tuple[WsApp, str]:
    name = add_session(
        service,
        backend,
        "api-refactor",
        tool,
        note="Improve API authentication",
        project="api-platform",
        attached=True,
    )
    backend.previews[name] = output
    return (
        WsApp(
            service,
            monochrome=False,
            hostname="wf-test-host",
            onboarding=False,
            no_animation=True,
        ),
        name,
    )


async def open_logs(pilot: Pilot, app: WsApp) -> LogScreen:
    await pilot.press("l")
    assert isinstance(app.screen, LogScreen)
    screen = app.screen
    for _ in range(80):
        if not screen.refreshing and (screen.captured_at is not None or screen.error_message):
            break
        await pilot.pause(0.05)
    assert not screen.refreshing
    await pilot.pause()
    return screen


async def wait_for_attention(pilot: Pilot, app: WsApp) -> None:
    for _ in range(80):
        if not app._attention_scanning:
            return
        await pilot.pause(0.05)
    raise AssertionError("attention scan did not complete")


@CI_FLAKY_LAYOUT_SNAPSHOTS
def test_wide_snapshot(
    snap_compare: SnapCompare,
    service: SessionService,
    fake_backend: FakeBackend,
) -> None:
    assert snap_compare(populated_app(service, fake_backend), terminal_size=(160, 45))


def test_standard_snapshot(
    snap_compare: SnapCompare,
    service: SessionService,
    fake_backend: FakeBackend,
) -> None:
    assert snap_compare(populated_app(service, fake_backend), terminal_size=(120, 35))


@CI_FLAKY_LAYOUT_SNAPSHOTS
def test_medium_snapshot(
    snap_compare: SnapCompare,
    service: SessionService,
    fake_backend: FakeBackend,
) -> None:
    assert snap_compare(populated_app(service, fake_backend), terminal_size=(100, 30))


def test_narrow_snapshot(
    snap_compare: SnapCompare,
    service: SessionService,
    fake_backend: FakeBackend,
) -> None:
    assert snap_compare(populated_app(service, fake_backend), terminal_size=(80, 24))


def test_very_narrow_snapshot(
    snap_compare: SnapCompare,
    service: SessionService,
    fake_backend: FakeBackend,
) -> None:
    assert snap_compare(populated_app(service, fake_backend), terminal_size=(50, 20))


def test_empty_snapshot(snap_compare: SnapCompare, service: SessionService) -> None:
    assert snap_compare(
        WsApp(service, monochrome=False, hostname="wf-test-host", onboarding=False),
        terminal_size=(120, 35),
    )


def test_health_row_hidden_when_no_alerts_snapshot(
    snap_compare: SnapCompare, service: SessionService, monkeypatch: pytest.MonkeyPatch
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

    class FakeUsage:
        total = 100
        free = 90

    monkeypatch.setattr("shutil.disk_usage", lambda _root: FakeUsage())
    app = WsApp(service, monochrome=False, hostname="wf-test-host", onboarding=False)

    async def wait_for_scan(pilot: Pilot) -> None:
        for _ in range(40):
            if not app._health_scanning:
                return
            await pilot.pause(0.05)

    assert snap_compare(app, terminal_size=(120, 35), run_before=wait_for_scan)


def test_health_row_shows_alert_snapshot(
    snap_compare: SnapCompare, service: SessionService, monkeypatch: pytest.MonkeyPatch
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
    app = WsApp(service, monochrome=False, hostname="wf-test-host", onboarding=False)

    async def wait_for_scan(pilot: Pilot) -> None:
        for _ in range(40):
            if not app._health_scanning:
                return
            await pilot.pause(0.05)

    assert snap_compare(app, terminal_size=(120, 35), run_before=wait_for_scan)


def test_warning_snapshot(
    snap_compare: SnapCompare,
    service: SessionService,
    fake_backend: FakeBackend,
) -> None:
    add_session(
        service,
        fake_backend,
        "decision-required",
        Tool.HERMES,
        note="Choose the deployment target before continuing",
        input_state=InputState.REQUIRED,
    )
    assert snap_compare(
        WsApp(service, monochrome=False, hostname="wf-test-host", onboarding=False),
        terminal_size=(120, 35),
    )


def test_failure_snapshot(
    snap_compare: SnapCompare,
    service: SessionService,
    fake_backend: FakeBackend,
) -> None:
    add_session(
        service,
        fake_backend,
        "failed-build",
        Tool.CODEX,
        note="Repair the packaging pipeline",
        failed=True,
    )
    assert snap_compare(
        WsApp(service, monochrome=False, hostname="wf-test-host", onboarding=False),
        terminal_size=(120, 35),
    )


def test_monochrome_snapshot(
    snap_compare: SnapCompare,
    service: SessionService,
    fake_backend: FakeBackend,
) -> None:
    assert snap_compare(
        populated_app(service, fake_backend, monochrome=True),
        terminal_size=(120, 35),
    )


@CI_FLAKY_LAYOUT_SNAPSHOTS
def test_long_content_snapshot(
    snap_compare: SnapCompare,
    service: SessionService,
    fake_backend: FakeBackend,
) -> None:
    name = add_session(
        service,
        fake_backend,
        "extremely-long-session-name-for-responsive-terminal-layout-validation",
        Tool.CLAUDE,
        note=(
            "Build and validate a deliberately long workflow description that must wrap cleanly "
            "inside the inspector without hiding status, output, or available actions."
        ),
        project="long-content-and-responsive-layout-validation-project",
        attached=True,
    )
    fake_backend.previews[name] = "\n".join(
        f"Output line {index}: validating bounded rendering and horizontal clipping"
        for index in range(20)
    )
    assert snap_compare(
        WsApp(service, monochrome=False, hostname="wf-test-host", onboarding=False),
        terminal_size=(160, 45),
    )


def test_light_snapshot(
    snap_compare: SnapCompare,
    service: SessionService,
    fake_backend: FakeBackend,
) -> None:
    app = populated_app(service, fake_backend)
    app.ui_theme = "light"
    app.theme = "light"
    app._refresh_theme_colors()
    assert snap_compare(app, terminal_size=(120, 35))


@pytest.mark.parametrize("theme", ["dark", "midnight", "cyberpunk", "terminal", "paper"])
def test_named_theme_snapshot(
    snap_compare: SnapCompare,
    service: SessionService,
    fake_backend: FakeBackend,
    theme: str,
) -> None:
    app = populated_app(service, fake_backend)
    app.ui_theme = theme
    app.theme = theme
    app._refresh_theme_colors()
    assert snap_compare(app, terminal_size=(120, 35))


@pytest.mark.parametrize("theme", ["ithaca", "cyberpunk", "paper"])
def test_toast_theme_snapshot(
    snap_compare: SnapCompare,
    service: SessionService,
    fake_backend: FakeBackend,
    theme: str,
) -> None:
    app = populated_app(service, fake_backend)
    app.ui_theme = theme
    app.theme = theme
    app._refresh_theme_colors()

    def show_toast(pilot: Pilot) -> None:
        app.notify("Theme toast styling check", severity="warning", timeout=30)

    assert snap_compare(
        app,
        terminal_size=(120, 35),
        run_before=show_toast,
    )


def test_diagnostics_modal_snapshot(
    snap_compare: SnapCompare,
    service: SessionService,
    fake_backend: FakeBackend,
) -> None:
    app = populated_app(service, fake_backend)

    async def open_diagnostics(pilot: Pilot) -> None:
        await wait_for_attention(pilot, app)
        app.action_diagnostics()
        await pilot.pause()

    assert snap_compare(app, terminal_size=(120, 35), run_before=open_diagnostics)


def test_diagnostics_running_snapshot(
    snap_compare: SnapCompare,
    service: SessionService,
    fake_backend: FakeBackend,
) -> None:
    app = populated_app(service, fake_backend)

    async def open_running_diagnostics(pilot: Pilot) -> None:
        screen = DiagnosticsScreen(service)
        screen.action_run = lambda: None  # type: ignore[method-assign]
        app.push_screen(screen)
        await pilot.pause()
        screen.running = True
        screen.query_one("#diagnostics-meta", Static).update("Running now")
        screen.query_one("#diagnostics-summary", Static).update(
            "Running diagnostics... Checking tmux and tool availability"
        )
        screen.query_one("#diagnostics-loading", LoadingIndicator).display = False
        for selector in ("#diagnostics-run", "#diagnostics-export", "#diagnostics-details"):
            screen.query_one(selector, Button).disabled = True
        await pilot.pause()

    assert snap_compare(app, terminal_size=(120, 35), run_before=open_running_diagnostics)


def test_filter_mode_snapshot(
    snap_compare: SnapCompare,
    service: SessionService,
    fake_backend: FakeBackend,
) -> None:
    app = populated_app(service, fake_backend)

    async def open_filter(pilot: Pilot) -> None:
        await pilot.press("f")
        await pilot.pause()

    assert snap_compare(app, terminal_size=(160, 45), run_before=open_filter)


def test_palette_mode_snapshot(
    snap_compare: SnapCompare,
    service: SessionService,
    fake_backend: FakeBackend,
) -> None:
    app = populated_app(service, fake_backend)

    async def open_palette(pilot: Pilot) -> None:
        await pilot.press("p")
        await pilot.pause()

    assert snap_compare(app, terminal_size=(120, 35), run_before=open_palette)


@pytest.mark.parametrize(
    "terminal_size",
    [(160, 45), (120, 35), (100, 30), (80, 24)],
    ids=["160x45", "120x35", "100x30", "80x24"],
)
def test_manage_responsive_snapshot(
    snap_compare: SnapCompare,
    service: SessionService,
    fake_backend: FakeBackend,
    terminal_size: tuple[int, int],
) -> None:
    app = populated_app(service, fake_backend)

    async def open_manage(pilot: Pilot) -> None:
        await pilot.press("d")
        await pilot.pause()

    assert snap_compare(app, terminal_size=terminal_size, run_before=open_manage)


def test_manage_filtered_snapshot(
    snap_compare: SnapCompare,
    service: SessionService,
    fake_backend: FakeBackend,
) -> None:
    app = populated_app(service, fake_backend)

    async def filter_manage(pilot: Pilot) -> None:
        await pilot.press("d", "/", *"owner-only", "enter")
        await pilot.pause()

    assert snap_compare(app, terminal_size=(120, 35), run_before=filter_manage)


def test_manage_disabled_snapshot(
    snap_compare: SnapCompare,
    service: SessionService,
    fake_backend: FakeBackend,
) -> None:
    app = populated_app(service, fake_backend)
    stopped = next(
        session.name
        for session in service.list_sessions()
        if "sample-release-workflow" in session.name
    )
    service.stop_session(stopped)

    async def open_manage(pilot: Pilot) -> None:
        await pilot.press("d")
        await pilot.pause()

    assert snap_compare(app, terminal_size=(120, 35), run_before=open_manage)


@pytest.mark.parametrize(
    "theme",
    ["dark", "light", "monochrome", "midnight", "cyberpunk", "terminal", "paper"],
)
def test_manage_theme_snapshot(
    snap_compare: SnapCompare,
    service: SessionService,
    fake_backend: FakeBackend,
    theme: str,
) -> None:
    app = populated_app(service, fake_backend, monochrome=theme == "monochrome")
    app.ui_theme = theme
    app.theme = theme
    app._refresh_theme_colors()

    async def open_manage(pilot: Pilot) -> None:
        await pilot.press("d")
        await pilot.pause()

    assert snap_compare(app, terminal_size=(120, 35), run_before=open_manage)


def test_manage_ascii_snapshot(
    snap_compare: SnapCompare,
    service: SessionService,
    fake_backend: FakeBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WS_ASCII", "1")
    app = populated_app(service, fake_backend)

    async def open_manage(pilot: Pilot) -> None:
        await pilot.press("d")
        await pilot.pause()

    assert snap_compare(app, terminal_size=(120, 35), run_before=open_manage)


@pytest.mark.parametrize(
    "terminal_size",
    [(160, 45), (120, 35), (100, 30), (80, 24)],
    ids=["160x45", "120x35", "100x30", "80x24"],
)
@CI_FLAKY_LOG_SNAPSHOTS
def test_logs_responsive_snapshot(
    snap_compare: SnapCompare,
    service: SessionService,
    fake_backend: FakeBackend,
    terminal_size: tuple[int, int],
) -> None:
    app, _name = logs_app(
        service,
        fake_backend,
        output="\n".join(
            (
                "Loading project context",
                "Validated API configuration",
                "Completed authentication audit",
                "Ready for the next workflow action",
            )
        ),
    )

    async def show_logs(pilot: Pilot) -> None:
        await open_logs(pilot, app)

    assert snap_compare(app, terminal_size=terminal_size, run_before=show_logs)


@CI_FLAKY_LOG_SNAPSHOTS
def test_logs_saved_snapshot(
    snap_compare: SnapCompare,
    service: SessionService,
    fake_backend: FakeBackend,
) -> None:
    created = service.create(
        CreateRequest(
            name="saved-output",
            tool=Tool.CLAUDE,
            cwd=Path("/tmp"),
            note="Review retained build output",
            logging_enabled=True,
        )
    )
    record = service.store.load(created.name)
    assert record is not None
    path = service.paths.logs_dir / f"{record.record_id}.log"
    path.write_text("Sanitized build output\nDeployment checks passed\n", encoding="utf-8")
    fake_backend.previews[created.name] = "Live pane is ready"
    app = WsApp(service, monochrome=False, onboarding=False, no_animation=True)

    async def show_saved(pilot: Pilot) -> None:
        screen = await open_logs(pilot, app)
        screen.query_one("#log-source-saved", Button).press()
        for _ in range(80):
            await pilot.pause(0.05)
            if not screen.refreshing and screen.output_source.value == "saved":
                break

    assert snap_compare(app, terminal_size=(120, 35), run_before=show_saved)


@CI_FLAKY_LOG_SNAPSHOTS
def test_logs_paused_snapshot(
    snap_compare: SnapCompare,
    service: SessionService,
    fake_backend: FakeBackend,
) -> None:
    app, _name = logs_app(service, fake_backend, output="Line one\nLine two\nLine three")

    async def show_paused(pilot: Pilot) -> None:
        await open_logs(pilot, app)
        await pilot.press("f")

    assert snap_compare(app, terminal_size=(120, 35), run_before=show_paused)


def test_logs_find_snapshot(
    snap_compare: SnapCompare,
    service: SessionService,
    fake_backend: FakeBackend,
) -> None:
    app, _name = logs_app(
        service,
        fake_backend,
        output="Project loaded\nAuthentication audit complete\nProject ready",
    )

    async def show_find(pilot: Pilot) -> None:
        await open_logs(pilot, app)
        await pilot.press("/", *"project")
        await pilot.pause()

    assert snap_compare(app, terminal_size=(120, 35), run_before=show_find)


@CI_FLAKY_LOG_SNAPSHOTS
def test_logs_warning_snapshot(
    snap_compare: SnapCompare,
    service: SessionService,
    fake_backend: FakeBackend,
) -> None:
    app, _name = logs_app(
        service,
        fake_backend,
        tool=Tool.CODEX,
        output="Warning: Codex usage limit reached\nRetry available: tomorrow at 10:00",
    )

    async def show_warning(pilot: Pilot) -> None:
        await open_logs(pilot, app)

    assert snap_compare(app, terminal_size=(120, 35), run_before=show_warning)


@CI_FLAKY_LOG_SNAPSHOTS
def test_logs_error_snapshot(
    snap_compare: SnapCompare,
    service: SessionService,
    fake_backend: FakeBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, _name = logs_app(service, fake_backend, output="Initial output")

    def fail_logs(*args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        raise TmuxError("tmux socket unavailable")

    monkeypatch.setattr(service, "logs", fail_logs)

    async def show_error(pilot: Pilot) -> None:
        await open_logs(pilot, app)

    assert snap_compare(app, terminal_size=(120, 35), run_before=show_error)


@CI_FLAKY_LOG_SNAPSHOTS
def test_logs_empty_snapshot(
    snap_compare: SnapCompare,
    service: SessionService,
    fake_backend: FakeBackend,
) -> None:
    app, _name = logs_app(service, fake_backend)

    async def show_empty(pilot: Pilot) -> None:
        await open_logs(pilot, app)

    assert snap_compare(app, terminal_size=(120, 35), run_before=show_empty)


@pytest.mark.parametrize(
    "theme",
    ["dark", "light", "monochrome", "midnight", "cyberpunk", "terminal", "paper"],
)
@CI_FLAKY_LOG_SNAPSHOTS
def test_logs_theme_snapshot(
    snap_compare: SnapCompare,
    service: SessionService,
    fake_backend: FakeBackend,
    theme: str,
) -> None:
    app, _name = logs_app(service, fake_backend, output="Sanitized output\nReady")
    app.ui_theme = theme
    app.theme = theme
    app._refresh_theme_colors()
    app.monochrome = theme == "monochrome"

    async def show_logs(pilot: Pilot) -> None:
        await open_logs(pilot, app)

    assert snap_compare(app, terminal_size=(120, 35), run_before=show_logs)


@CI_FLAKY_LOG_SNAPSHOTS
def test_logs_ascii_snapshot(
    snap_compare: SnapCompare,
    service: SessionService,
    fake_backend: FakeBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WS_ASCII", "1")
    app, _name = logs_app(service, fake_backend, output="Sanitized output\nReady")

    async def show_logs(pilot: Pilot) -> None:
        await open_logs(pilot, app)

    assert snap_compare(app, terminal_size=(120, 35), run_before=show_logs)


@pytest.mark.parametrize(
    "terminal_size",
    [(80, 24), (99, 30)],
    ids=["80x24", "99x30"],
)
def test_narrow_detail_responsive_snapshot(
    snap_compare: SnapCompare,
    service: SessionService,
    fake_backend: FakeBackend,
    terminal_size: tuple[int, int],
) -> None:
    app, _name = logs_app(
        service,
        fake_backend,
        output="Validated API configuration\nReady for the next workflow action",
    )

    async def show_detail(pilot: Pilot) -> None:
        await pilot.press("enter")
        await pilot.pause()
        assert app.narrow_detail_open

    assert snap_compare(app, terminal_size=terminal_size, run_before=show_detail)


def test_narrow_detail_warning_snapshot(
    snap_compare: SnapCompare,
    service: SessionService,
    fake_backend: FakeBackend,
) -> None:
    app, _name = logs_app(
        service,
        fake_backend,
        tool=Tool.CODEX,
        output="Warning: Codex usage limit reached\nRetry available: tomorrow at 10:00",
    )

    async def show_detail(pilot: Pilot) -> None:
        await pilot.press("enter")
        await pilot.pause()

    assert snap_compare(app, terminal_size=(80, 24), run_before=show_detail)


def test_narrow_detail_failure_snapshot(
    snap_compare: SnapCompare,
    service: SessionService,
    fake_backend: FakeBackend,
) -> None:
    name = add_session(
        service,
        fake_backend,
        "failed-worker",
        Tool.SHELL,
        note="Inspect the failed background worker",
        failed=True,
    )
    fake_backend.previews[name] = "Worker exited with status 1"
    app = WsApp(service, monochrome=False, onboarding=False, no_animation=True)

    async def show_detail(pilot: Pilot) -> None:
        await pilot.press("enter")
        await pilot.pause()

    assert snap_compare(app, terminal_size=(80, 24), run_before=show_detail)


def test_narrow_detail_long_content_snapshot(
    snap_compare: SnapCompare,
    service: SessionService,
    fake_backend: FakeBackend,
) -> None:
    name = add_session(
        service,
        fake_backend,
        "api-authentication-and-authorization-refactor-with-a-long-name",
        Tool.CLAUDE,
        note=(
            "Refactor API authentication, authorization, token rotation, audit logging, "
            "and failure recovery across all public endpoints."
        ),
        project="enterprise-api-platform-with-shared-services",
    )
    fake_backend.previews[name] = "Reviewing authentication boundaries\nAudit remains in progress"
    app = WsApp(service, monochrome=False, onboarding=False, no_animation=True)

    async def show_detail(pilot: Pilot) -> None:
        await pilot.press("enter")
        await pilot.pause()

    assert snap_compare(app, terminal_size=(99, 30), run_before=show_detail)


@pytest.mark.parametrize(
    "theme",
    ["dark", "light", "monochrome", "midnight", "cyberpunk", "terminal", "paper"],
)
def test_narrow_detail_theme_snapshot(
    snap_compare: SnapCompare,
    service: SessionService,
    fake_backend: FakeBackend,
    theme: str,
) -> None:
    app, _name = logs_app(service, fake_backend, output="Sanitized output\nReady")
    app.ui_theme = theme
    app.theme = theme
    app._refresh_theme_colors()
    app.monochrome = theme == "monochrome"

    async def show_detail(pilot: Pilot) -> None:
        await pilot.press("enter")
        await pilot.pause()

    assert snap_compare(app, terminal_size=(80, 24), run_before=show_detail)


def test_narrow_detail_ascii_snapshot(
    snap_compare: SnapCompare,
    service: SessionService,
    fake_backend: FakeBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WS_ASCII", "1")
    app, _name = logs_app(service, fake_backend, output="Sanitized output\nReady")

    async def show_detail(pilot: Pilot) -> None:
        await pilot.press("enter")
        await pilot.pause()

    assert snap_compare(app, terminal_size=(80, 24), run_before=show_detail)


def test_attention_scanning_snapshot(
    snap_compare: SnapCompare,
    service: SessionService,
    fake_backend: FakeBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = populated_app(service, fake_backend)
    monkeypatch.setattr(app, "_start_attention_scan", lambda: None)
    assert snap_compare(app, terminal_size=(120, 35))


@pytest.mark.parametrize(
    "terminal_size",
    [(120, 35), (80, 24)],
    ids=["120x35", "80x24"],
)
def test_attention_view_responsive_snapshot(
    snap_compare: SnapCompare,
    service: SessionService,
    fake_backend: FakeBackend,
    terminal_size: tuple[int, int],
) -> None:
    app = populated_app(service, fake_backend)

    async def show_attention(pilot: Pilot) -> None:
        await wait_for_attention(pilot, app)
        app.action_attention()
        await pilot.pause()

    assert snap_compare(app, terminal_size=terminal_size, run_before=show_attention)


def test_attention_complete_empty_snapshot(
    snap_compare: SnapCompare,
    service: SessionService,
    fake_backend: FakeBackend,
) -> None:
    app, _name = logs_app(service, fake_backend, tool=Tool.CLAUDE, output="Ready")

    async def show_attention(pilot: Pilot) -> None:
        await wait_for_attention(pilot, app)
        app.action_attention()
        await pilot.pause()

    assert snap_compare(app, terminal_size=(120, 35), run_before=show_attention)


def test_attention_monochrome_snapshot(
    snap_compare: SnapCompare,
    service: SessionService,
    fake_backend: FakeBackend,
) -> None:
    app = populated_app(service, fake_backend, monochrome=True)

    async def show_attention(pilot: Pilot) -> None:
        await wait_for_attention(pilot, app)
        app.action_attention()
        await pilot.pause()

    assert snap_compare(app, terminal_size=(80, 24), run_before=show_attention)


def test_attention_ascii_snapshot(
    snap_compare: SnapCompare,
    service: SessionService,
    fake_backend: FakeBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WS_ASCII", "1")
    app = populated_app(service, fake_backend)

    async def show_attention(pilot: Pilot) -> None:
        await wait_for_attention(pilot, app)
        app.action_attention()
        await pilot.pause()

    assert snap_compare(app, terminal_size=(80, 24), run_before=show_attention)


def test_identity_form_snapshot(
    snap_compare: SnapCompare,
    service: SessionService,
    fake_backend: FakeBackend,
) -> None:
    app = populated_app(service, fake_backend)

    async def open_identity(pilot: Pilot) -> None:
        await pilot.press("d", "e")
        await pilot.pause(0.25)
        assert isinstance(app.screen, IdentityOrganizationScreen)

    assert snap_compare(app, terminal_size=(120, 35), run_before=open_identity)


def test_status_form_snapshot(
    snap_compare: SnapCompare,
    service: SessionService,
    fake_backend: FakeBackend,
) -> None:
    app = populated_app(service, fake_backend)

    async def open_status(pilot: Pilot) -> None:
        await pilot.press("d", "s")
        await pilot.pause()
        assert isinstance(app.screen, StatusScreen)

    assert snap_compare(app, terminal_size=(120, 35), run_before=open_status)


def test_create_form_snapshot(
    snap_compare: SnapCompare,
    service: SessionService,
    fake_backend: FakeBackend,
) -> None:
    app = populated_app(service, fake_backend)

    async def enter_valid_name(pilot: Pilot) -> None:
        await pilot.press("c")
        app.screen.query_one("#create-name", Input).value = "api-refactor"
        await pilot.pause(0.25)

    assert snap_compare(app, terminal_size=(120, 35), run_before=enter_valid_name)


def test_create_validation_error_snapshot(
    snap_compare: SnapCompare,
    service: SessionService,
    fake_backend: FakeBackend,
) -> None:
    app = populated_app(service, fake_backend)

    async def enter_invalid_values(pilot: Pilot) -> None:
        await pilot.press("c")
        app.screen.query_one("#create-name", Input).value = "sample-web-portal"
        app.screen.query_one("#create-cwd", Input).value = "/missing/wf-directory"
        await pilot.pause(0.25)

    assert snap_compare(app, terminal_size=(120, 35), run_before=enter_invalid_values)


def test_create_advanced_options_snapshot(
    snap_compare: SnapCompare,
    service: SessionService,
    fake_backend: FakeBackend,
) -> None:
    app = populated_app(service, fake_backend)

    async def open_advanced_options(pilot: Pilot) -> None:
        await pilot.press("c")
        await pilot.pause()
        assert isinstance(app.screen, CreateSessionScreen)
        app.screen._set_advanced(True)
        app.screen.query_one("#create-advanced-toggle", Button).focus()
        await pilot.pause(0.25)

    assert snap_compare(app, terminal_size=(120, 35), run_before=open_advanced_options)


@CI_FLAKY_LAYOUT_SNAPSHOTS
def test_usage_limit_warning_snapshot(
    snap_compare: SnapCompare,
    service: SessionService,
    fake_backend: FakeBackend,
) -> None:
    name = add_session(service, fake_backend, "usage-limited", Tool.CODEX)
    fake_backend.previews[name] = (
        "Warning: Codex usage limit reached\nRetry available: 23 Jul 2026, 10:46 AM"
    )
    assert snap_compare(
        WsApp(
            service,
            monochrome=False,
            hostname="wf-test-host",
            onboarding=False,
            no_animation=True,
        ),
        terminal_size=(120, 35),
    )


def test_reduced_motion_snapshot(
    snap_compare: SnapCompare,
    service: SessionService,
    fake_backend: FakeBackend,
) -> None:
    app = populated_app(service, fake_backend)
    assert app.motion == "off"
    assert snap_compare(app, terminal_size=(120, 35))


def test_destructive_confirmation_snapshot(
    snap_compare: SnapCompare,
    service: SessionService,
    fake_backend: FakeBackend,
) -> None:
    app = populated_app(service, fake_backend)

    async def open_confirmation(pilot: Pilot) -> None:
        await pilot.press("d")
        app.screen.action_choose("stop-session")
        for _ in range(40):
            await pilot.pause(0.05)
            if isinstance(app.screen, ConfirmActionScreen):
                break
        assert isinstance(app.screen, ConfirmActionScreen)

    assert snap_compare(app, terminal_size=(120, 35), run_before=open_confirmation)


@CI_FLAKY_LAYOUT_SNAPSHOTS
@pytest.mark.parametrize("count", [50, 200])
def test_large_inventory_snapshot(
    snap_compare: SnapCompare,
    service: SessionService,
    fake_backend: FakeBackend,
    count: int,
) -> None:
    for index in range(count):
        add_session(service, fake_backend, f"load-{index:03d}", Tool.SHELL)
    assert snap_compare(
        WsApp(service, monochrome=False, hostname="wf-test-host", onboarding=False),
        terminal_size=(160, 45),
    )
