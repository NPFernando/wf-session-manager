from __future__ import annotations

import json
from collections import deque
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event

import pytest
from textual.app import App
from textual.command import CommandPalette
from textual.containers import VerticalScroll
from textual.pilot import Pilot
from textual.widgets import Button, Input, OptionList, Select, Static, Switch, TextArea

from conftest import FakeBackend
from workspace_session_manager.config import (
    HealthConfig,
    InterfaceConfig,
    NotificationConfig,
    SlaRuleConfig,
)
from workspace_session_manager.errors import TmuxError
from workspace_session_manager.models import (
    AgentState,
    CreateRequest,
    FilterPreset,
    HealthCheck,
    HealthStatus,
    InputState,
    OutputSource,
    RuntimeState,
    SessionDetails,
    SessionView,
    TaskState,
    Tool,
)
from workspace_session_manager.service import SessionService
from workspace_session_manager.tui import (
    ACTIVITY_SPARK_MIN_SAMPLES,
    THEME_MODES,
    TOOL_LABELS,
    ConfirmActionScreen,
    CreateFailureScreen,
    CreateSessionScreen,
    DeleteSessionScreen,
    DependencyGraphScreen,
    DiagnosticsScreen,
    FederationControlScreen,
    FilterScreen,
    FilterState,
    HealthAlertsScreen,
    IdentityOrganizationScreen,
    InteractionMode,
    InterfaceControlsScreen,
    LogScreen,
    ManageSessionScreen,
    MoreActionsScreen,
    NoteScreen,
    OnboardingScreen,
    PolicySandboxScreen,
    SearchOutputScreen,
    StatusScreen,
    WsApp,
    build_session_groups,
    condensed_session_label,
    detect_activity,
    display_path,
    humanize_task,
    relative_activity,
    session_group,
    session_option_id,
    sparkline,
)

pytestmark = pytest.mark.tui_behavior


def create_managed(service: SessionService, name: str, tool: Tool) -> str:
    return service.create(CreateRequest(name=name, tool=tool, cwd=Path("/tmp"))).name


def test_snapshot_mode_freezes_clock_and_disables_motion(
    service: SessionService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WS_SNAPSHOT_MODE", "1")
    monkeypatch.setenv("WS_SNAPSHOT_NOW", "2099-01-01T00:00:00+00:00")
    app = WsApp(service, onboarding=False)
    assert app.snapshot_mode is True
    assert app.motion == "off"
    assert app._now_utc() == datetime(2099, 1, 1, tzinfo=UTC)


async def wait_for_create_validation(pilot: Pilot[object], screen: CreateSessionScreen) -> None:
    for _ in range(40):
        if screen._validated_signature == screen._signature():
            return
        await pilot.pause(0.05)
    raise AssertionError("create-session validation did not complete")


async def wait_for_confirmation(pilot: Pilot[object], app: WsApp) -> ConfirmActionScreen:
    for _ in range(40):
        if isinstance(app.screen, ConfirmActionScreen) and app.screen.query("#confirm-submit"):
            return app.screen
        await pilot.pause(0.05)
    raise AssertionError("confirmation screen did not open")


async def wait_for_manage(pilot: Pilot[object], app: WsApp) -> ManageSessionScreen:
    for _ in range(40):
        if isinstance(app.screen, ManageSessionScreen):
            return app.screen
        await pilot.pause(0.05)
    raise AssertionError("manage screen did not open")


async def wait_for_search_output_screen(pilot: Pilot[object], app: WsApp) -> SearchOutputScreen:
    for _ in range(40):
        if isinstance(app.screen, SearchOutputScreen):
            return app.screen
        await pilot.pause(0.05)
    raise AssertionError("search output screen did not open")


async def wait_for_output_search(pilot: Pilot[object], screen: SearchOutputScreen) -> None:
    for _ in range(80):
        options = screen.query_one("#search-output-results", OptionList)
        ids = {options.get_option_at_index(index).id for index in range(options.option_count)}
        if screen._debounce_timer is None and "search-output-loading" not in ids:
            return
        await pilot.pause(0.05)
    raise AssertionError("output search did not complete")


async def wait_for_identity_validation(
    pilot: Pilot[object], screen: IdentityOrganizationScreen
) -> None:
    for _ in range(40):
        status = screen.query_one("#identity-name-status", Static)
        if not status.has_class("checking"):
            return
        await pilot.pause(0.05)
    raise AssertionError("identity validation did not complete")


async def wait_for_log_refresh(pilot: Pilot[object], screen: LogScreen) -> None:
    for _ in range(80):
        if not screen.refreshing and screen.captured_at is not None:
            return
        await pilot.pause(0.05)
    raise AssertionError("log refresh did not complete")


async def wait_for_detail_refresh(pilot: Pilot[object], app: WsApp) -> None:
    for _ in range(80):
        if not app._detail_refreshing:
            return
        await pilot.pause(0.05)
    raise AssertionError("detail refresh did not complete")


async def wait_for_attention_scan(pilot: Pilot[object], app: WsApp) -> None:
    saw_work = False
    for _ in range(120):
        saw_work = saw_work or app._session_refreshing or app._attention_scanning
        if (
            not app._session_refreshing
            and not app._attention_scanning
            and (saw_work or app._attention_baseline_established)
        ):
            return
        await pilot.pause(0.05)
    raise AssertionError("attention scan did not complete")


async def wait_for_health_scan(pilot: Pilot[object], app: WsApp) -> None:
    for _ in range(120):
        if not app._health_scanning:
            return
        await pilot.pause(0.05)
    raise AssertionError("health scan did not complete")


async def wait_for_federation_idle(pilot: Pilot[object], screen: FederationControlScreen) -> None:
    for _ in range(80):
        if not screen._loading:
            return
        await pilot.pause(0.05)
    raise AssertionError("federation screen remained busy")


def enable_health(
    service: SessionService, *, disk_warn_percent: int = 10, disk_fail_percent: int = 2
) -> None:
    """Health checks are disabled by the shared `service` fixture (hermetic
    by default); tests exercising the health-cockpit feature opt in here with
    only the disk-space check enabled, so no real apt/docker/git subprocess
    ever runs during a TUI test."""
    service.config = service.config.model_copy(
        update={
            "health": HealthConfig(
                enabled=True,
                apt_updates_enabled=False,
                reboot_required_enabled=False,
                git_dirty_enabled=False,
                docker_enabled=False,
                zombie_sessions_enabled=False,
                idle_sessions_enabled=False,
                orphaned_logs_enabled=False,
                missing_cwd_enabled=False,
                disk_ttl_seconds=5.0,
                disk_warn_percent=disk_warn_percent,
                disk_fail_percent=disk_fail_percent,
            )
        }
    )


def test_sparkline_empty_history_renders_nothing() -> None:
    assert sparkline(None) == ""
    assert sparkline(deque()) == ""


def test_sparkline_all_zero_history_renders_flattest_glyph() -> None:
    result = sparkline(deque([0, 0, 0]))
    assert result == "▁▁▁"


def test_sparkline_scales_relative_to_peak_sample() -> None:
    result = sparkline(deque([0, 50, 100]))
    assert result[0] == "▁"
    assert result[-1] == "█"
    assert result[0] != result[1] != result[2]


def test_sparkline_ascii_fallback_uses_ascii_glyphs() -> None:
    result = sparkline(deque([0, 100]), ascii_only=True)
    assert all(char in "_.-:=+*#" for char in result)


@pytest.mark.asyncio
async def test_tui_loads_grouped_rows_and_searches_on_demand(
    service: SessionService,
) -> None:
    create_managed(service, "first", Tool.CLAUDE)
    create_managed(service, "second", Tool.CODEX)
    app = WsApp(service, monochrome=False, onboarding=False)
    async with app.run_test(size=(120, 36)) as pilot:
        assert len(app.visible_sessions) == 2
        assert not app.has_class("searching")
        assert app.query_one("#search-mode").display is False

        await pilot.press("/")
        await pilot.press("c", "o", "d", "e", "x")
        await pilot.pause()
        assert app.has_class("searching")
        assert [item.tool for item in app.visible_sessions] == [Tool.CODEX]

        await pilot.press("escape")
        await pilot.pause()
        assert not app.has_class("searching")
        assert app.filter_query == ""
        assert len(app.visible_sessions) == 2


@pytest.mark.asyncio
async def test_search_enter_commits_filter_without_attaching(service: SessionService) -> None:
    create_managed(service, "first", Tool.CLAUDE)
    create_managed(service, "second", Tool.CODEX)
    app = WsApp(service, monochrome=False)
    async with app.run_test(size=(120, 36)) as pilot:
        await pilot.press("/")
        await pilot.press("c", "o", "d", "e", "x", "enter")
        await pilot.pause()
        assert app.filter_query == "codex"
        assert not app.has_class("searching")
        assert app.return_value is None
        assert app.selected_name == "codex-second"


@pytest.mark.asyncio
async def test_wide_enter_attaches_selected_session(service: SessionService) -> None:
    create_managed(service, "first", Tool.CLAUDE)
    app = WsApp(service, monochrome=False)
    async with app.run_test(size=(120, 36)) as pilot:
        await pilot.pause()
        await pilot.press("enter")
    assert app.return_value == "claude-first"


@pytest.mark.asyncio
async def test_narrow_enter_opens_in_place_detail_then_attaches(service: SessionService) -> None:
    create_managed(service, "first", Tool.CLAUDE)
    app = WsApp(service, monochrome=False)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.press("enter")
        await pilot.pause()
        assert app.narrow_detail_open
        assert app.has_class("narrow-detail")
        assert app.query_one("#detail-pane").display
        assert not app.query_one("#session-pane").display
        assert "Enter Attach" in str(app.query_one("#action-bar", Static).content)
        await pilot.press("enter")
    assert app.return_value == "claude-first"


@pytest.mark.asyncio
async def test_narrow_detail_actions_card_buttons_are_visible_and_functional(
    service: SessionService,
) -> None:
    name = create_managed(service, "touch-target", Tool.CLAUDE)
    app = WsApp(service, monochrome=False, onboarding=False)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.press("enter")
        await pilot.pause()
        assert app.narrow_detail_open
        assert app.query_one("#actions-card").display
        open_button = app.query_one("#action-open", Button)
        manage_button = app.query_one("#action-manage", Button)
        logs_button = app.query_one("#action-logs", Button)
        pin_button = app.query_one("#action-pin", Button)
        assert not open_button.disabled
        assert not manage_button.disabled
        assert not logs_button.disabled
        assert not pin_button.disabled
        assert str(pin_button.label) == "Pin"

        await pilot.click("#action-pin")
        await wait_for_detail_refresh(pilot, app)
        await pilot.pause()
        assert str(pin_button.label) == "Unpin"
        session = next(item for item in app.sessions if item.name == name)
        assert session.pinned

        await pilot.click("#action-manage")
        await pilot.pause()
        assert isinstance(app.screen, ManageSessionScreen)


@pytest.mark.asyncio
async def test_narrow_detail_restores_viewports_after_forms_logs_and_back(
    service: SessionService,
    fake_backend: FakeBackend,
) -> None:
    name = create_managed(service, "narrow-workspace", Tool.CLAUDE)
    service.update_note(name, "\n".join(f"Task detail {index}" for index in range(24)))
    fake_backend.previews[name] = "\n".join(f"output line {index}" for index in range(40))
    app = WsApp(service, monochrome=False, onboarding=False, no_animation=True)

    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.press("enter")
        assert app.narrow_detail_open
        app.output_mode = "raw"
        app._render_details(name)
        await wait_for_detail_refresh(pilot, app)
        inspector = app.query_one("#inspector-scroll", VerticalScroll)
        output = app.query_one("#recent-output-scroll", VerticalScroll)
        await pilot.press("j")
        await pilot.pause()
        assert inspector.scroll_offset.y > 0
        inspector.scroll_to(y=5, animate=False, force=True)
        output.scroll_to(y=7, animate=False, force=True)
        await pilot.pause()
        expected_inspector_y = inspector.scroll_offset.y
        expected_output_y = output.scroll_offset.y
        assert expected_inspector_y > 0
        assert expected_output_y > 0

        app.refresh_sessions()
        await pilot.pause()
        assert app.narrow_detail_open
        assert app.output_mode == "raw"
        assert inspector.scroll_offset.y == expected_inspector_y
        assert output.scroll_offset.y == expected_output_y

        await pilot.press("e")
        await pilot.pause()
        assert isinstance(app.screen, IdentityOrganizationScreen)
        await pilot.press("escape")
        await pilot.pause()
        assert app.narrow_detail_open
        assert inspector.scroll_offset.y == expected_inspector_y
        assert output.scroll_offset.y == expected_output_y

        await pilot.press("l")
        assert isinstance(app.screen, LogScreen)
        await wait_for_log_refresh(pilot, app.screen)
        await pilot.press("escape")
        await pilot.pause()
        assert app.narrow_detail_open
        assert inspector.scroll_offset.y == expected_inspector_y
        assert output.scroll_offset.y == expected_output_y

        await pilot.press("question_mark")
        await pilot.pause()
        assert "Scroll details" in str(app.screen.query_one("#message-content", Static).content)
        await pilot.press("escape")
        await pilot.pause()
        assert app.narrow_detail_open

        await pilot.press("escape")
        assert not app.narrow_detail_open
        assert app.query_one("#session-pane").display
        await pilot.press("enter")
        await pilot.pause()
        assert app.narrow_detail_open
        assert app.output_mode == "raw"
        assert inspector.scroll_offset.y == expected_inspector_y
        assert output.scroll_offset.y == expected_output_y


@pytest.mark.asyncio
async def test_narrow_detail_search_and_filter_return_to_list(service: SessionService) -> None:
    create_managed(service, "narrow-modes", Tool.SHELL)
    app = WsApp(service, monochrome=False, onboarding=False, no_animation=True)

    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.press("enter", "/")
        assert not app.narrow_detail_open
        assert app.interaction_mode is InteractionMode.SEARCH
        assert app.has_class("searching")
        await pilot.press("escape", "enter", "f")
        await pilot.pause()
        assert isinstance(app.screen, FilterScreen)
        assert not app.narrow_detail_open
        await pilot.press("escape")
        await pilot.pause()
        assert app.interaction_mode is InteractionMode.NORMAL
        assert not app.narrow_detail_open
        assert app.query_one("#session-pane").display


@pytest.mark.asyncio
async def test_narrow_stopped_detail_opens_manage_and_restores_detail(
    service: SessionService,
) -> None:
    name = create_managed(service, "stopped-narrow", Tool.SHELL)
    service.stop_session(name)
    app = WsApp(service, monochrome=False, onboarding=False, no_animation=True)

    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.press("enter")
        assert app.narrow_detail_open
        assert "Enter Manage" in str(app.query_one("#action-bar", Static).content)
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, ManageSessionScreen)
        await pilot.press("escape")
        await pilot.pause()
        assert app.narrow_detail_open


@pytest.mark.asyncio
async def test_narrow_detail_closes_on_identity_loss_and_wide_resize(
    service: SessionService,
    fake_backend: FakeBackend,
) -> None:
    name = create_managed(service, "resize-detail", Tool.SHELL)
    app = WsApp(service, monochrome=False, onboarding=False, no_animation=True)

    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.press("enter")
        assert app.narrow_detail_open
        await pilot.resize_terminal(100, 30)
        await pilot.pause()
        assert not app.narrow_detail_open
        assert app.query_one("#session-pane").display
        assert app.query_one("#detail-pane").display
        await pilot.resize_terminal(80, 24)
        await pilot.pause()
        assert not app.narrow_detail_open
        assert app.query_one("#session-pane").display
        assert not app.query_one("#detail-pane").display

        await pilot.press("enter")
        assert app.narrow_detail_open
        fake_backend.sessions[name] = fake_backend.sessions[name].model_copy(
            update={"session_id": "$replacement"}
        )
        app.refresh_sessions()
        await pilot.pause()
        assert not app.narrow_detail_open
        assert app.query_one("#session-pane").display


@pytest.mark.asyncio
async def test_logs_follow_pause_manual_refresh_and_restore_dashboard_timer(
    service: SessionService,
    fake_backend: FakeBackend,
) -> None:
    name = create_managed(service, "live-logs", Tool.SHELL)
    fake_backend.previews[name] = "first line\nsecond line"
    app = WsApp(service, monochrome=False, onboarding=False, no_animation=True)
    async with app.run_test(size=(120, 35)) as pilot:
        dashboard_timer = app._dashboard_refresh_timer
        assert dashboard_timer is not None and dashboard_timer._active.is_set()

        await pilot.press("l")
        assert isinstance(app.screen, LogScreen)
        screen = app.screen
        await wait_for_log_refresh(pilot, screen)
        assert screen.output_source is OutputSource.PANE
        assert screen.follow_output
        assert "second line" in screen.query_one("#log-output", TextArea).text
        assert not dashboard_timer._active.is_set()

        await pilot.press("f")
        assert not screen.follow_output
        fake_backend.previews[name] = "updated while paused"
        screen._poll_if_following()
        await pilot.pause()
        assert "second line" in screen.rendered_output

        await pilot.press("r")
        await wait_for_log_refresh(pilot, screen)
        assert screen.rendered_output == "updated while paused"
        assert not screen.follow_output

        await pilot.press("escape")
        await pilot.pause()
        assert app.screen is app.screen_stack[0]
        assert dashboard_timer._active.is_set()
        assert app.interaction_mode is InteractionMode.NORMAL


@pytest.mark.asyncio
async def test_logs_switch_between_live_and_saved_sources(
    service: SessionService,
    fake_backend: FakeBackend,
) -> None:
    created = service.create(
        CreateRequest(
            name="source-switch",
            tool=Tool.SHELL,
            cwd=Path("/tmp"),
            logging_enabled=True,
        )
    )
    record = service.store.load(created.name)
    assert record is not None
    path = service.paths.logs_dir / f"{record.record_id}.log"
    path.write_text("saved history\n", encoding="utf-8")
    fake_backend.previews[created.name] = "live output"
    app = WsApp(service, monochrome=False, onboarding=False, no_animation=True)

    async with app.run_test(size=(120, 35)) as pilot:
        await pilot.press("l")
        assert isinstance(app.screen, LogScreen)
        screen = app.screen
        await wait_for_log_refresh(pilot, screen)
        assert screen.output_source is OutputSource.PANE
        assert screen.rendered_output == "live output"
        assert not screen.query_one("#log-source-saved", Button).disabled

        await pilot.click("#log-source-saved")
        await wait_for_log_refresh(pilot, screen)
        assert screen.output_source is OutputSource.SAVED
        assert screen.rendered_output == "saved history\n"
        assert screen.query_one("#log-source-saved", Button).has_class("active")


@pytest.mark.asyncio
async def test_logs_saved_source_tails_incrementally_for_logged_sessions(
    service: SessionService,
    fake_backend: FakeBackend,
) -> None:
    created = service.create(
        CreateRequest(
            name="tail-append",
            tool=Tool.SHELL,
            cwd=Path("/tmp"),
            logging_enabled=True,
        )
    )
    record = service.store.load(created.name)
    assert record is not None
    path = service.paths.logs_dir / f"{record.record_id}.log"
    path.write_text("first line\n", encoding="utf-8")
    app = WsApp(service, monochrome=False, onboarding=False, no_animation=True)

    async with app.run_test(size=(120, 35)) as pilot:
        await pilot.press("l")
        screen = app.screen
        assert isinstance(screen, LogScreen)
        await wait_for_log_refresh(pilot, screen)
        await pilot.click("#log-source-saved")
        await wait_for_log_refresh(pilot, screen)
        assert screen._tailing
        assert screen.rendered_output == "first line\n"
        assert "live tail" in str(screen.query_one("#log-status").render())

        with path.open("a", encoding="utf-8") as handle:
            handle.write("second line\n")
        screen.action_refresh()
        await wait_for_log_refresh(pilot, screen)
        assert screen.rendered_output == "first line\nsecond line\n"


@pytest.mark.asyncio
async def test_logs_saved_source_falls_back_to_snapshot_without_logging(
    service: SessionService,
    fake_backend: FakeBackend,
) -> None:
    created = service.create(
        CreateRequest(
            name="tail-fallback",
            tool=Tool.SHELL,
            cwd=Path("/tmp"),
            logging_enabled=False,
        )
    )
    # A saved log can still exist from before logging was disabled; the saved
    # source stays selectable, it just can't be tailed without logging_enabled.
    record = service.store.load(created.name)
    assert record is not None
    service.paths.logs_dir.mkdir(parents=True, exist_ok=True)
    path = service.paths.logs_dir / f"{record.record_id}.log"
    path.write_text("legacy output\n", encoding="utf-8")
    fake_backend.previews[created.name] = "live output"
    app = WsApp(service, monochrome=False, onboarding=False, no_animation=True)

    async with app.run_test(size=(120, 35)) as pilot:
        await pilot.press("l")
        screen = app.screen
        assert isinstance(screen, LogScreen)
        await wait_for_log_refresh(pilot, screen)
        await pilot.click("#log-source-saved")
        await wait_for_log_refresh(pilot, screen)
        assert not screen._tailing
        assert screen.rendered_output == "legacy output"
        assert "snapshot" in str(screen.query_one("#log-status").render())


@pytest.mark.asyncio
async def test_logs_tail_resyncs_after_rotation(
    service: SessionService,
    fake_backend: FakeBackend,
) -> None:
    created = service.create(
        CreateRequest(
            name="tail-rotate-ui",
            tool=Tool.SHELL,
            cwd=Path("/tmp"),
            logging_enabled=True,
        )
    )
    record = service.store.load(created.name)
    assert record is not None
    path = service.paths.logs_dir / f"{record.record_id}.log"
    path.write_text("before rotation\n", encoding="utf-8")
    app = WsApp(service, monochrome=False, onboarding=False, no_animation=True)

    async with app.run_test(size=(120, 35)) as pilot:
        await pilot.press("l")
        screen = app.screen
        assert isinstance(screen, LogScreen)
        await wait_for_log_refresh(pilot, screen)
        await pilot.click("#log-source-saved")
        await wait_for_log_refresh(pilot, screen)
        assert screen.rendered_output == "before rotation\n"

        path.write_text("after rotation\n", encoding="utf-8")
        screen.action_refresh()
        await wait_for_log_refresh(pilot, screen)
        assert screen.rendered_output == "after rotation"


@pytest.mark.asyncio
async def test_logs_find_navigation_pauses_follow_and_copy_uses_selection(
    service: SessionService,
    fake_backend: FakeBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    name = create_managed(service, "find-logs", Tool.SHELL)
    fake_backend.previews[name] = "alpha one\nbeta\nalpha two"
    copied: list[str] = []
    app = WsApp(service, monochrome=False, onboarding=False, no_animation=True)
    monkeypatch.setattr(app, "copy_to_clipboard", copied.append)

    async with app.run_test(size=(120, 35)) as pilot:
        await pilot.press("l")
        assert isinstance(app.screen, LogScreen)
        screen = app.screen
        await wait_for_log_refresh(pilot, screen)

        await pilot.press("/", *"alpha")
        await pilot.pause()
        assert screen.finding
        assert not screen.follow_output
        assert len(screen.matches) == 2
        assert screen.match_index == 0
        assert screen.query_one("#log-output", TextArea).selected_text == "alpha"

        search = screen.query_one("#log-find-input", Input)
        search.value = "missing"
        await pilot.pause()
        assert screen.matches == []
        assert "No matches" in str(screen.query_one("#log-find-count", Static).content)
        await pilot.press("ctrl+u")
        assert search.value == ""
        assert "Type to find" in str(screen.query_one("#log-find-count", Static).content)
        search.value = "alpha"
        await pilot.pause()

        await pilot.press("enter")
        assert screen.match_index == 1
        await pilot.press("shift+enter")
        assert screen.match_index == 0
        await pilot.press("escape")
        assert not screen.finding
        assert not screen.follow_output

        await pilot.press("c")
        assert copied == ["alpha"]
        screen.query_one("#log-output", TextArea).move_cursor((0, 0))
        await pilot.press("c")
        assert copied[-1] == screen.rendered_output


@pytest.mark.asyncio
async def test_logs_surface_warning_refresh_error_and_identity_guard(
    service: SessionService,
    fake_backend: FakeBackend,
) -> None:
    name = create_managed(service, "warning-logs", Tool.CODEX)
    fake_backend.previews[name] = (
        "Warning: Codex usage limit reached\nRetry available: tomorrow at 10:00"
    )
    app = WsApp(service, monochrome=False, onboarding=False, no_animation=True)
    async with app.run_test(size=(120, 35)) as pilot:
        await pilot.press("l")
        assert isinstance(app.screen, LogScreen)
        screen = app.screen
        await wait_for_log_refresh(pilot, screen)
        assert screen.has_class("has-log-alert")
        assert "usage limit reached" in str(screen.query_one("#log-alert", Static).content)

        fake_backend.sessions[name] = fake_backend.sessions[name].model_copy(
            update={"session_id": "$replacement"}
        )
        await pilot.press("r")
        for _ in range(80):
            if screen.error_message:
                break
            await pilot.pause(0.05)
        assert screen.error_message
        assert not screen.follow_output
        assert screen.has_class("has-log-error")

        screen.action_attach()
        assert app.return_value is None


@pytest.mark.asyncio
async def test_logs_retry_time_refresh_guards_and_stale_result(
    service: SessionService,
    fake_backend: FakeBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    name = create_managed(service, "retry-logs", Tool.SHELL)
    fake_backend.previews[name] = "Recovered output"
    original_logs = service.logs
    should_fail = True

    def flaky_logs(session_name: str, *, source: OutputSource | None = None) -> SessionDetails:
        if should_fail:
            raise TmuxError("tmux socket unavailable")
        return original_logs(session_name, source=source)

    monkeypatch.setattr(service, "logs", flaky_logs)
    app = WsApp(service, monochrome=False, onboarding=False, no_animation=True)
    async with app.run_test(size=(120, 35)) as pilot:
        await pilot.press("l")
        assert isinstance(app.screen, LogScreen)
        screen = app.screen
        for _ in range(80):
            if screen.error_message:
                break
            await pilot.pause(0.05)
        assert screen.error_message == "tmux socket unavailable"
        assert not screen.refreshing
        assert "Output unavailable" in screen.query_one("#log-output", TextArea).placeholder
        assert "Attach unavailable" in str(screen.query_one("#log-action-bar", Static).content)

        should_fail = False
        await pilot.press("r")
        await wait_for_log_refresh(pilot, screen)
        assert screen.rendered_output == "Recovered output"
        assert screen.error_message == ""

        await pilot.press("t")
        assert "Captured" in str(screen.query_one("#log-status", Static).content)

        details = original_logs(name, source=OutputSource.PANE)
        screen.rendered_output = "Keep newer output"
        screen._finish_refresh(screen._refresh_generation - 1, details, "")
        assert screen.rendered_output == "Keep newer output"

        screen.refreshing = True
        generation = screen._refresh_generation
        screen.action_refresh()
        assert screen._refresh_generation == generation
        screen.refreshing = False


@pytest.mark.asyncio
async def test_logs_restore_viewport_for_each_source(
    service: SessionService,
    fake_backend: FakeBackend,
) -> None:
    created = service.create(
        CreateRequest(
            name="viewport-logs",
            tool=Tool.SHELL,
            cwd=Path("/tmp"),
            logging_enabled=True,
        )
    )
    record = service.store.load(created.name)
    assert record is not None
    path = service.paths.logs_dir / f"{record.record_id}.log"
    path.write_text("\n".join(f"saved line {index}" for index in range(20)), encoding="utf-8")
    fake_backend.previews[created.name] = "\n".join(f"live line {index}" for index in range(20))
    app = WsApp(service, monochrome=False, onboarding=False, no_animation=True)

    async with app.run_test(size=(100, 24)) as pilot:
        await pilot.press("l")
        assert isinstance(app.screen, LogScreen)
        screen = app.screen
        await wait_for_log_refresh(pilot, screen)
        await pilot.press("f")
        area = screen.query_one("#log-output", TextArea)
        area.move_cursor((6, 0))
        area.move_cursor((6, 4), select=True)
        live_selection = area.selection

        await pilot.click("#log-source-saved")
        await wait_for_log_refresh(pilot, screen)
        area.move_cursor((3, 0))
        area.move_cursor((3, 5), select=True)
        saved_selection = area.selection

        await pilot.click("#log-source-pane")
        await wait_for_log_refresh(pilot, screen)
        await pilot.pause()
        assert area.selection == live_selection

        await pilot.click("#log-source-saved")
        await wait_for_log_refresh(pilot, screen)
        await pilot.pause()
        assert area.selection == saved_selection


@pytest.mark.asyncio
async def test_logs_resize_and_stopped_session_disable_attach(
    service: SessionService,
) -> None:
    name = create_managed(service, "stopped-view", Tool.SHELL)
    service.stop_session(name)
    app = WsApp(service, monochrome=False, onboarding=False, no_animation=True)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.press("l")
        assert isinstance(app.screen, LogScreen)
        screen = app.screen
        await wait_for_log_refresh(pilot, screen)
        assert screen.output_source is OutputSource.SAVED
        assert screen.session.runtime is RuntimeState.STOPPED
        assert screen.query_one("#log-source-pane", Button).disabled
        screen.action_attach()
        assert app.return_value is None

        await pilot.resize_terminal(80, 24)
        await pilot.pause()
        assert screen.has_class("log-narrow")
        await pilot.resize_terminal(50, 18)
        await pilot.pause()
        assert screen.has_class("log-narrow")
        assert not screen.has_class("log-too-small")
        await pilot.resize_terminal(35, 12)
        await pilot.pause()
        assert screen.has_class("log-too-small")
        assert screen.query_one("#log-small-terminal", Static).display


@pytest.mark.asyncio
async def test_zero_search_results_clear_actionable_selection(service: SessionService) -> None:
    create_managed(service, "first", Tool.CLAUDE)
    app = WsApp(service, monochrome=False)
    async with app.run_test(size=(120, 36)) as pilot:
        await pilot.press("/")
        app.query_one("#search", Input).value = "no-match"
        await pilot.pause()
        assert app.visible_sessions == []
        assert app.selected_name is None
        assert "No matches" in str(app.query_one("#identity", Static).content)


@pytest.mark.asyncio
async def test_empty_inventory_keeps_toolbar_actions_but_no_session_action(
    service: SessionService,
) -> None:
    app = WsApp(service, monochrome=False, onboarding=False)
    async with app.run_test(size=(120, 36)) as pilot:
        await pilot.pause()
        assert app.selected_name is None
        assert app.visible_sessions == []
        assert app.query_one("#sessions", OptionList).option_count == 0
        overview = str(app.query_one("#overview", Static).content)
        assert "Why empty:" in overview
        assert "Primary action:" in overview
        assert app.query_one("#toolbar-create", Button).display
        assert app.query_one("#toolbar-filter", Button).display
        app.action_more_actions()
        assert app.screen is app.screen_stack[0]


@pytest.mark.asyncio
async def test_refresh_clears_removed_or_reused_tmux_identity(
    service: SessionService,
    fake_backend: FakeBackend,
) -> None:
    name = create_managed(service, "first", Tool.CLAUDE)
    app = WsApp(service, monochrome=False)
    async with app.run_test(size=(120, 36)) as pilot:
        assert app.selected_name == name
        fake_backend.sessions[name] = fake_backend.sessions[name].model_copy(
            update={"session_id": "$replacement"}
        )
        app.refresh_sessions()
        await pilot.pause()
        assert app.selected_name is None
        assert app.visible_sessions == []


@pytest.mark.asyncio
async def test_create_dialog_is_keyboard_accessible(service: SessionService) -> None:
    app = WsApp(service, monochrome=False, onboarding=False)
    async with app.run_test(size=(100, 30)) as pilot:
        base_screen = app.screen
        await pilot.press("c")
        assert isinstance(app.screen, CreateSessionScreen)
        await pilot.press("escape")
        assert app.screen is base_screen


@pytest.mark.asyncio
async def test_delete_requires_manage_and_exact_confirmation(
    service: SessionService,
    fake_backend: FakeBackend,
) -> None:
    name = create_managed(service, "delete-me", Tool.SHELL)
    app = WsApp(service, monochrome=False)
    async with app.run_test(size=(120, 36)) as pilot:
        await pilot.press("d")
        assert isinstance(app.screen, MoreActionsScreen)
        assert app.focused is app.screen.query_one("#manage-actions", OptionList)

        await pilot.press("d")
        await pilot.pause()
        assert isinstance(app.screen, DeleteSessionScreen)
        assert app.focused is app.screen.query_one("#delete-cancel", Button)
        await pilot.click("#delete-confirm")
        await pilot.press(*name)
        await pilot.click("#delete-submit")
        await pilot.pause()
        assert not fake_backend.session_exists(name)


@pytest.mark.asyncio
async def test_refresh_preserves_selection_filter_and_list_scroll(
    service: SessionService,
) -> None:
    for index in range(40):
        create_managed(service, f"session-{index:02d}", Tool.SHELL)
    app = WsApp(service, monochrome=False)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        options = app.query_one("#sessions", OptionList)
        target = app.visible_sessions[20]
        options.highlighted = options.get_option_index(
            session_option_id(target.name, target.session_id)
        )
        app.filter_query = "session"
        options.scroll_to(y=20, animate=False, force=True)
        await pilot.pause()
        assert app.selected_name == target.name
        before_scroll = options.scroll_offset.y
        app.refresh_sessions()
        await pilot.pause()
        assert app.selected_name == target.name
        assert app.filter_query == "session"
        assert options.scroll_offset.y == before_scroll


@pytest.mark.asyncio
async def test_sessions_from_different_backends_can_share_a_tmux_id(
    service: SessionService,
    fake_backend: FakeBackend,
) -> None:
    """Tool backends can both report IDs such as ``$1``."""
    claude = fake_backend.add("claude-shared-id", session_id="$1", command="claude")
    codex = fake_backend.add("codex-shared-id", session_id="$1", command="codex")
    app = WsApp(service, monochrome=False, onboarding=False)
    app.show_unmanaged = True

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        options = app.query_one("#sessions", OptionList)
        claude_id = session_option_id(claude.name, claude.session_id)
        codex_id = session_option_id(codex.name, codex.session_id)
        assert claude_id != codex_id
        assert options.get_option(claude_id)
        assert options.get_option(codex_id)
        assert app._option_sessions[claude_id].name == claude.name
        assert app._option_sessions[codex_id].name == codex.name


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("size", "layout"),
    [
        ((160, 45), "wide"),
        ((120, 35), "wide"),
        ((100, 30), "medium"),
        ((80, 24), "narrow"),
        ((79, 24), "very-narrow"),
        ((40, 15), "very-narrow"),
        ((39, 15), "too-small"),
        ((40, 14), "too-small"),
    ],
)
async def test_responsive_layout_modes(
    service: SessionService,
    size: tuple[int, int],
    layout: str,
) -> None:
    app = WsApp(service, monochrome=False)
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        assert app.has_class(layout)
        if layout == "very-narrow":
            assert app.has_class("narrow")
        if layout == "too-small":
            fallback = str(app.query_one("#small-terminal", Static).content)
            assert "Minimum: 40x15" in fallback
            assert "ws list" in fallback
            assert "ws --classic" in fallback


@pytest.mark.asyncio
async def test_ascii_mode_uses_text_separators_and_navigation(
    service: SessionService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    create_managed(service, "ascii", Tool.SHELL)
    monkeypatch.setenv("WS_ASCII", "1")
    app = WsApp(service, monochrome=True, hostname="ascii-host")
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        footer = str(app.query_one("#action-bar", Static).content)
        assert "Up/Down/jk Nav" in footer
        options = app.query_one("#sessions", OptionList)
        session = app.visible_sessions[0]
        option = options.get_option(session_option_id(session.name, session.session_id))
        assert " · " not in str(option.prompt)


def test_grouping_is_exclusive_and_prioritized(service: SessionService) -> None:
    name = create_managed(service, "grouped", Tool.CLAUDE)
    session = service.get(name)
    now = datetime.now(UTC)
    session = session.model_copy(
        update={
            "runtime": RuntimeState.ATTACHED,
            "pinned": True,
            "input_state": InputState.REQUIRED,
            "last_active_at": now,
        }
    )
    assert session_group(session, now=now) == "Needs Input"
    session = session.model_copy(update={"input_state": InputState.NONE})
    assert session_group(session, now=now) == "Pinned"
    session = session.model_copy(update={"pinned": False})
    assert session_group(session, now=now) == "Attached"
    session = session.model_copy(update={"runtime": RuntimeState.DETACHED})
    assert session_group(session, now=now) == "Detached"
    session = session.model_copy(update={"last_active_at": now - timedelta(days=2)})
    assert session_group(session, now=now) == "Detached"


def test_counted_grouping_modes_keep_pins_as_ordering_not_a_group(service: SessionService) -> None:
    attached_name = create_managed(service, "attached", Tool.CLAUDE)
    input_name = create_managed(service, "input", Tool.CODEX)
    stopped_name = create_managed(service, "stopped", Tool.HERMES)
    attached = service.get(attached_name).model_copy(update={"runtime": RuntimeState.ATTACHED})
    needs_input = service.organize(input_name, input_state=InputState.REQUIRED)
    stopped = service.get(stopped_name).model_copy(update={"runtime": RuntimeState.STOPPED})

    groups = build_session_groups(
        [attached, needs_input, stopped],
        grouping="attention",
        notices=lambda session: detect_activity(session, ""),
    )
    assert [(label, len(items)) for label, items in groups] == [
        ("ATTACHED", 1),
        ("BLOCKED", 1),
        ("WARNINGS", 1),
    ]


@pytest.mark.asyncio
async def test_grouping_and_density_persist_between_dashboard_launches(
    service: SessionService,
) -> None:
    create_managed(service, "persisted-view", Tool.SHELL)
    app = WsApp(service, monochrome=False, onboarding=False, no_animation=True)
    async with app.run_test(size=(120, 35)) as pilot:
        await pilot.pause()
        await pilot.press("g", "z")
        assert app.grouping == "runtime"
        assert app.density == "compact"
        assert app.has_class("compact-density")

    restored = WsApp(service, monochrome=False, onboarding=False, no_animation=True)
    assert restored.grouping == "runtime"
    assert restored.density == "compact"


@pytest.mark.asyncio
async def test_compact_density_renders_single_line_session_rows(service: SessionService) -> None:
    create_managed(service, "compact-row", Tool.CLAUDE)
    app = WsApp(service, monochrome=False, onboarding=False, no_animation=True)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.press("z")
        await pilot.pause()
        option = app.query_one("#sessions", OptionList).get_option_at_index(1)
        assert "\n" not in str(option.prompt)


@pytest.mark.asyncio
async def test_header_hides_hostname_unless_explicitly_configured(
    service: SessionService,
) -> None:
    create_managed(service, "header", Tool.SHELL)
    app = WsApp(service, hostname="private-host-name", onboarding=False, no_animation=True)
    async with app.run_test(size=(160, 45)) as pilot:
        await pilot.pause()
        assert "private-host-name" not in str(app.query_one("#app-header", Static).content)

    service.config = service.config.model_copy(
        update={
            "interface": InterfaceConfig(environment_display="label", environment_label="Home Lab")
        }
    )
    labelled = WsApp(service, hostname="private-host-name", onboarding=False, no_animation=True)
    async with labelled.run_test(size=(160, 45)) as pilot:
        await pilot.pause()
        header = str(labelled.query_one("#app-header", Static).content)
        assert "Home Lab" in header
        assert "private-host-name" not in header


def test_path_and_activity_display_helpers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path("/home/test")))
    assert display_path(Path("/home/test")) == "~"
    assert display_path(Path("/home/test/project")) == "~/project"
    now = datetime(2026, 7, 20, tzinfo=UTC)
    assert relative_activity(now - timedelta(seconds=5), now=now) == "<1m"
    assert relative_activity(now - timedelta(minutes=2), now=now) == "2m"
    assert relative_activity(now - timedelta(hours=3), now=now) == "3h"


def test_raw_legacy_task_is_humanized() -> None:
    assert (
        humanize_task("codex task: https-example-internal-sample-release (ubuntu)")
        == "Work on example internal sample release"
    )


def test_condensed_session_label_shortens_long_slug(service: SessionService) -> None:
    long_name = create_managed(
        service, "https-astrology-fernandofamily-com-en-pancha-pakshi", Tool.CODEX
    )
    session = service.get(long_name)
    assert condensed_session_label(session) == "codex · astrology / pancha-pakshi"


def test_usage_limit_detection_is_structured(service: SessionService) -> None:
    name = create_managed(service, "limited", Tool.CODEX)
    session = service.get(name)
    notice = detect_activity(
        session,
        "Warning: Codex usage limit reached\nRetry available: 23 Jul 2026, 10:46 AM",
    )
    assert notice.level == "warning"
    assert notice.title == "Codex usage limit reached"
    assert notice.detail == "Retry available: 23 Jul 2026, 10:46 AM"
    assert notice.agent_state is AgentState.PAUSED


def test_claude_session_limit_wording_is_detected(service: SessionService) -> None:
    name = create_managed(service, "limited", Tool.CLAUDE)
    notice = detect_activity(
        service.get(name),
        "You've hit your session limit\nAvailable again at 10:10 AM",
    )
    assert notice.warning
    assert notice.title == "Claude Code session limit reached"
    assert notice.detail == "Retry available: 10:10 AM"


@pytest.mark.asyncio
async def test_create_form_validates_duplicates_and_directory_inline(
    service: SessionService,
    tmp_path: Path,
) -> None:
    create_managed(service, "existing", Tool.CLAUDE)
    project = tmp_path / "detected-project"
    project.mkdir()
    (project / ".git").mkdir()
    app = WsApp(service, monochrome=False, onboarding=False)
    async with app.run_test(size=(120, 35)) as pilot:
        await pilot.press("c")
        assert isinstance(app.screen, CreateSessionScreen)
        submit = app.screen.query_one("#create-submit", Button)
        assert submit.disabled

        app.screen.query_one("#create-name", Input).value = "existing"
        app.screen.query_one("#create-cwd", Input).value = str(project)
        await wait_for_create_validation(pilot, app.screen)
        assert submit.disabled
        assert "already exists" in str(app.screen.query_one("#create-name-status", Static).content)

        app.screen.query_one("#create-name", Input).value = "new-work"
        await wait_for_create_validation(pilot, app.screen)
        assert not submit.disabled
        assert app.screen.query_one("#create-project", Input).value == "detected-project"

        app.screen.query_one("#create-cwd", Input).value = str(project / "missing")
        await wait_for_create_validation(pilot, app.screen)
        assert submit.disabled
        assert app.screen.query_one("#create-name", Input).value == "new-work"


@pytest.mark.asyncio
async def test_create_form_uses_latest_normalized_name_value(
    service: SessionService,
    tmp_path: Path,
) -> None:
    app = WsApp(service, monochrome=False, onboarding=False)
    async with app.run_test(size=(120, 35)) as pilot:
        await pilot.press("c")
        form = app.screen
        form.query_one("#create-name", Input).value = "_"
        form.query_one("#create-name", Input).value = "api-refactor"
        form.query_one("#create-cwd", Input).value = str(tmp_path)
        await wait_for_create_validation(pilot, form)
        status = str(form.query_one("#create-name-status", Static).content)
        assert "Available as claude-api-refactor" in status
        assert not form.query_one("#create-submit", Button).disabled
        command_status = str(form.query_one("#create-command-status", Static).content)
        assert "Executable ready:" in command_status
        progress = str(form.query_one("#create-progress", Static).content)
        assert "Progress: 3/3 required checks complete" in progress
        summary = str(form.query_one("#create-summary", Static).content)
        assert "Ensure `claude` is authenticated before attaching." in summary


@pytest.mark.asyncio
async def test_create_form_submit_focuses_first_invalid_field(service: SessionService) -> None:
    app = WsApp(service, monochrome=False, onboarding=False)
    async with app.run_test(size=(120, 35)) as pilot:
        await pilot.press("c")
        form = app.screen
        assert isinstance(form, CreateSessionScreen)
        await pilot.click("#create-submit")
        await pilot.pause()
        assert app.focused is form.query_one("#create-name", Input)


@pytest.mark.asyncio
async def test_create_form_draft_persists_after_cancel(
    service: SessionService,
    tmp_path: Path,
) -> None:
    app = WsApp(service, monochrome=False, onboarding=False)
    async with app.run_test(size=(120, 35)) as pilot:
        await pilot.press("c")
        form = app.screen
        assert isinstance(form, CreateSessionScreen)
        form.query_one("#create-name", Input).value = "draft-session"
        form.query_one("#create-cwd", Input).value = str(tmp_path)
        form.query_one("#create-note", TextArea).text = "draft note"
        await pilot.press("escape")
        await pilot.pause()
        assert app.interaction_mode is InteractionMode.NORMAL

        await pilot.press("c")
        await pilot.pause()
        form = app.screen
        assert isinstance(form, CreateSessionScreen)
        assert form.query_one("#create-name", Input).value == "draft-session"
        assert form.query_one("#create-note", TextArea).text == "draft note"


@pytest.mark.asyncio
async def test_create_form_sections_and_validation_copy_are_visible(
    service: SessionService,
    tmp_path: Path,
) -> None:
    app = WsApp(service, monochrome=False, onboarding=False)
    async with app.run_test(size=(120, 35)) as pilot:
        await pilot.press("c")
        form = app.screen
        assert isinstance(form, CreateSessionScreen)
        form.query_one("#create-name", Input).value = "api-refactor"
        form.query_one("#create-cwd", Input).value = str(tmp_path)
        await wait_for_create_validation(pilot, form)
        validation = str(form.query_one("#create-validation", Static).content)
        assert "Session ID available as:" in validation
        assert "Directory exists:" in validation
        assert "Executable ready:" in validation


@pytest.mark.asyncio
async def test_create_form_suggests_recent_tool_for_detected_project(
    service: SessionService,
    tmp_path: Path,
) -> None:
    existing = create_managed(service, "project-history", Tool.CODEX)
    service.organize(existing, project="backend-api")
    project = tmp_path / "backend-api"
    project.mkdir()
    (project / ".git").mkdir()

    app = WsApp(service, monochrome=False, onboarding=False)
    async with app.run_test(size=(120, 35)) as pilot:
        await pilot.press("c")
        form = app.screen
        form.query_one("#create-name", Input).value = "new-api-work"
        form.query_one("#create-cwd", Input).value = str(project)
        await wait_for_create_validation(pilot, form)
        assert form.query_one("#create-tool", Select).value == Tool.CODEX.value


@pytest.mark.asyncio
async def test_create_form_does_not_override_manual_tool_choice_for_project_suggestion(
    service: SessionService,
    tmp_path: Path,
) -> None:
    existing = create_managed(service, "project-history", Tool.CODEX)
    service.organize(existing, project="backend-api")
    project = tmp_path / "backend-api"
    project.mkdir()
    (project / ".git").mkdir()

    app = WsApp(service, monochrome=False, onboarding=False)
    async with app.run_test(size=(120, 35)) as pilot:
        await pilot.press("c")
        form = app.screen
        form.query_one("#create-tool", Select).value = Tool.SHELL.value
        form.query_one("#create-name", Input).value = "new-api-work"
        form.query_one("#create-cwd", Input).value = str(project)
        await wait_for_create_validation(pilot, form)
        assert form.query_one("#create-tool", Select).value == Tool.SHELL.value


@pytest.mark.asyncio
async def test_create_defaults_to_first_enabled_tool(service: SessionService) -> None:
    service.config = service.config.model_copy(
        update={
            "tools": {
                **service.config.tools,
                Tool.CLAUDE: service.config.tools[Tool.CLAUDE].model_copy(
                    update={"enabled": False}
                ),
            }
        }
    )
    app = WsApp(service, monochrome=False, onboarding=False)
    async with app.run_test(size=(120, 35)) as pilot:
        await pilot.press("c")
        await pilot.pause()
        assert isinstance(app.screen, CreateSessionScreen)
        assert app.screen.query_one("#create-tool", Select).value == Tool.COPILOT.value


@pytest.mark.asyncio
async def test_create_form_session_id_max_length_matches_model_limit(
    service: SessionService,
) -> None:
    app = WsApp(service, monochrome=False, onboarding=False)
    async with app.run_test(size=(120, 35)) as pilot:
        await pilot.press("c")
        await pilot.pause()
        assert isinstance(app.screen, CreateSessionScreen)
        assert app.screen.query_one("#create-name", Input).max_length == 80


@pytest.mark.asyncio
async def test_create_form_tool_options_only_show_enabled_profiles(
    service: SessionService,
) -> None:
    service.config = service.config.model_copy(
        update={
            "tools": {
                **service.config.tools,
                Tool.CLAUDE: service.config.tools[Tool.CLAUDE].model_copy(
                    update={"enabled": False}
                ),
                Tool.HERMES: service.config.tools[Tool.HERMES].model_copy(
                    update={"enabled": False}
                ),
            }
        }
    )
    app = WsApp(service, monochrome=False, onboarding=False)
    async with app.run_test(size=(120, 35)) as pilot:
        await pilot.press("c")
        await pilot.pause()
        assert isinstance(app.screen, CreateSessionScreen)
        options = [
            str(option[0]) for option in app.screen.query_one("#create-tool", Select)._options
        ]
        assert "Claude" not in options
        assert "Hermes" not in options
        assert "Copilot" in options
        assert "Codex" in options
        assert "Shell" in options


@pytest.mark.asyncio
async def test_create_form_with_disabled_default_tool_falls_back_to_first_enabled(
    service: SessionService,
) -> None:
    service.config = service.config.model_copy(
        update={
            "tools": {
                **service.config.tools,
                Tool.CLAUDE: service.config.tools[Tool.CLAUDE].model_copy(
                    update={"enabled": False}
                ),
            }
        }
    )
    app = WsApp(service, monochrome=False, onboarding=False)
    screen = CreateSessionScreen(
        Path("/tmp"),
        service=service,
        default_tool=Tool.CLAUDE,
    )
    async with app.run_test(size=(120, 35)) as pilot:
        app.push_screen(screen)
        await pilot.pause()
        assert screen.query_one("#create-tool", Select).value == Tool.COPILOT.value


@pytest.mark.asyncio
async def test_create_form_without_service_shows_all_tool_profiles(
    service: SessionService,
) -> None:
    app = WsApp(service, monochrome=False, onboarding=False)
    screen = CreateSessionScreen(
        Path("/tmp"),
        service=None,
        default_tool=Tool.HERMES,
    )
    async with app.run_test(size=(120, 35)) as pilot:
        app.push_screen(screen)
        await pilot.pause()
        options = [str(option[0]) for option in screen.query_one("#create-tool", Select)._options]
        assert options == [TOOL_LABELS[tool] for tool in Tool]
        assert screen.query_one("#create-tool", Select).value == Tool.HERMES.value


@pytest.mark.asyncio
async def test_create_form_shows_command_error_when_tool_command_missing(
    service: SessionService,
    tmp_path: Path,
) -> None:
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
    app = WsApp(service, monochrome=False, onboarding=False)
    async with app.run_test(size=(120, 35)) as pilot:
        await pilot.press("c")
        form = app.screen
        form.query_one("#create-name", Input).value = "api-refactor"
        form.query_one("#create-cwd", Input).value = str(tmp_path)
        await wait_for_create_validation(pilot, form)
        command_status = str(form.query_one("#create-command-status", Static).content)
        assert "command not found" in command_status
        assert form.query_one("#create-submit", Button).disabled


@pytest.mark.asyncio
async def test_action_bar_shows_default_create_tool(service: SessionService) -> None:
    service.config = service.config.model_copy(
        update={
            "tools": {
                **service.config.tools,
                Tool.CLAUDE: service.config.tools[Tool.CLAUDE].model_copy(
                    update={"enabled": False}
                ),
            }
        }
    )
    app = WsApp(service, monochrome=False, onboarding=False)
    async with app.run_test(size=(120, 35)) as pilot:
        await pilot.pause()
        footer = str(app.query_one("#action-bar", Static).content)
        assert "Create(Copilot)" in footer


@pytest.mark.asyncio
async def test_create_suspends_and_restores_search_mode(service: SessionService) -> None:
    create_managed(service, "first", Tool.CLAUDE)
    create_managed(service, "second", Tool.CODEX)
    app = WsApp(service, monochrome=False, onboarding=False)
    async with app.run_test(size=(120, 35)) as pilot:
        await pilot.press("/")
        await pilot.press("c", "o", "d", "e", "x")
        visible_before = [session.name for session in app.visible_sessions]
        app.action_create()
        await pilot.pause()

        assert isinstance(app.screen, CreateSessionScreen)
        assert app.interaction_mode is InteractionMode.FORM
        assert not app.has_class("searching")
        assert app.filter_query == "codex"
        assert app.query_one("#search", Input).value == "codex"
        assert app.query_one("#search-mode").display is False
        assert app.query_one("#action-bar").display is False
        assert app.screen.query_one("#create-form-help").display is True
        assert len(app.screen.query("#create-form-help")) == 1

        await pilot.click("#create-cancel")
        await pilot.pause()
        assert app.interaction_mode is InteractionMode.SEARCH
        assert app.has_class("searching")
        assert app.filter_query == "codex"
        assert [session.name for session in app.visible_sessions] == visible_before
        assert app.focused is app.query_one("#search", Input)
        assert app.query_one("#action-bar").display is True


@pytest.mark.asyncio
async def test_filter_suspends_and_restores_search_mode(service: SessionService) -> None:
    create_managed(service, "first", Tool.CLAUDE)
    create_managed(service, "second", Tool.CODEX)
    app = WsApp(service, monochrome=False, onboarding=False)
    async with app.run_test(size=(120, 35)) as pilot:
        await pilot.press("/", "c", "o", "d", "e", "x")
        visible_before = [session.name for session in app.visible_sessions]
        app.action_filter()
        await pilot.pause()

        assert isinstance(app.screen, FilterScreen)
        assert app.interaction_mode is InteractionMode.FILTER
        assert not app.has_class("searching")
        assert app.query_one("#search-mode").display is False
        assert app.query_one("#action-bar").display is False
        assert len(app.screen.query(".mode-help")) == 1

        await pilot.press("escape")
        await pilot.pause()
        assert app.interaction_mode is InteractionMode.SEARCH
        assert app.has_class("searching")
        assert app.filter_query == "codex"
        assert [session.name for session in app.visible_sessions] == visible_before
        assert app.focused is app.query_one("#search", Input)


@pytest.mark.asyncio
async def test_palette_has_exclusive_mode_and_restores_search(service: SessionService) -> None:
    create_managed(service, "first", Tool.CLAUDE)
    app = WsApp(service, monochrome=False, onboarding=False)
    async with app.run_test(size=(120, 35)) as pilot:
        await pilot.press("/", "f", "i", "r", "s", "t")
        app.action_command_palette()
        await pilot.pause()

        assert isinstance(app.screen, CommandPalette)
        assert app.interaction_mode is InteractionMode.PALETTE
        assert not app.has_class("searching")
        assert app.query_one("#search-mode").display is False
        assert app.query_one("#action-bar").display is False

        await pilot.press("escape")
        await pilot.pause()
        assert app.interaction_mode is InteractionMode.SEARCH
        assert app.has_class("searching")
        assert app.filter_query == "first"
        assert app.focused is app.query_one("#search", Input)


@pytest.mark.asyncio
async def test_palette_restores_dashboard_before_dispatching_command(
    service: SessionService,
) -> None:
    create_managed(service, "first", Tool.CLAUDE)
    app = WsApp(service, monochrome=False, onboarding=False)
    async with app.run_test(size=(120, 35)) as pilot:
        await pilot.press("p")
        app.screen.query_one(Input).value = "filter sessions"
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()

        assert isinstance(app.screen, FilterScreen)
        assert app.interaction_mode is InteractionMode.FILTER
        assert app._mode_context is not None
        assert app._mode_context.mode is InteractionMode.NORMAL


@pytest.mark.asyncio
async def test_palette_can_open_create_form_from_saved_preset(service: SessionService) -> None:
    service.save_preset(
        "backend-dev",
        tool=Tool.SHELL,
        cwd=Path("/tmp"),
        project="api",
        tags=["backend"],
        logging_enabled=False,
    )
    app = WsApp(service, monochrome=False, onboarding=False)
    async with app.run_test(size=(120, 35)) as pilot:
        await pilot.press("p")
        app.screen.query_one(Input).value = "preset: backend-dev"
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()

        assert isinstance(app.screen, CreateSessionScreen)
        assert app.interaction_mode is InteractionMode.FORM
        screen = app.screen
        assert screen.query_one("#create-tool", Select).value == Tool.SHELL.value
        assert screen.query_one("#create-cwd", Input).value == display_path(Path("/tmp"))
        assert screen.query_one("#create-project", Input).value == "api"
        assert screen.query_one("#create-tags", Input).value == "backend"
        assert screen.query_one("#create-logging", Switch).value is False


@pytest.mark.asyncio
async def test_palette_create_from_preset_rejects_disabled_preset_tool(
    service: SessionService,
) -> None:
    service.save_preset("backend-dev", tool=Tool.HERMES, cwd=Path("/tmp"))
    service.config = service.config.model_copy(
        update={
            "tools": {
                **service.config.tools,
                Tool.HERMES: service.config.tools[Tool.HERMES].model_copy(
                    update={"enabled": False}
                ),
            }
        }
    )
    app = WsApp(service, monochrome=False, onboarding=False)
    notifications: list[tuple[str, dict[str, object]]] = []
    app.notify = lambda message, **kwargs: notifications.append((message, kwargs))
    async with app.run_test(size=(120, 35)) as pilot:
        await pilot.press("p")
        app.screen.query_one(Input).value = "preset: backend-dev"
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert notifications
        assert "preset is disabled in config.toml" in notifications[-1][0]
        assert not isinstance(app.screen, CreateSessionScreen)


@pytest.mark.asyncio
async def test_palette_alias_can_open_interface_controls(service: SessionService) -> None:
    create_managed(service, "first", Tool.SHELL)
    app = WsApp(service, monochrome=False, onboarding=False)
    async with app.run_test(size=(120, 35)) as pilot:
        await pilot.press("p")
        app.screen.query_one(Input).value = "settings"
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, InterfaceControlsScreen)


@pytest.mark.asyncio
async def test_palette_typo_tolerance_can_find_filter_command(service: SessionService) -> None:
    create_managed(service, "first", Tool.SHELL)
    app = WsApp(service, monochrome=False, onboarding=False)
    async with app.run_test(size=(120, 35)) as pilot:
        await pilot.press("p")
        app.screen.query_one(Input).value = "filter sesions"
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, FilterScreen)


@pytest.mark.asyncio
async def test_create_screen_shows_mode_breadcrumb(service: SessionService) -> None:
    app = WsApp(service, monochrome=False, onboarding=False)
    async with app.run_test(size=(120, 35)) as pilot:
        await pilot.press("c")
        await pilot.pause()
        assert isinstance(app.screen, CreateSessionScreen)
        breadcrumb = str(app.screen.query_one(".modal-breadcrumb", Static).content)
        assert "Dashboard -> Create" in breadcrumb


@pytest.mark.asyncio
async def test_preset_launcher_warns_when_no_presets_saved(service: SessionService) -> None:
    app = WsApp(service, monochrome=False, onboarding=False)
    notifications: list[tuple[str, dict[str, object]]] = []
    app.notify = lambda message, **kwargs: notifications.append((message, kwargs))
    async with app.run_test(size=(120, 35)) as pilot:
        await pilot.press("o")
        await pilot.pause()
        assert notifications
        assert "No presets saved yet." in notifications[-1][0]


@pytest.mark.asyncio
async def test_preset_launcher_blank_option_opens_default_create_form(
    service: SessionService,
) -> None:
    service.save_preset("backend-dev", tool=Tool.SHELL, cwd=Path("/tmp"))
    app = WsApp(service, monochrome=False, onboarding=False)
    async with app.run_test(size=(120, 35)) as pilot:
        await pilot.press("o")
        await pilot.pause()
        options = app.screen.query_one("#preset-launcher-options", OptionList)
        assert options.option_count >= 1
        await pilot.press("enter")
        await pilot.pause()

        assert isinstance(app.screen, CreateSessionScreen)
        assert app.screen.query_one("#create-tool", Select).value == Tool.CLAUDE.value


@pytest.mark.asyncio
async def test_preset_launcher_rejects_disabled_preset_tool(service: SessionService) -> None:
    service.save_preset("backend-dev", tool=Tool.HERMES, cwd=Path("/tmp"))
    service.config = service.config.model_copy(
        update={
            "tools": {
                **service.config.tools,
                Tool.HERMES: service.config.tools[Tool.HERMES].model_copy(
                    update={"enabled": False}
                ),
            }
        }
    )
    app = WsApp(service, monochrome=False, onboarding=False)
    notifications: list[tuple[str, dict[str, object]]] = []
    app.notify = lambda message, **kwargs: notifications.append((message, kwargs))
    async with app.run_test(size=(120, 35)) as pilot:
        await pilot.press("o")
        await pilot.pause()
        options = app.screen.query_one("#preset-launcher-options", OptionList)
        options.highlighted = 1
        await pilot.press("enter")
        await pilot.pause()
        assert notifications
        assert "preset is disabled in config.toml" in notifications[-1][0]
        assert not isinstance(app.screen, CreateSessionScreen)


@pytest.mark.asyncio
async def test_global_shortcuts_do_not_stack_over_filter(service: SessionService) -> None:
    create_managed(service, "protected", Tool.SHELL)
    app = WsApp(service, monochrome=False, onboarding=False)
    async with app.run_test(size=(120, 35)) as pilot:
        await pilot.press("f")
        screen = app.screen
        stack_depth = len(app.screen_stack)

        await pilot.press("c", "/", "d", "p")
        await pilot.pause()
        assert app.screen is screen
        assert len(app.screen_stack) == stack_depth


@pytest.mark.asyncio
async def test_macro_warnings_to_logs_opens_first_warning_session(
    service: SessionService,
) -> None:
    warning_name = create_managed(service, "warning", Tool.CLAUDE)
    service.stop_session(warning_name)
    app = WsApp(service, monochrome=False, onboarding=False)
    async with app.run_test(size=(120, 35)) as pilot:
        await pilot.press("0")
        await pilot.pause()
        assert isinstance(app.screen, LogScreen)


@pytest.mark.asyncio
async def test_triage_wizard_guides_warning_to_logs(
    service: SessionService,
) -> None:
    warning_name = create_managed(service, "triage-warning", Tool.CLAUDE)
    service.stop_session(warning_name)
    app = WsApp(service, monochrome=False, onboarding=False)
    async with app.run_test(size=(120, 35)) as pilot:
        await pilot.press("W")
        await pilot.pause()
        assert app.screen.query_one("#triage-step", Static).content
        app.screen.action_next_step()
        await pilot.pause()
        app.screen.action_next_step()
        await pilot.pause()
        app.screen.action_next_step()
        await pilot.pause()
        assert isinstance(app.screen, LogScreen)


@pytest.mark.asyncio
async def test_undo_shortcut_reverts_pin_change(
    service: SessionService,
) -> None:
    name = create_managed(service, "undo-pin", Tool.SHELL)
    app = WsApp(service, monochrome=False, onboarding=False)
    async with app.run_test(size=(120, 35)) as pilot:
        await pilot.pause()
        assert not service.get(name).pinned
        app.action_toggle_pin()
        await pilot.pause()
        assert service.get(name).pinned
        await pilot.press("U")
        await pilot.pause()
        assert not service.get(name).pinned


@pytest.mark.asyncio
async def test_project_visual_profile_restores_per_project_preferences(
    service: SessionService,
) -> None:
    api_name = create_managed(service, "profile-api", Tool.SHELL)
    web_name = create_managed(service, "profile-web", Tool.SHELL)
    service.organize(api_name, project="api")
    service.organize(web_name, project="web")
    app = WsApp(service, monochrome=False, onboarding=False)
    async with app.run_test(size=(120, 35)) as pilot:
        await pilot.pause()
        api_option = next(
            option_id
            for option_id, session in app._option_sessions.items()
            if session.name == api_name
        )
        web_option = next(
            option_id
            for option_id, session in app._option_sessions.items()
            if session.name == web_name
        )
        app._select_option(api_option)
        app.action_cycle_text_scale()
        await pilot.pause()
        assert app.text_scale == "readable"

        app._select_option(web_option)
        app.action_toggle_density()
        await pilot.pause()
        assert app.density == "compact"

        app._select_option(api_option)
        await pilot.pause()
        assert app.text_scale == "readable"
        assert app.density == "comfortable"


@pytest.mark.asyncio
async def test_grouped_notifications_increment_repeat_counter(service: SessionService) -> None:
    create_managed(service, "notify", Tool.SHELL)
    app = WsApp(service, monochrome=False, onboarding=False)
    notifications: list[tuple[str, dict[str, object]]] = []
    app.notify = lambda message, **kwargs: notifications.append((message, kwargs))
    async with app.run_test(size=(120, 35)) as _pilot:
        app.action_quick_filter_all()
        app.action_quick_filter_all()
        assert notifications
        assert notifications[-1][0].endswith("(x2)")


@pytest.mark.asyncio
async def test_theme_recommendation_appears_on_limited_terminal(
    service: SessionService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TERM", "linux")
    app = WsApp(service, monochrome=False, onboarding=False)
    notifications: list[tuple[str, dict[str, object]]] = []
    app.notify = lambda message, **kwargs: notifications.append((message, kwargs))
    async with app.run_test(size=(120, 35)) as _pilot:
        assert any("Recommended: enable high contrast" in message for message, _ in notifications)


@pytest.mark.asyncio
async def test_basic_create_form_fits_without_scrolling(
    service: SessionService,
    tmp_path: Path,
) -> None:
    app = WsApp(service, monochrome=False, onboarding=False, default_cwd=tmp_path)
    async with app.run_test(size=(120, 35)) as pilot:
        await pilot.press("c")
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, CreateSessionScreen)
        assert not isinstance(screen.query_one("#create-basic"), VerticalScroll)
        assert screen.query_one("#create-advanced").display is False
        assert screen.query_one(".dialog-actions").region.bottom <= 35


@pytest.mark.asyncio
async def test_advanced_options_preserve_values_and_focus(
    service: SessionService,
) -> None:
    app = WsApp(service, monochrome=False, onboarding=False)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.press("c")
        await pilot.click("#create-advanced-toggle")
        tags = app.screen.query_one("#create-tags", Input)
        tags.value = "backend, urgent"
        tags.focus()
        await pilot.pause()

        app.screen.action_cancel()
        await pilot.pause()
        assert isinstance(app.screen, CreateSessionScreen)
        assert app.screen.query_one("#create-advanced").display is False
        assert tags.value == "backend, urgent"

        await pilot.press("enter")
        await pilot.pause()
        assert app.screen.query_one("#create-advanced").display is True
        assert app.focused is tags
        assert tags.value == "backend, urgent"


@pytest.mark.asyncio
async def test_create_form_preset_select_populates_fields(
    service: SessionService,
    tmp_path: Path,
) -> None:
    service.save_preset(
        "backend-dev",
        tool=Tool.SHELL,
        cwd=tmp_path,
        project="api",
        tags=["backend"],
        logging_enabled=False,
    )
    app = WsApp(service, monochrome=False, onboarding=False)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.press("c")
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, CreateSessionScreen)
        screen.query_one("#create-preset", Select).value = "backend-dev"
        await pilot.pause()

        assert screen.query_one("#create-tool", Select).value == Tool.SHELL.value
        assert screen.query_one("#create-cwd", Input).value == display_path(tmp_path)
        assert screen.query_one("#create-project", Input).value == "api"
        assert screen.query_one("#create-tags", Input).value == "backend"
        assert screen.query_one("#create-logging", Switch).value is False
        assert screen.query_one("#create-preset", Select).value == Select.NULL


@pytest.mark.asyncio
async def test_manage_clone_action_prefills_create_form_from_session(
    service: SessionService,
) -> None:
    name = create_managed(service, "original", Tool.SHELL)
    service.organize(name, tags=["backend"], project="api")
    service.set_logging(name, False)
    app = WsApp(service, monochrome=False, onboarding=False, no_animation=True)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.press("d", "c")
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, CreateSessionScreen)
        assert screen.query_one("#create-tool", Select).value == Tool.SHELL.value
        assert screen.query_one("#create-cwd", Input).value == display_path(Path("/tmp"))
        assert screen.query_one("#create-project", Input).value == "api"
        assert screen.query_one("#create-tags", Input).value == "backend"
        assert screen.query_one("#create-logging", Switch).value is False


@pytest.mark.asyncio
async def test_manage_clone_action_creates_matching_session_on_submit(
    service: SessionService,
) -> None:
    name = create_managed(service, "original", Tool.SHELL)
    service.organize(name, tags=["backend"], project="api")
    app = WsApp(service, monochrome=False, onboarding=False, no_animation=True)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.press("d", "c")
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, CreateSessionScreen)
        screen.query_one("#create-name", Input).value = "cloned-session"
        await wait_for_create_validation(pilot, screen)
        await pilot.press("ctrl+enter")
        await pilot.pause()

    cloned = service.get("cloned-session")
    assert cloned.tool is Tool.SHELL
    assert cloned.project == "api"
    assert cloned.tags == ["backend"]


@pytest.mark.asyncio
async def test_create_form_has_no_preset_select_when_no_presets_saved(
    service: SessionService,
) -> None:
    app = WsApp(service, monochrome=False, onboarding=False)
    async with app.run_test(size=(120, 35)) as pilot:
        await pilot.press("c")
        await pilot.pause()
        assert not app.screen.query("#create-preset")


@pytest.mark.asyncio
async def test_home_directory_does_not_become_ubuntu_project(
    service: SessionService,
) -> None:
    app = WsApp(service, monochrome=False, onboarding=False, default_cwd=Path.home())
    async with app.run_test(size=(120, 35)) as pilot:
        await pilot.press("c")
        app.screen.query_one("#create-name", Input).value = "home-task"
        await wait_for_create_validation(pilot, app.screen)
        assert app.screen.query_one("#create-project", Input).value == ""
        assert "Project not detected" in str(
            app.screen.query_one("#create-project-status", Static).content
        )
        assert app.screen.query_one("#create-home-project", Button).display is True


@pytest.mark.asyncio
async def test_recent_directory_selection_updates_working_directory(
    service: SessionService,
    tmp_path: Path,
) -> None:
    recent = tmp_path / "recent-project"
    recent.mkdir()
    service.create(CreateRequest(name="recent", tool=Tool.SHELL, cwd=recent))
    app = WsApp(service, monochrome=False, onboarding=False, default_cwd=tmp_path)
    async with app.run_test(size=(120, 35)) as pilot:
        await pilot.press("c")
        app.screen.query_one("#create-recent-dir", Select).value = str(recent)
        await pilot.pause()
        assert app.screen.query_one("#create-cwd", Input).value == display_path(recent)


@pytest.mark.asyncio
async def test_ctrl_enter_requires_current_validation_and_updates_grouped_list(
    service: SessionService,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = WsApp(service, monochrome=False, onboarding=False, default_cwd=tmp_path)
    async with app.run_test(size=(120, 35)) as pilot:
        base_screen = app.screen
        await pilot.press("c")
        await pilot.press("ctrl+enter")
        assert isinstance(app.screen, CreateSessionScreen)

        app.screen.query_one("#create-name", Input).value = "api_refactor"
        await wait_for_create_validation(pilot, app.screen)
        assert not app.screen.query_one("#create-submit", Button).disabled
        render_calls = 0
        original_render = app._render_options

        def tracked_render() -> None:
            nonlocal render_calls
            render_calls += 1
            original_render()

        monkeypatch.setattr(app, "_render_options", tracked_render)
        await pilot.press("ctrl+enter")
        await pilot.pause()

        assert app.screen is base_screen
        assert render_calls == 1
        assert app.selected_name == "claude-api-refactor"
        assert service.get("claude-api-refactor").display_name == "api_refactor"
        created = service.get("claude-api-refactor")
        option = app.query_one("#sessions", OptionList).get_option(
            session_option_id(created.name, created.session_id)
        )
        assert option is not None


@pytest.mark.asyncio
async def test_multiline_task_enter_does_not_submit(
    service: SessionService,
) -> None:
    app = WsApp(service, monochrome=False, onboarding=False)
    async with app.run_test(size=(120, 35)) as pilot:
        await pilot.press("c")
        task = app.screen.query_one("#create-note", TextArea)
        task.focus()
        await pilot.press("a", "enter", "b")
        assert isinstance(app.screen, CreateSessionScreen)
        assert task.text == "a\nb"


@pytest.mark.asyncio
async def test_prefix_can_be_disabled_without_changing_the_backend(
    service: SessionService,
    tmp_path: Path,
) -> None:
    app = WsApp(service, monochrome=False, onboarding=False, default_cwd=tmp_path)
    async with app.run_test(size=(120, 35)) as pilot:
        await pilot.press("c")
        app.screen.query_one("#create-name", Input).value = "api_refactor"
        app.screen.query_one("#create-prefix", Switch).value = False
        await wait_for_create_validation(pilot, app.screen)
        assert "Available as api-refactor" in str(
            app.screen.query_one("#create-name-status", Static).content
        )


@pytest.mark.asyncio
async def test_failed_startup_is_actionable_and_leaves_no_metadata(
    service: SessionService,
    fake_backend: FakeBackend,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    notifications: list[tuple[str, dict[str, object]]] = []

    def fail_start(*args: object, **kwargs: object) -> None:
        raise TmuxError("isolated startup failed")

    monkeypatch.setattr(fake_backend, "create_session", fail_start)
    app = WsApp(service, monochrome=False, onboarding=False, default_cwd=tmp_path)
    monkeypatch.setattr(
        app,
        "notify",
        lambda message, **kwargs: notifications.append((message, kwargs)),
    )
    async with app.run_test(size=(120, 35)) as pilot:
        await pilot.press("c")
        app.screen.query_one("#create-name", Input).value = "will-fail"
        await wait_for_create_validation(pilot, app.screen)
        await pilot.press("ctrl+enter")
        await pilot.pause()

        assert service.store.load("claude-will-fail") is None
        assert not fake_backend.session_exists("claude-will-fail")
        assert isinstance(app.screen, CreateFailureScreen)
        assert app.interaction_mode is InteractionMode.CONFIRMATION
        assert app.screen.query_one("#create-failure-remove", Button).disabled
        assert app.focused is app.screen.query_one("#create-failure-close", Button)
        assert notifications
        message, options = notifications[-1]
        assert "Retry" in message
        assert options["title"] == "Session startup failed"
        assert options["timeout"] == 0
        await pilot.click("#create-failure-close")
        await pilot.pause()
        assert app.interaction_mode is InteractionMode.NORMAL


@pytest.mark.asyncio
async def test_usage_limit_updates_header_row_activity_and_agent_state(
    service: SessionService,
    fake_backend: FakeBackend,
) -> None:
    name = create_managed(service, "limited", Tool.CLAUDE)
    fake_backend.previews[name] = "You've hit your session limit\nAvailable again at 10:10 AM"
    app = WsApp(service, monochrome=False, onboarding=False)
    async with app.run_test(size=(120, 35)) as pilot:
        await pilot.pause()
        assert "1 warning" in str(app.query_one("#app-header", Static).content)
        assert "Claude Code session limit reached" in str(
            app.query_one("#activity", Static).content
        )
        assert "Agent         Paused" in str(app.query_one("#runtime-status", Static).content)
        session = app.sessions[0]
        option = app.query_one("#sessions", OptionList).get_option(
            session_option_id(session.name, session.session_id)
        )
        assert "!" in str(option.prompt)
        summary = str(app.query_one("#recent-output", Static).content)
        assert "tmux session remains active" in summary
        assert "You've hit" not in summary
        app._set_output_mode("raw")
        await pilot.pause()
        assert "You've hit" in str(app.query_one("#recent-output", Static).content)


@pytest.mark.asyncio
async def test_attention_scan_finds_unselected_warning_and_restores_temporary_view(
    service: SessionService,
    fake_backend: FakeBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    limited = create_managed(service, "a-limited", Tool.CODEX)
    selected = create_managed(service, "z-selected", Tool.CLAUDE)
    service.organize(selected, pinned=True)
    fake_backend.previews[limited] = (
        "Warning: Codex usage limit reached\nRetry available: tomorrow at 10:00"
    )
    fake_backend.previews[selected] = "Ready"
    notifications: list[tuple[str, dict[str, object]]] = []
    app = WsApp(service, monochrome=False, onboarding=False, no_animation=True)
    monkeypatch.setattr(
        app,
        "notify",
        lambda message, **kwargs: notifications.append((message, kwargs)),
    )

    async with app.run_test(size=(120, 35)) as pilot:
        await wait_for_detail_refresh(pilot, app)
        await wait_for_attention_scan(pilot, app)
        assert app.selected_name == selected
        assert "1 warning" in str(app.query_one("#app-header", Static).content)
        limited_view = next(session for session in app.sessions if session.name == limited)
        prompt = (
            app.query_one("#sessions", OptionList)
            .get_option(session_option_id(limited_view.name, limited_view.session_id))
            .prompt
        )
        assert "!" in str(prompt)
        assert notifications == []

        app.filter_query = "z-selected"
        app.filters = FilterState(tool=Tool.CLAUDE)
        app.query_one("#search", Input).value = app.filter_query
        app._render_options()
        await pilot.pause()
        assert [session.name for session in app.visible_sessions] == [selected]

        app.action_attention()
        await pilot.pause()
        assert app.has_class("attention-view")
        assert [session.name for session in app.visible_sessions] == [limited]
        assert "Attention" in str(app.query_one("#app-header", Static).content)
        assert "Esc Back" in str(app.query_one("#action-bar", Static).content)

        await pilot.press("escape")
        await pilot.pause()
        assert not app.has_class("attention-view")
        assert app.filter_query == "z-selected"
        assert app.filters == FilterState(tool=Tool.CLAUDE)
        assert app.selected_name == selected
        assert [session.name for session in app.visible_sessions] == [selected]

        app.action_attention()
        await pilot.press("/")
        assert app._attention_context is None
        assert app.has_class("searching")
        assert app.query_one("#search", Input).value == "z-selected"
        await pilot.press("escape")

        app.action_attention()
        await pilot.press("f")
        await pilot.pause()
        assert app._attention_context is None
        assert isinstance(app.screen, FilterScreen)
        assert app.screen.query_one("#filter-tool", Select).value == Tool.CLAUDE.value
        await pilot.press("escape")
        await pilot.pause()
        assert app.filter_query == "z-selected"
        assert app.filters == FilterState(tool=Tool.CLAUDE)


@pytest.mark.asyncio
async def test_attention_scan_marks_selected_warning_session(
    service: SessionService,
    fake_backend: FakeBackend,
) -> None:
    selected = create_managed(service, "selected-warning", Tool.CODEX)
    fake_backend.previews[selected] = (
        "Warning: Codex usage limit reached\nRetry available: tomorrow at 10:00"
    )
    app = WsApp(service, monochrome=False, onboarding=False, no_animation=True)

    async with app.run_test(size=(120, 35)) as pilot:
        await wait_for_detail_refresh(pilot, app)
        await wait_for_attention_scan(pilot, app)
        assert app.selected_name == selected
        assert "1 warning" in str(app.query_one("#app-header", Static).content)
        session = app.sessions[0]
        option = app.query_one("#sessions", OptionList).get_option(
            session_option_id(session.name, session.session_id)
        )
        assert "!" in str(option.prompt)
        assert "Codex usage limit reached" in str(app.query_one("#activity", Static).content)


@pytest.mark.asyncio
async def test_apply_filter_preset_updates_grouping_density_and_quick_filter(
    service: SessionService,
) -> None:
    service.save_filter_preset(
        FilterPreset(
            name="runtime-compact",
            query="",
            quick_filter="detached",
            grouping="runtime",
            density="compact",
        )
    )
    app = WsApp(service, monochrome=False, onboarding=False, no_animation=True)
    async with app.run_test(size=(120, 35)) as pilot:
        await pilot.pause()
        app.action_apply_filter_preset("runtime-compact")
        await pilot.pause()
        assert app.quick_filter == "detached"
        assert app.grouping == "runtime"
        assert app.density == "compact"
        assert app.has_class("compact-density")


@pytest.mark.asyncio
async def test_bulk_selection_mark_all_clear_and_invert_visible(
    service: SessionService,
) -> None:
    create_managed(service, "one", Tool.SHELL)
    create_managed(service, "two", Tool.CLAUDE)
    app = WsApp(service, monochrome=False, onboarding=False, no_animation=True)
    async with app.run_test(size=(120, 35)) as pilot:
        await pilot.pause()
        app.action_bulk_select_visible()
        assert len(app._bulk_selected) == len(app.visible_sessions)
        app.action_bulk_clear_selection()
        assert app._bulk_selected == set()
        app.action_bulk_invert_visible()
        assert len(app._bulk_selected) == len(app.visible_sessions)
        app.action_bulk_invert_visible()
        assert app._bulk_selected == set()


@pytest.mark.asyncio
async def test_attention_scan_notifies_once_after_baseline_and_clears_resolution(
    service: SessionService,
    fake_backend: FakeBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = create_managed(service, "a-candidate", Tool.CODEX)
    selected = create_managed(service, "z-selected", Tool.CLAUDE)
    service.organize(selected, pinned=True)
    fake_backend.previews[candidate] = "Ready"
    fake_backend.previews[selected] = "Ready"
    notifications: list[tuple[str, dict[str, object]]] = []
    app = WsApp(service, monochrome=False, onboarding=False, no_animation=True)
    monkeypatch.setattr(
        app,
        "notify",
        lambda message, **kwargs: notifications.append((message, kwargs)),
    )

    async with app.run_test(size=(120, 35)) as pilot:
        await wait_for_detail_refresh(pilot, app)
        await wait_for_attention_scan(pilot, app)
        assert app._attention_baseline_established
        assert notifications == []

        fake_backend.previews[candidate] = "You've hit your session limit"
        app.refresh_sessions()
        await wait_for_attention_scan(pilot, app)
        warning_notifications = [
            item for item in notifications if item[1].get("title") == "New session warning"
        ]
        assert len(warning_notifications) == 1
        assert "a-candidate" in warning_notifications[0][0]

        app.refresh_sessions()
        await wait_for_attention_scan(pilot, app)
        assert (
            len([item for item in notifications if item[1].get("title") == "New session warning"])
            == 1
        )

        fake_backend.previews[candidate] = "Recovered and ready"
        app.refresh_sessions()
        await wait_for_attention_scan(pilot, app)
        assert "No warnings" in str(app.query_one("#app-header", Static).content)
        app.action_attention()
        await pilot.pause()
        assert app.visible_sessions == []
        assert "No sessions need attention" in str(app.query_one("#identity", Static).content)


@pytest.mark.asyncio
async def test_activity_sparkline_appears_only_after_enough_samples_at_wide_width(
    service: SessionService,
    fake_backend: FakeBackend,
) -> None:
    other = create_managed(service, "a-quiet-session", Tool.CODEX)
    watched = create_managed(service, "z-watched-session", Tool.CLAUDE)
    service.organize(watched, pinned=True)
    fake_backend.previews[other] = "idle"
    fake_backend.previews[watched] = "starting up"
    app = WsApp(service, monochrome=False, onboarding=False, no_animation=True)

    async with app.run_test(size=(120, 35)) as pilot:
        assert app.has_class("wide")
        await wait_for_detail_refresh(pilot, app)
        await wait_for_attention_scan(pilot, app)

        watched_view = next(item for item in app.sessions if item.name == watched)
        identity = (watched_view.name, watched_view.session_id)

        for count in range(ACTIVITY_SPARK_MIN_SAMPLES + 1):
            fake_backend.previews[other] = f"idle output changing round {count}"
            app.refresh_sessions()
            await wait_for_attention_scan(pilot, app)

        other_view = next(item for item in app.sessions if item.name == other)
        history = app._activity_history.get((other_view.name, other_view.session_id))
        assert history is not None
        assert len(history) >= ACTIVITY_SPARK_MIN_SAMPLES
        prompt = str(
            app.query_one("#sessions", OptionList)
            .get_option(session_option_id(other_view.name, other_view.session_id))
            .prompt
        )
        assert any(glyph in prompt for glyph in "▁▂▃▄▅▆▇█")

        # The pinned/selected session's own detail-refresh path also records
        # samples, but it never changed output, so its history should exist
        # (from the identical no-op initial fetch) without ever growing past
        # the flat idle state -- this just exercises that code path too.
        assert identity in app._activity_history


@pytest.mark.asyncio
async def test_new_warning_sends_telegram_alert_when_enabled(
    service: SessionService,
    fake_backend: FakeBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = create_managed(service, "a-candidate", Tool.CODEX)
    selected = create_managed(service, "z-selected", Tool.CLAUDE)
    service.organize(selected, pinned=True)
    fake_backend.previews[candidate] = "Ready"
    fake_backend.previews[selected] = "Ready"
    service.config = service.config.model_copy(
        update={
            "notifications": NotificationConfig(
                telegram_enabled=True,
                telegram_bot_token="test-token",  # noqa: S106
                telegram_chat_id="12345",
            )
        }
    )
    sent: list[str] = []
    monkeypatch.setattr(
        "workspace_session_manager.tui.send_telegram",
        lambda config, text: sent.append(text) or True,
    )
    app = WsApp(service, monochrome=False, onboarding=False, no_animation=True)

    async with app.run_test(size=(120, 35)) as pilot:
        await wait_for_detail_refresh(pilot, app)
        await wait_for_attention_scan(pilot, app)
        assert sent == []

        fake_backend.previews[candidate] = "You've hit your session limit"
        app.refresh_sessions()
        await wait_for_attention_scan(pilot, app)
        for _ in range(20):
            if sent:
                break
            await pilot.pause(0.05)
        assert len(sent) == 1
        assert "a-candidate" in sent[0]


@pytest.mark.asyncio
async def test_new_warning_does_not_send_telegram_alert_when_disabled(
    service: SessionService,
    fake_backend: FakeBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = create_managed(service, "a-candidate", Tool.CODEX)
    selected = create_managed(service, "z-selected", Tool.CLAUDE)
    service.organize(selected, pinned=True)
    fake_backend.previews[candidate] = "Ready"
    fake_backend.previews[selected] = "Ready"
    assert not service.config.notifications.telegram_enabled
    from workspace_session_manager.notifier import send_telegram as real_send_telegram

    results: list[bool] = []

    def spy(config: object, text: str) -> bool:
        result = real_send_telegram(config, text)  # type: ignore[arg-type]
        results.append(result)
        return result

    monkeypatch.setattr("workspace_session_manager.tui.send_telegram", spy)
    app = WsApp(service, monochrome=False, onboarding=False, no_animation=True)

    async with app.run_test(size=(120, 35)) as pilot:
        await wait_for_detail_refresh(pilot, app)
        await wait_for_attention_scan(pilot, app)

        fake_backend.previews[candidate] = "You've hit your session limit"
        app.refresh_sessions()
        await wait_for_attention_scan(pilot, app)
        for _ in range(20):
            if results:
                break
            await pilot.pause(0.05)
        assert results == [False]


@pytest.mark.asyncio
async def test_activity_sparkline_hidden_at_narrow_width(
    service: SessionService,
    fake_backend: FakeBackend,
) -> None:
    name = create_managed(service, "narrow-activity", Tool.CODEX)
    fake_backend.previews[name] = "output"
    app = WsApp(service, monochrome=False, onboarding=False, no_animation=True)

    async with app.run_test(size=(80, 24)) as pilot:
        assert not app.has_class("wide")
        await wait_for_detail_refresh(pilot, app)
        session_view = next(item for item in app.sessions if item.name == name)
        assert app._activity_spark_for(session_view) == ""


def test_attention_batch_reserves_priority_and_rotates_detached_sessions(
    service: SessionService,
    fake_backend: FakeBackend,
) -> None:
    service.config = service.config.model_copy(update={"attention_scan_budget": 4})
    attached_names: set[str] = set()
    detached_names: set[str] = set()
    for index in range(4):
        name = create_managed(service, f"attached-{index}", Tool.CLAUDE)
        fake_backend.sessions[name] = fake_backend.sessions[name].model_copy(
            update={"attached_clients": 1}
        )
        attached_names.add(name)
    for index in range(6):
        detached_names.add(create_managed(service, f"detached-{index}", Tool.CODEX))

    app = WsApp(service, monochrome=False, onboarding=False, no_animation=True)
    app.sessions = service.list_sessions()
    now = datetime.now(UTC)
    app._attention_scanned_at = {
        (session.name, session.session_id): now
        for session in app.sessions
        if session.name in attached_names
    }
    seen_detached: set[str] = set()
    for offset in range(3):
        batch = app._attention_batch()
        assert len(batch) == 4
        assert sum(item.session.name in attached_names for item in batch) == 2
        seen_detached.update(
            item.session.name for item in batch if item.session.name in detached_names
        )
        observed = now + timedelta(seconds=offset + 1)
        for item in batch:
            app._attention_scanned_at[(item.session.name, item.session.session_id)] = observed
    assert seen_detached == detached_names


@pytest.mark.asyncio
async def test_attention_scan_error_is_deduplicated_and_recovers(
    service: SessionService,
    fake_backend: FakeBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failing = create_managed(service, "a-failing", Tool.CODEX)
    selected = create_managed(service, "z-selected", Tool.CLAUDE)
    service.organize(selected, pinned=True)
    fake_backend.previews[selected] = "Ready"
    original = service.inspect_snapshot

    def inspect_with_failure(session: SessionView, **kwargs: object) -> SessionDetails:
        if session.name == failing:
            raise TmuxError("capture unavailable")
        return original(session, **kwargs)

    monkeypatch.setattr(service, "inspect_snapshot", inspect_with_failure)
    notifications: list[tuple[str, dict[str, object]]] = []
    app = WsApp(service, monochrome=False, onboarding=False, no_animation=True)
    monkeypatch.setattr(
        app,
        "notify",
        lambda message, **kwargs: notifications.append((message, kwargs)),
    )

    async with app.run_test(size=(120, 35)) as pilot:
        await wait_for_attention_scan(pilot, app)
        assert "capture unavailable" in app._attention_scan_error
        assert (
            len(
                [item for item in notifications if item[1].get("title") == "Attention scan delayed"]
            )
            == 1
        )

        app.refresh_sessions()
        await wait_for_attention_scan(pilot, app)
        assert (
            len(
                [item for item in notifications if item[1].get("title") == "Attention scan delayed"]
            )
            == 1
        )

        monkeypatch.setattr(service, "inspect_snapshot", original)
        fake_backend.previews[failing] = "Recovered"
        app.refresh_sessions()
        await wait_for_attention_scan(pilot, app)
        assert app._attention_scan_error == ""
        assert app._attention_complete()


@pytest.mark.asyncio
async def test_attention_scan_discards_removed_exact_identity(
    service: SessionService,
    fake_backend: FakeBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = create_managed(service, "a-stale", Tool.CODEX)
    selected = create_managed(service, "z-selected", Tool.CLAUDE)
    service.organize(selected, pinned=True)
    fake_backend.previews[candidate] = "You've hit your session limit"
    fake_backend.previews[selected] = "Ready"
    started = Event()
    release = Event()
    original = service.inspect_snapshot

    def delayed_inspect(session: SessionView, **kwargs: object) -> SessionDetails:
        if session.name == candidate:
            started.set()
            release.wait(timeout=5)
        return original(session, **kwargs)

    monkeypatch.setattr(service, "inspect_snapshot", delayed_inspect)
    app = WsApp(service, monochrome=False, onboarding=False, no_animation=True)

    async with app.run_test(size=(120, 35)) as pilot:
        for _ in range(80):
            if started.is_set():
                break
            await pilot.pause(0.05)
        assert started.is_set()
        stale_identity = next(
            (session.name, session.session_id)
            for session in app.sessions
            if session.name == candidate
        )
        fake_backend.sessions[candidate] = fake_backend.sessions[candidate].model_copy(
            update={"session_id": "$replacement"}
        )
        app.refresh_sessions()
        release.set()
        await wait_for_attention_scan(pilot, app)
        assert stale_identity not in app._alerts
        assert stale_identity not in app._attention_scanned_at
        assert all(session.name != candidate for session in app.sessions)


@pytest.mark.asyncio
async def test_diagnostics_is_centered_modal_with_safe_default_details(
    service: SessionService,
) -> None:
    create_managed(service, "diagnostics", Tool.SHELL)
    app = WsApp(service, monochrome=False, onboarding=False)
    async with app.run_test(size=(120, 35)) as pilot:
        app.action_diagnostics()
        await pilot.pause()
        assert isinstance(app.screen, DiagnosticsScreen)
        assert app.focused is app.screen.query_one("#diagnostics-close", Button)
        summary = str(app.screen.query_one("#diagnostics-summary", Static).content)
        assert "passed" in summary and "failed" in summary and "information" in summary
        content = str(app.screen.query_one("#diagnostics-content", Static).content)
        assert str(service.paths.state_dir) not in content
        await pilot.click("#diagnostics-details")
        await pilot.pause()
        assert "~" in str(
            app.screen.query_one("#diagnostics-content", Static).content
        ) or "tmp" in str(app.screen.query_one("#diagnostics-content", Static).content)
        await pilot.press("escape")
        assert app.screen is app.screen_stack[0]


@pytest.mark.asyncio
async def test_slow_diagnostics_shows_progress_then_duration(
    service: SessionService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = service.doctor
    started = Event()
    release = Event()

    def slow_doctor():  # type: ignore[no-untyped-def]
        started.set()
        release.wait(timeout=2)
        return original()

    monkeypatch.setattr(service, "doctor", slow_doctor)
    app = WsApp(service, monochrome=False, onboarding=False, no_animation=True)
    async with app.run_test(size=(120, 35)) as pilot:
        app.action_diagnostics()
        await pilot.pause(0.28)
        assert started.is_set()
        assert app.screen.running
        assert app.screen.query_one("#diagnostics-loading").display
        release.set()
        await pilot.pause(0.1)
        assert not app.screen.running
        assert "Completed in" in str(app.screen.query_one("#diagnostics-meta", Static).content)


@pytest.mark.asyncio
async def test_manage_requires_cancel_focused_confirmation_for_stop(
    service: SessionService,
    fake_backend: FakeBackend,
) -> None:
    name = create_managed(service, "protected", Tool.SHELL)
    app = WsApp(service, monochrome=False, onboarding=False)
    async with app.run_test(size=(120, 35)) as pilot:
        await pilot.press("d")
        assert isinstance(app.screen, ManageSessionScreen)
        assert isinstance(app.screen, MoreActionsScreen)
        assert app.focused is app.screen.query_one("#manage-actions", OptionList)
        await pilot.press("t")
        await wait_for_confirmation(pilot, app)
        assert app.focused is app.screen.query_one("#confirm-cancel", Button)
        await pilot.press("escape")
        await pilot.pause()
        assert fake_backend.session_exists(name)
        assert isinstance(app.screen, ManageSessionScreen)
        assert app.interaction_mode is InteractionMode.MANAGE
        options = app.screen.query_one("#manage-actions", OptionList)
        assert app.focused is options
        assert options.get_option_at_index(options.highlighted or 0).id == (
            "manage-action:stop-session"
        )


@pytest.mark.asyncio
async def test_danger_confirmation_requires_typed_session_name(
    service: SessionService,
) -> None:
    name = create_managed(service, "typed-confirm", Tool.SHELL)
    app = WsApp(service, monochrome=False, onboarding=False)
    async with app.run_test(size=(120, 35)) as pilot:
        await pilot.press("d", "t")
        confirm = await wait_for_confirmation(pilot, app)
        submit = confirm.query_one("#confirm-submit", Button)
        assert submit.disabled
        confirm.query_one("#confirm-typed", Input).value = "wrong-name"
        await pilot.pause()
        assert submit.disabled
        confirm.query_one("#confirm-typed", Input).value = name
        await pilot.pause()
        assert not submit.disabled


@pytest.mark.asyncio
async def test_manage_restart_attach_exits_with_session_target(
    service: SessionService,
    fake_backend: FakeBackend,
) -> None:
    name = create_managed(service, "restartable", Tool.SHELL)
    app = WsApp(service, monochrome=False, onboarding=False)
    async with app.run_test(size=(120, 35)) as pilot:
        await pilot.press("d")
        assert isinstance(app.screen, ManageSessionScreen)
        await pilot.press("y")
        await wait_for_confirmation(pilot, app)
        app.screen.query_one("#confirm-submit", Button).press()
        await pilot.pause()
    assert app.return_value == name


@pytest.mark.asyncio
async def test_manage_confirmation_keeps_original_session_target(
    service: SessionService,
    fake_backend: FakeBackend,
) -> None:
    original_name = create_managed(service, "original", Tool.SHELL)
    other_name = create_managed(service, "other", Tool.SHELL)
    app = WsApp(service, monochrome=False, onboarding=False)
    async with app.run_test(size=(120, 35)) as pilot:
        original = next(session for session in app.sessions if session.name == original_name)
        other = next(session for session in app.sessions if session.name == other_name)
        app.selected_name = original.name
        app.selected_session_id = original.session_id
        app.action_manage()
        await pilot.pause()

        app.selected_name = other.name
        app.selected_session_id = other.session_id
        app.screen.action_choose("stop-session")
        confirm = await wait_for_confirmation(pilot, app)
        confirm.query_one("#confirm-typed", Input).value = original_name
        await pilot.pause()
        confirm.query_one("#confirm-submit", Button).press()
        await pilot.pause()

        assert not fake_backend.session_exists(original_name)
        assert fake_backend.session_exists(other_name)


@pytest.mark.asyncio
async def test_manage_fits_all_categories_at_120x35(service: SessionService) -> None:
    create_managed(service, "managed", Tool.SHELL)
    app = WsApp(service, monochrome=False, onboarding=False, no_animation=True)
    async with app.run_test(size=(120, 35)) as pilot:
        await pilot.press("d")
        screen = await wait_for_manage(pilot, app)
        options = screen.query_one("#manage-actions", OptionList)
        option_ids = {
            options.get_option_at_index(index).id for index in range(options.option_count)
        }

        assert options.option_count == 17
        assert options.max_scroll_y <= 1
        assert {
            "manage-category:general",
            "manage-category:runtime",
            "manage-category:danger",
            "manage-action:identity",
            "manage-action:restart",
            "manage-action:restart-attach",
            "manage-action:delete",
        } <= option_ids
        assert options.get_option_at_index(options.highlighted or 0).id == (
            "manage-action:identity"
        )


@pytest.mark.asyncio
async def test_manage_modal_shows_display_name_full_id_and_shortcuts(
    service: SessionService,
) -> None:
    name = create_managed(
        service, "https-astrology-fernandofamily-com-en-pancha-pakshi", Tool.CODEX
    )
    app = WsApp(service, monochrome=False, onboarding=False, no_animation=True)
    async with app.run_test(size=(120, 35)) as pilot:
        await pilot.press("d")
        screen = await wait_for_manage(pilot, app)
        context = str(screen.query_one(".dialog-context", Static).content)
        assert "Display name" in context
        assert "Full ID" in context
        assert name in context
        option = screen.query_one("#manage-actions", OptionList).get_option(
            "manage-action:identity"
        )
        assert "[e]" in str(option.prompt)


@pytest.mark.asyncio
async def test_manage_find_is_local_and_cancellable(service: SessionService) -> None:
    create_managed(service, "managed", Tool.SHELL)
    app = WsApp(service, monochrome=False, onboarding=False, no_animation=True)
    async with app.run_test(size=(120, 35)) as pilot:
        await pilot.press("d", "/")
        screen = await wait_for_manage(pilot, app)
        search = screen.query_one("#manage-search", Input)
        assert app.interaction_mode is InteractionMode.MANAGE
        assert screen.has_class("finding")
        assert app.focused is search

        await pilot.press(*"leave tmux")
        options = screen.query_one("#manage-actions", OptionList)
        assert options.option_count == 2
        assert options.get_option_at_index(1).id == "manage-action:remove-metadata"

        await pilot.press("escape")
        assert not screen.has_class("finding")
        assert options.option_count == 17
        assert app.focused is options

        await pilot.press("/", *"identity", "enter")
        assert not screen.has_class("finding")
        assert options.option_count == 2
        assert app.interaction_mode is InteractionMode.MANAGE


@pytest.mark.asyncio
async def test_manage_disabled_actions_explain_stopped_state(service: SessionService) -> None:
    name = create_managed(service, "stopped", Tool.SHELL)
    service.stop_session(name)
    app = WsApp(service, monochrome=False, onboarding=False, no_animation=True)
    async with app.run_test(size=(120, 35)) as pilot:
        await pilot.press("d")
        screen = await wait_for_manage(pilot, app)
        options = screen.query_one("#manage-actions", OptionList)
        stop = options.get_option("manage-action:stop-session")
        logging = options.get_option("manage-action:logging")

        assert stop.disabled
        assert logging.disabled
        assert "Unavailable: stopped" in str(stop.prompt)


@pytest.mark.asyncio
async def test_manage_identity_edit_returns_with_filter_and_new_identity(
    service: SessionService,
) -> None:
    original_name = create_managed(service, "original", Tool.SHELL)
    app = WsApp(service, monochrome=False, onboarding=False, no_animation=True)
    async with app.run_test(size=(120, 35)) as pilot:
        await pilot.press("d", "/", *"identity", "enter", "enter")
        assert isinstance(app.screen, IdentityOrganizationScreen)
        identity = app.screen
        identity.query_one("#identity-display-name", Input).value = "Renamed Workflow"
        identity.query_one("#identity-name", Input).value = "renamed session"
        await wait_for_identity_validation(pilot, identity)
        assert not identity.query_one("#identity-submit", Button).disabled

        await pilot.press("ctrl+enter")
        manage = await wait_for_manage(pilot, app)
        assert service.store.load(original_name) is None
        updated = service.get("renamed-session")
        assert updated.display_name == "Renamed Workflow"
        assert manage.state.query == "identity"
        assert manage.query_one("#manage-actions", OptionList).option_count == 2

        await pilot.press("escape")
        await pilot.pause()
        assert app.screen is app.screen_stack[0]
        assert app.selected_name == "renamed-session"


@pytest.mark.asyncio
async def test_manage_task_status_and_pin_stay_in_workflow(service: SessionService) -> None:
    name = create_managed(service, "workflow", Tool.SHELL)
    app = WsApp(service, monochrome=False, onboarding=False, no_animation=True)
    async with app.run_test(size=(120, 35)) as pilot:
        await pilot.press("d", "n")
        assert isinstance(app.screen, NoteScreen)
        app.screen.query_one("#note-value", TextArea).text = "First line\nSecond line"
        await pilot.press("ctrl+enter")
        await wait_for_manage(pilot, app)
        assert service.get(name).note == "First line\nSecond line"

        await pilot.press("s")
        assert isinstance(app.screen, StatusScreen)
        app.screen.query_one("#status-task-state", Select).value = TaskState.BLOCKED.value
        app.screen.query_one("#status-input-state", Select).value = InputState.REQUIRED.value
        await pilot.press("ctrl+enter")
        await wait_for_manage(pilot, app)
        updated = service.get(name)
        assert updated.task_state is TaskState.BLOCKED
        assert updated.input_state is InputState.REQUIRED

        await pilot.press("*")
        manage = await wait_for_manage(pilot, app)
        assert service.get(name).pinned
        pin = manage.query_one("#manage-actions", OptionList).get_option("manage-action:pin")
        assert "Unpin session" in str(pin.prompt)


@pytest.mark.asyncio
async def test_manage_is_full_screen_at_narrow_width(service: SessionService) -> None:
    create_managed(service, "narrow-manage", Tool.SHELL)
    app = WsApp(service, monochrome=False, onboarding=False, no_animation=True)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.press("d")
        screen = await wait_for_manage(pilot, app)
        await pilot.pause()
        assert screen.has_class("narrow-manage")
        assert screen.query_one("#more-dialog").region.size == screen.size


@pytest.mark.asyncio
async def test_filter_dialog_applies_tool_and_warning_filters(service: SessionService) -> None:
    claude = create_managed(service, "decision", Tool.CLAUDE)
    create_managed(service, "other", Tool.CODEX)
    service.organize(claude, input_state=InputState.REQUIRED)
    app = WsApp(service, monochrome=False, onboarding=False)
    async with app.run_test(size=(120, 35)) as pilot:
        await pilot.press("f")
        assert isinstance(app.screen, FilterScreen)
        app.screen.query_one("#filter-tool", Select).value = Tool.CLAUDE.value
        await pilot.click("#filter-warnings")
        await pilot.click("#filter-apply")
        await pilot.pause()
        assert [session.name for session in app.visible_sessions] == [claude]
        assert "Claude Code" in str(app.query_one("#app-header", Static).content)


@pytest.mark.asyncio
async def test_quick_filters_support_active_detached_warning_stopped_and_blocked(
    service: SessionService,
    fake_backend: FakeBackend,
) -> None:
    active = create_managed(service, "active", Tool.CLAUDE)
    detached = create_managed(service, "detached", Tool.CODEX)
    stopped = create_managed(service, "stopped", Tool.SHELL)
    blocked = create_managed(service, "blocked", Tool.HERMES)
    fake_backend.sessions[active] = fake_backend.sessions[active].model_copy(
        update={"attached_clients": 1}
    )
    service.stop_session(stopped)
    service.organize(blocked, state=TaskState.BLOCKED)
    app = WsApp(service, monochrome=False, onboarding=False)
    async with app.run_test(size=(120, 35)) as pilot:
        await pilot.pause()
        await pilot.press("2")
        assert all(item.runtime is RuntimeState.ATTACHED for item in app.visible_sessions)
        await pilot.press("3")
        assert all(item.runtime is RuntimeState.DETACHED for item in app.visible_sessions)
        await pilot.press("5")
        assert all(
            item.runtime in {RuntimeState.STOPPED, RuntimeState.FAILED}
            for item in app.visible_sessions
        )
        await pilot.press("6")
        assert all(item.task_state is TaskState.BLOCKED for item in app.visible_sessions)
        await pilot.press("1")
        names = {item.name for item in app.visible_sessions}
        assert {active, detached, stopped, blocked} <= names


@pytest.mark.asyncio
async def test_filter_dialog_applies_tag_and_project_filters(service: SessionService) -> None:
    backend = create_managed(service, "backend-work", Tool.CLAUDE)
    frontend = create_managed(service, "frontend-work", Tool.CODEX)
    service.organize(backend, tags=["backend"], project="api")
    service.organize(frontend, tags=["frontend"], project="web")
    app = WsApp(service, monochrome=False, onboarding=False)
    async with app.run_test(size=(120, 35)) as pilot:
        await pilot.press("f")
        assert isinstance(app.screen, FilterScreen)
        screen = app.screen
        assert screen.available_tags == ("backend", "frontend")
        assert screen.available_projects == ("api", "web")
        screen.query_one("#filter-tag", Select).value = "backend"
        await pilot.click("#filter-apply")
        await pilot.pause()
        assert [session.name for session in app.visible_sessions] == [backend]

        await pilot.press("f")
        app.screen.query_one("#filter-tag", Select).value = "any"
        app.screen.query_one("#filter-project", Select).value = "web"
        await pilot.click("#filter-apply")
        await pilot.pause()
        assert [session.name for session in app.visible_sessions] == [frontend]


@pytest.mark.asyncio
async def test_onboarding_is_safe_and_recorded(service: SessionService) -> None:
    app = WsApp(service, monochrome=False)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        assert isinstance(app.screen, OnboardingScreen)
        assert app.focused is app.screen.query_one("#onboarding-close", Button)
        await pilot.press("escape")
        await pilot.pause()
        assert service.onboarding_seen()


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [50, 200])
async def test_large_inventories_render_once_per_session(
    service: SessionService,
    count: int,
) -> None:
    for index in range(count):
        create_managed(service, f"load-{index:03d}", Tool.SHELL)
    app = WsApp(service, monochrome=False, onboarding=False)
    async with app.run_test(size=(160, 45)) as pilot:
        await pilot.pause()
        assert len(app.visible_sessions) == count
        assert len(app._option_sessions) == count
        assert len(set(app._option_sessions)) == count


@pytest.mark.asyncio
async def test_terminal_resizing_preserves_selection_and_switches_modes(
    service: SessionService,
) -> None:
    create_managed(service, "resize", Tool.SHELL)
    app = WsApp(service, monochrome=False, onboarding=False)
    async with app.run_test(size=(160, 45)) as pilot:
        selected = app.selected_name
        await pilot.resize_terminal(100, 30)
        await pilot.pause()
        assert app.has_class("medium")
        assert app.selected_name == selected
        await pilot.resize_terminal(80, 24)
        await pilot.pause()
        assert app.has_class("narrow")
        assert app.selected_name == selected
        await pilot.resize_terminal(72, 20)
        await pilot.pause()
        assert app.has_class("very-narrow")
        assert app.selected_name == selected
        await pilot.resize_terminal(35, 12)
        await pilot.pause()
        assert app.has_class("too-small")


@pytest.mark.asyncio
async def test_theme_cycle_covers_every_mode(service: SessionService) -> None:
    create_managed(service, "theme", Tool.SHELL)
    app = WsApp(service, monochrome=False, onboarding=False)
    async with app.run_test(size=(120, 35)) as pilot:
        assert app.ui_theme == "ithaca"
        assert app.theme == "ithaca"
        for expected in THEME_MODES[1:]:
            app.action_cycle_theme()
            await pilot.pause()
            assert app.ui_theme == expected
            assert app.theme == expected
        app.action_cycle_theme()
        await pilot.pause()
        assert app.ui_theme == "ithaca"
        assert app.theme == "ithaca"


@pytest.mark.asyncio
async def test_text_scale_cycle_updates_layout_class(service: SessionService) -> None:
    create_managed(service, "scale", Tool.SHELL)
    app = WsApp(service, monochrome=False, onboarding=False)
    async with app.run_test(size=(120, 35)) as pilot:
        assert app.text_scale == "comfortable"
        assert not app.has_class("text-scale-compact")
        assert not app.has_class("text-scale-readable")
        app.action_cycle_text_scale()
        await pilot.pause()
        assert app.text_scale == "readable"
        assert app.has_class("text-scale-readable")
        app.action_cycle_text_scale()
        await pilot.pause()
        assert app.text_scale == "compact"
        assert app.has_class("text-scale-compact")
        app.action_cycle_text_scale()
        await pilot.pause()
        assert app.text_scale == "comfortable"
        assert not app.has_class("text-scale-compact")
        assert not app.has_class("text-scale-readable")


@pytest.mark.asyncio
async def test_motion_preset_respects_profile_caps(service: SessionService) -> None:
    create_managed(service, "motion", Tool.SHELL)
    app = WsApp(service, monochrome=False, onboarding=False)
    async with app.run_test(size=(120, 35)) as pilot:
        assert app.motion_preset == "auto"
        assert app.motion == "subtle"
        app.action_cycle_motion_preset()  # off
        await pilot.pause()
        assert app.motion == "off"
        app.action_cycle_motion_preset()  # subtle
        await pilot.pause()
        assert app.motion == "subtle"
        app.action_cycle_motion_preset()  # full (capped by balanced profile)
        await pilot.pause()
        assert app.motion_preset == "full"
        assert app.motion == "subtle"


@pytest.mark.asyncio
async def test_visual_modes_and_hint_density_toggle(service: SessionService) -> None:
    create_managed(service, "visual", Tool.SHELL)
    app = WsApp(service, monochrome=False, onboarding=False)
    async with app.run_test(size=(120, 35)) as pilot:
        assert app.hint_level == "minimal"
        app.action_toggle_hint_level()
        await pilot.pause()
        assert app.hint_level == "verbose"
        app.action_toggle_high_contrast()
        await pilot.pause()
        assert app.high_contrast
        assert app.has_class("high-contrast")
        app.action_cycle_accent_mode()
        await pilot.pause()
        assert app.accent_mode == "vivid"
        assert app.has_class("accent-vivid")
        app.action_cycle_accent_mode()
        await pilot.pause()
        app.action_cycle_accent_mode()
        await pilot.pause()
        assert app.accent_mode == "safe"
        assert app.has_class("accent-safe")


@pytest.mark.asyncio
async def test_layout_preset_cycles_density_and_text_scale(service: SessionService) -> None:
    create_managed(service, "layout-presets", Tool.SHELL)
    app = WsApp(service, monochrome=False, onboarding=False)
    async with app.run_test(size=(120, 35)) as pilot:
        assert app.density == "comfortable"
        assert app.text_scale == "comfortable"
        app.action_cycle_layout_preset()
        await pilot.pause()
        assert app.density == "comfortable"
        assert app.text_scale == "readable"
        app.action_cycle_layout_preset()
        await pilot.pause()
        assert app.density == "compact"
        assert app.text_scale == "compact"


@pytest.mark.asyncio
async def test_create_advanced_section_prefers_saved_expanded_state(
    service: SessionService,
) -> None:
    create_managed(service, "advanced-pref", Tool.SHELL)
    app = WsApp(service, monochrome=False, onboarding=False)
    app.create_advanced_by_default = True
    async with app.run_test(size=(120, 35)) as pilot:
        app.action_create()
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, CreateSessionScreen)
        advanced = screen.query_one("#create-advanced", VerticalScroll)
        assert not advanced.has_class("collapsed")
        toggle = screen.query_one("#create-advanced-toggle", Button)
        assert "Hide advanced options" in str(toggle.label)


@pytest.mark.asyncio
async def test_interface_controls_panel_applies_theme_in_place(service: SessionService) -> None:
    create_managed(service, "iface-panel", Tool.SHELL)
    app = WsApp(service, monochrome=False, onboarding=False)
    async with app.run_test(size=(120, 35)) as pilot:
        initial_theme = app.ui_theme
        await pilot.press("I")
        await pilot.pause()
        assert isinstance(app.screen, InterfaceControlsScreen)
        await pilot.press("enter")
        await pilot.pause()
        assert app.ui_theme != initial_theme
        assert isinstance(app.screen, InterfaceControlsScreen)
        await pilot.press("escape")
        await pilot.pause()
        assert app.interaction_mode is InteractionMode.NORMAL


@pytest.mark.asyncio
async def test_toggle_create_advanced_default_action(service: SessionService) -> None:
    create_managed(service, "advanced-default", Tool.SHELL)
    app = WsApp(service, monochrome=False, onboarding=False)
    async with app.run_test(size=(120, 35)) as pilot:
        assert app.create_advanced_by_default is False
        app.action_toggle_create_advanced_default()
        await pilot.pause()
        assert app.create_advanced_by_default is True


@pytest.mark.asyncio
async def test_no_color_starts_in_monochrome_mode(
    service: SessionService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NO_COLOR", "1")
    app = WsApp(service, hostname="no-color-host", onboarding=False)
    assert app.monochrome
    assert app.ui_theme == "monochrome"
    async with app.run_test(size=(120, 35)) as pilot:
        await pilot.pause()
        assert app.theme == "monochrome"


def test_motion_can_be_disabled_by_cli_env_and_monochrome(
    service: SessionService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WS_MOTION", "off")
    assert WsApp(service, monochrome=False, onboarding=False).motion == "off"
    monkeypatch.delenv("WS_MOTION")
    assert WsApp(service, monochrome=False, onboarding=False, no_animation=True).motion == "off"
    assert WsApp(service, monochrome=True, onboarding=False).motion == "off"


def test_motion_precedence_preserves_full_mode_until_an_accessibility_override(
    service: SessionService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service.config = service.config.model_copy(
        update={"interface": InterfaceConfig(animations="full")}
    )
    assert WsApp(service, monochrome=False, onboarding=False).motion == "full"
    monkeypatch.setenv("WS_MOTION", "off")
    assert WsApp(service, monochrome=False, onboarding=False).motion == "off"
    monkeypatch.setenv("WS_MOTION", "full")
    assert WsApp(service, monochrome=False, onboarding=False, no_animation=True).motion == "off"
    service.config = service.config.model_copy(
        update={"interface": InterfaceConfig(animations="full", reduce_motion=True)}
    )
    assert WsApp(service, monochrome=False, onboarding=False).motion == "off"


def test_ws_no_animation_env_disables_motion(
    service: SessionService, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WS_NO_ANIMATION", "1")
    assert WsApp(service, monochrome=False, onboarding=False).motion == "off"


def test_clipboard_copy_falls_back_to_osc52_when_native_unavailable(
    service: SessionService, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = WsApp(service, monochrome=False, onboarding=False)

    def _raise_copy(_self: App[object], _text: str) -> None:
        raise RuntimeError("clipboard unavailable")

    monkeypatch.setattr(App, "copy_to_clipboard", _raise_copy)
    monkeypatch.setattr(app, "_osc52_copy", lambda _text: True)

    assert app.copy_to_clipboard("hello")
    assert app._last_copy_channel == "osc52"


def test_clipboard_copy_reports_failure_when_no_provider_available(
    service: SessionService, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = WsApp(service, monochrome=False, onboarding=False)

    def _raise_copy(_self: App[object], _text: str) -> None:
        raise RuntimeError("clipboard unavailable")

    monkeypatch.setattr(App, "copy_to_clipboard", _raise_copy)
    monkeypatch.setattr(app, "_osc52_copy", lambda _text: False)

    assert app.copy_to_clipboard("hello") is False
    assert app._last_copy_channel == "none"


@pytest.mark.asyncio
async def test_performance_budget_scales_with_terminal_size(service: SessionService) -> None:
    create_managed(service, "size-a", Tool.SHELL)
    wide = WsApp(service, monochrome=False, onboarding=False, no_animation=True)
    async with wide.run_test(size=(160, 45)) as pilot:
        await pilot.pause()
        wide_cap = wide._session_render_cap
    narrow = WsApp(service, monochrome=False, onboarding=False, no_animation=True)
    async with narrow.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        narrow_cap = narrow._session_render_cap
    assert narrow_cap <= wide_cap


@pytest.mark.asyncio
async def test_high_latency_triggers_auto_safe_mode_and_live_perf_stats(
    service: SessionService,
) -> None:
    create_managed(service, "latency", Tool.SHELL)
    app = WsApp(service, monochrome=False, onboarding=False, no_animation=True)
    async with app.run_test(size=(120, 35)) as pilot:
        await pilot.pause()
        app._refresh_latency_ema_ms = 1400
        app._update_performance_budget()
        await pilot.pause()
        assert app._auto_safe_mode
        assert app.motion == "off"
        header = str(app.query_one("#app-header", Static).content)
        assert "perf ssh-safe(auto)" in header
        assert "rows/" in header


@pytest.mark.asyncio
async def test_modal_cancel_restores_dashboard_focus(service: SessionService) -> None:
    create_managed(service, "focus", Tool.SHELL)
    app = WsApp(service, monochrome=False, onboarding=False, no_animation=True)
    async with app.run_test(size=(120, 35)) as pilot:
        sessions = app.query_one("#sessions", OptionList)
        assert app.focused is sessions
        await pilot.press("c", "escape")
        await pilot.pause()
        assert app.screen is app.screen_stack[0]
        assert app.focused is sessions


@pytest.mark.asyncio
async def test_failed_refresh_preserves_selection_and_filters(
    service: SessionService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    create_managed(service, "refresh", Tool.SHELL)
    app = WsApp(service, monochrome=False, onboarding=False, no_animation=True)
    async with app.run_test(size=(120, 35)) as pilot:
        await pilot.press("/", "r", "e", "f", "enter")
        selected = app.selected_name

        def fail_refresh(*args: object, **kwargs: object):  # type: ignore[no-untyped-def]
            raise TmuxError("simulated refresh interruption")

        monkeypatch.setattr(service, "list_sessions", fail_refresh)
        app.refresh_sessions()
        await pilot.pause()
        assert app.selected_name == selected
        assert app.filter_query == "ref"
        assert not app.tmux_connected
        assert "tmux unavailable" in str(app.query_one("#app-header", Static).content)


@pytest.mark.asyncio
async def test_health_disabled_by_default_never_scans(service: SessionService) -> None:
    app = WsApp(service, monochrome=False, onboarding=False, no_animation=True)
    async with app.run_test(size=(120, 35)) as pilot:
        await pilot.pause()
        assert app._health_checks == []
        assert not app._health_scanning
        assert not app.has_class("has-critical-alerts")


@pytest.mark.asyncio
async def test_cached_health_alert_shown_instantly_without_a_live_probe(
    service: SessionService,
) -> None:
    enable_health(service)
    cached_check = HealthCheck(
        name="disk-space", status=HealthStatus.WARN, detail="fabricated cached value"
    )
    service._write_health_cache("disk-space", cached_check)
    app = WsApp(service, monochrome=False, onboarding=False, no_animation=True)
    async with app.run_test(size=(120, 35)):
        # Asserted before any pilot.pause(): the cached value must already be
        # rendered synchronously from on_mount, before any background worker
        # could possibly have completed a fresh probe.
        assert app._health_checks == [cached_check]
        assert not app.has_class("has-critical-alerts")
        assert app._health_warning_count() == 1


@pytest.mark.asyncio
async def test_health_row_stays_collapsed_for_noncritical_scan_results(
    service: SessionService, monkeypatch: pytest.MonkeyPatch
) -> None:
    enable_health(service, disk_warn_percent=60, disk_fail_percent=40)

    class FakeUsage:
        total = 100
        free = 50

    monkeypatch.setattr("shutil.disk_usage", lambda _root: FakeUsage())
    app = WsApp(service, monochrome=False, onboarding=False, no_animation=True)
    async with app.run_test(size=(120, 35)) as pilot:
        await wait_for_health_scan(pilot, app)
        assert not app.has_class("has-critical-alerts")
        assert any(check.status is HealthStatus.WARN for check in app._health_checks)
        assert app._health_warning_count() == 1


@pytest.mark.asyncio
async def test_health_row_hidden_when_all_checks_pass(
    service: SessionService, monkeypatch: pytest.MonkeyPatch
) -> None:
    enable_health(service)

    class FakeUsage:
        total = 100
        free = 90

    monkeypatch.setattr("shutil.disk_usage", lambda _root: FakeUsage())
    app = WsApp(service, monochrome=False, onboarding=False, no_animation=True)
    async with app.run_test(size=(120, 35)) as pilot:
        await wait_for_health_scan(pilot, app)
        assert not app.has_class("has-critical-alerts")


@pytest.mark.asyncio
async def test_finish_health_scan_discards_stale_generation(service: SessionService) -> None:
    enable_health(service)
    app = WsApp(service, monochrome=False, onboarding=False, no_animation=True)
    async with app.run_test(size=(120, 35)):
        app._health_checks = []
        stale_result = [
            HealthCheck(name="disk-space", status=HealthStatus.FAIL, detail="should be ignored")
        ]
        app._finish_health_scan(app._health_scan_generation - 1, stale_result, "")
        assert app._health_checks == []
        assert not app.has_class("has-critical-alerts")


@pytest.mark.asyncio
async def test_health_scan_does_not_start_while_a_modal_is_open(
    service: SessionService,
) -> None:
    enable_health(service)
    app = WsApp(service, monochrome=False, onboarding=False, no_animation=True)
    async with app.run_test(size=(120, 35)) as pilot:
        await wait_for_health_scan(pilot, app)
        app.action_diagnostics()
        await pilot.pause()
        assert len(app.screen_stack) > 1
        app._health_scanning = False
        app._start_health_scan(force=True)
        assert not app._health_scanning


@pytest.mark.asyncio
async def test_health_alerts_screen_opens_shows_checks_and_refreshes(
    service: SessionService, monkeypatch: pytest.MonkeyPatch
) -> None:
    enable_health(service, disk_warn_percent=60, disk_fail_percent=40)

    class FakeUsage:
        total = 100
        free = 50

    monkeypatch.setattr("shutil.disk_usage", lambda _root: FakeUsage())
    app = WsApp(service, monochrome=False, onboarding=False, no_animation=True)
    async with app.run_test(size=(120, 35)) as pilot:
        await wait_for_health_scan(pilot, app)
        app.action_health_alerts()
        await pilot.pause()
        assert isinstance(app.screen, HealthAlertsScreen)
        screen = app.screen
        for _ in range(40):
            if not screen.running:
                break
            await pilot.pause(0.05)
        assert any(check.name == "disk-space" for check in screen.checks)
        assert "STATUS  CHECK" in str(screen.query_one("#health-alerts-header", Static).content)
        assert "Disk space" in str(screen.query_one("#health-alerts-content", Static).content)
        assert "Selected warning detail" in str(
            screen.query_one("#health-alerts-selected", Static).content
        )

        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, HealthAlertsScreen)


@pytest.mark.asyncio
async def test_health_alerts_view_sessions_focuses_related_warning_sessions(
    service: SessionService, monkeypatch: pytest.MonkeyPatch
) -> None:
    stopped_name = create_managed(service, "old-warning", Tool.CLAUDE)
    service.stop_session(stopped_name)
    warning_check = HealthCheck(
        name="zombie-sessions",
        status=HealthStatus.WARN,
        detail="1 stopped session untouched for 14+ days",
        corrective_action="Review stopped sessions.",
    )
    monkeypatch.setattr(
        service, "refresh_health_alerts", lambda force=False, only=None: [warning_check]
    )
    monkeypatch.setattr(service, "cached_health_alerts", lambda: [warning_check])
    app = WsApp(service, monochrome=False, onboarding=False, no_animation=True)
    async with app.run_test(size=(120, 35)) as pilot:
        await pilot.pause()
        app.action_health_alerts()
        await pilot.pause()
        assert isinstance(app.screen, HealthAlertsScreen)
        await pilot.press("v")
        await pilot.pause()
        assert not isinstance(app.screen, HealthAlertsScreen)
        assert app.quick_filter == "warnings"
        assert app.filters.warnings_only
        assert app.selected_name == stopped_name


@pytest.mark.asyncio
async def test_policy_sandbox_screen_opens_and_renders_simulation(
    service: SessionService,
) -> None:
    app = WsApp(service, monochrome=False, onboarding=False, no_animation=True)
    async with app.run_test(size=(120, 35)) as pilot:
        await pilot.press("v")
        await pilot.pause()
        assert isinstance(app.screen, PolicySandboxScreen)
        hint = str(app.screen.query_one(".inline-chip", Static).content)
        assert "simulation-only" in hint
        output = str(app.screen.query_one("#policy-output", Static).content)
        assert "Policy simulation" in output
        assert "sandbox preview only" in output
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, PolicySandboxScreen)


@pytest.mark.asyncio
async def test_dependency_graph_screen_opens_and_shows_relations(
    service: SessionService,
) -> None:
    service.add_dependency("api-worker", "api-db")
    service.add_dependency("api-worker", "api-cache")
    service.add_dependency("api-ingest", "api-worker")
    app = WsApp(service, monochrome=False, onboarding=False, no_animation=True)
    async with app.run_test(size=(120, 35)) as pilot:
        await pilot.press("D")
        await pilot.pause()
        assert isinstance(app.screen, DependencyGraphScreen)
        details = str(app.screen.query_one("#dependency-details", Static).content)
        assert "Blocked by:" in details
        assert "Critical path:" in details
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, DependencyGraphScreen)


@pytest.mark.asyncio
async def test_federation_control_center_opens_and_filters_hosts(
    service: SessionService, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        service,
        "federated_sessions",
        lambda hosts=None: [
            {
                "host": "vm-a",
                "error": "",
                "sessions": [{"name": "claude-api"}, {"name": "codex-review"}],
            },
            {"host": "vm-b", "error": "ssh timeout", "sessions": []},
        ],
    )
    app = WsApp(service, monochrome=False, onboarding=False, no_animation=True)
    async with app.run_test(size=(120, 35)) as pilot:
        await pilot.press("F")
        await pilot.pause()
        assert isinstance(app.screen, FederationControlScreen)
        await wait_for_federation_idle(pilot, app.screen)
        summary = str(app.screen.query_one("#federation-summary", Static).content)
        hint = str(app.screen.query_one("#federation-hint", Static).content)
        details = str(app.screen.query_one("#federation-details", Static).content)
        cards = str(app.screen.query_one("#federation-host-cards", Static).content)
        assert "Hosts: 2/2" in summary
        assert "selected host" in hint
        assert "Host:" in details
        assert "[vm-a] HEALTHY" in cards
        assert "[vm-b] DOWN" in cards
        app.screen.query_one("#federation-filter", Input).value = "vm-b"
        await pilot.pause()
        options = app.screen.query_one("#federation-hosts", OptionList)
        assert options.option_count == 1
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, FederationControlScreen)


@pytest.mark.asyncio
async def test_federation_control_center_dispatches_bulk_action_for_filtered_hosts(
    service: SessionService, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        service,
        "federated_sessions",
        lambda hosts=None: [
            {"host": "vm-a", "error": "", "sessions": [{"name": "claude-api"}]},
            {"host": "vm-b", "error": "", "sessions": [{"name": "codex-review"}]},
        ],
    )
    calls: list[tuple[str, tuple[str, ...]]] = []

    def fake_action(action: str, *, hosts=None, args=()):
        selected_hosts = tuple(hosts or [])
        calls.append((action, selected_hosts))
        return [{"host": host, "ok": True, "error": "", "stdout": ""} for host in selected_hosts]

    monkeypatch.setattr(service, "federated_action", fake_action)
    app = WsApp(service, monochrome=False, onboarding=False, no_animation=True)
    async with app.run_test(size=(120, 35)) as pilot:
        await pilot.press("F")
        await pilot.pause()
        assert isinstance(app.screen, FederationControlScreen)
        await wait_for_federation_idle(pilot, app.screen)
        app.screen.query_one("#federation-filter", Input).value = "vm-b"
        await pilot.pause()
        await pilot.press("a")
        await pilot.press("2")
        await wait_for_federation_idle(pilot, app.screen)
        assert calls == [("health", ("vm-b",))]
        status = str(app.screen.query_one("#federation-action-status", Static).content)
        assert "health completed on 1 host(s)" in status


@pytest.mark.asyncio
async def test_federation_control_center_focuses_matching_local_session(
    service: SessionService, monkeypatch: pytest.MonkeyPatch
) -> None:
    local_name = create_managed(service, "api", Tool.CLAUDE)
    monkeypatch.setattr(
        service,
        "federated_sessions",
        lambda hosts=None: [
            {"host": "vm-a", "error": "", "sessions": [{"name": local_name}]},
        ],
    )
    app = WsApp(service, monochrome=False, onboarding=False, no_animation=True)
    async with app.run_test(size=(120, 35)) as pilot:
        await pilot.press("F")
        await pilot.pause()
        assert isinstance(app.screen, FederationControlScreen)
        await wait_for_federation_idle(pilot, app.screen)
        await pilot.press("enter")
        await pilot.pause()
        assert not isinstance(app.screen, FederationControlScreen)
        assert app.selected_name == local_name


@pytest.mark.asyncio
async def test_federation_control_center_focuses_warning_session_first(
    service: SessionService, monkeypatch: pytest.MonkeyPatch
) -> None:
    ok_name = create_managed(service, "api-ok", Tool.CLAUDE)
    warn_name = create_managed(service, "api-warn", Tool.COPILOT)
    monkeypatch.setattr(
        service,
        "federated_sessions",
        lambda hosts=None: [
            {
                "host": "vm-a",
                "error": "",
                "sessions": [
                    {"name": ok_name, "runtime": "attached", "task_state": "in_progress"},
                    {"name": warn_name, "runtime": "stopped", "task_state": "blocked"},
                ],
            },
        ],
    )
    app = WsApp(service, monochrome=False, onboarding=False, no_animation=True)
    async with app.run_test(size=(120, 35)) as pilot:
        await pilot.press("F")
        await pilot.pause()
        assert isinstance(app.screen, FederationControlScreen)
        await wait_for_federation_idle(pilot, app.screen)
        await pilot.press("w")
        await pilot.pause()
        assert not isinstance(app.screen, FederationControlScreen)
        assert app.selected_name == warn_name


@pytest.mark.asyncio
async def test_federation_control_center_manage_action_drillthrough(
    service: SessionService, monkeypatch: pytest.MonkeyPatch
) -> None:
    warn_name = create_managed(service, "api-manage", Tool.CLAUDE)
    monkeypatch.setattr(
        service,
        "federated_sessions",
        lambda hosts=None: [
            {
                "host": "vm-a",
                "error": "",
                "sessions": [{"name": warn_name, "runtime": "failed", "task_state": "blocked"}],
            },
        ],
    )
    app = WsApp(service, monochrome=False, onboarding=False, no_animation=True)
    async with app.run_test(size=(120, 35)) as pilot:
        await pilot.press("F")
        await pilot.pause()
        assert isinstance(app.screen, FederationControlScreen)
        await wait_for_federation_idle(pilot, app.screen)
        await pilot.press("m")
        await pilot.pause()
        assert isinstance(app.screen, ManageSessionScreen)
        assert app.selected_name == warn_name


@pytest.mark.asyncio
async def test_federation_control_center_tracks_per_host_health_summary(
    service: SessionService, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        service,
        "federated_sessions",
        lambda hosts=None: [
            {"host": "vm-a", "error": "", "sessions": [{"name": "claude-api"}]},
        ],
    )

    def fake_action(action: str, *, hosts=None, args=()):
        assert action == "health"
        host = (hosts or ["vm-a"])[0]
        report = {
            "checks": [
                {"name": "disk", "status": "pass", "detail": "ok"},
                {"name": "docker", "status": "warn", "detail": "degraded"},
                {"name": "tmux", "status": "fail", "detail": "down"},
            ]
        }
        return [{"host": host, "ok": False, "error": "exit 1", "stdout": json.dumps(report)}]

    monkeypatch.setattr(service, "federated_action", fake_action)
    app = WsApp(service, monochrome=False, onboarding=False, no_animation=True)
    async with app.run_test(size=(120, 35)) as pilot:
        await pilot.press("F")
        await pilot.pause()
        assert isinstance(app.screen, FederationControlScreen)
        await wait_for_federation_idle(pilot, app.screen)
        await pilot.press("2")
        await wait_for_federation_idle(pilot, app.screen)
        details = str(app.screen.query_one("#federation-details", Static).content)
        cards = str(app.screen.query_one("#federation-host-cards", Static).content)
        assert "Health checks: fail=1 warn=1 pass=1" in details
        assert "last-health=fail=1 warn=1 pass=1" in cards


@pytest.mark.asyncio
async def test_federation_control_center_renders_sla_panel_with_ownership_hints(
    service: SessionService, monkeypatch: pytest.MonkeyPatch
) -> None:
    local_name = create_managed(service, "api-sla", Tool.CLAUDE)
    service.config = service.config.model_copy(
        update={
            "sla_rules": (
                SlaRuleConfig(
                    name="urgent",
                    severity="warn",
                    detached_hours=1.0,
                    blocked_hours=1.0,
                    input_required_hours=1.0,
                ),
            )
        }
    )
    stale = (datetime.now(UTC) - timedelta(hours=5)).isoformat()
    monkeypatch.setattr(
        service,
        "federated_sessions",
        lambda hosts=None: [
            {
                "host": "vm-a",
                "error": "",
                "sessions": [
                    {
                        "name": local_name,
                        "runtime": "detached",
                        "task_state": "in_progress",
                        "input_state": "none",
                        "created_at": stale,
                    }
                ],
            },
        ],
    )
    app = WsApp(service, monochrome=False, onboarding=False, no_animation=True)
    async with app.run_test(size=(120, 35)) as pilot:
        await pilot.press("F")
        await pilot.pause()
        assert isinstance(app.screen, FederationControlScreen)
        await wait_for_federation_idle(pilot, app.screen)
        panel = str(app.screen.query_one("#federation-sla-panel", Static).content)
        assert "SLA breaches (1)" in panel
        assert "vm-a/" in panel
        assert "managed locally" in panel


@pytest.mark.asyncio
async def test_federation_control_center_saves_and_applies_pinned_view(
    service: SessionService, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_federated_sessions(hosts=None):
        selected = list(hosts or ["vm-a", "vm-b"])
        rows = []
        for host in selected:
            rows.append({"host": host, "error": "", "sessions": [{"name": f"{host}-session"}]})
        return rows

    monkeypatch.setattr(service, "federated_sessions", fake_federated_sessions)
    app = WsApp(service, monochrome=False, onboarding=False, no_animation=True)
    async with app.run_test(size=(120, 35)) as pilot:
        await pilot.press("F")
        await pilot.pause()
        assert isinstance(app.screen, FederationControlScreen)
        await wait_for_federation_idle(pilot, app.screen)
        app.screen.query_one("#federation-filter", Input).value = "vm-b"
        await pilot.pause()
        app.screen.query_one("#federation-dashboard-name", Input).value = "ops"
        await pilot.press("5")
        await pilot.pause()
        app.screen.query_one("#federation-filter", Input).value = ""
        await pilot.pause()
        await pilot.press("6")
        await wait_for_federation_idle(pilot, app.screen)
        summary = str(app.screen.query_one("#federation-summary", Static).content)
        assert "Hosts: 1/1" in summary


@pytest.mark.asyncio
async def test_federation_control_center_renders_fleet_diff_panel(
    service: SessionService, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        service,
        "federated_sessions",
        lambda hosts=None: [
            {"host": "vm-a", "error": "", "sessions": [{"name": "a1"}]},
            {"host": "vm-b", "error": "", "sessions": [{"name": "b1"}, {"name": "b2"}]},
        ],
    )
    monkeypatch.setattr(
        service,
        "fleet_diff_live",
        lambda hosts=(): {
            "baseline": "fleet-baseline",
            "drifts": [
                {"host": "vm-b", "field": "session_count", "left": 1, "right": 2},
                {"host": "vm-b", "field": "detached", "left": 0, "right": 2},
            ],
            "host_spread": [{"field": "session_count", "spread": 1}],
        },
    )
    app = WsApp(service, monochrome=False, onboarding=False, no_animation=True)
    async with app.run_test(size=(120, 35)) as pilot:
        await pilot.press("F")
        await pilot.pause()
        assert isinstance(app.screen, FederationControlScreen)
        await wait_for_federation_idle(pilot, app.screen)
        await pilot.press("8")
        await pilot.pause()
        panel = str(app.screen.query_one("#federation-diff-panel", Static).content)
        assert "Fleet diff vs fleet-baseline" in panel
        assert "drift item" in panel


@pytest.mark.asyncio
async def test_federation_control_center_safe_mode_reduces_scan_budget(
    service: SessionService, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_federated_sessions(hosts=None):
        selected = list(hosts) if hosts else [f"vm-{index:02d}" for index in range(60)]
        return [
            {"host": host, "error": "", "sessions": [{"name": f"{host}-a"}]} for host in selected
        ]

    monkeypatch.setattr(service, "federated_sessions", fake_federated_sessions)
    app = WsApp(service, monochrome=False, onboarding=False, no_animation=True)
    async with app.run_test(size=(120, 35)) as pilot:
        await pilot.press("F")
        await pilot.pause()
        assert isinstance(app.screen, FederationControlScreen)
        await wait_for_federation_idle(pilot, app.screen)
        summary = str(app.screen.query_one("#federation-summary", Static).content)
        assert "Hosts: 60/60" in summary
        await pilot.press("9")
        await wait_for_federation_idle(pilot, app.screen)
        summary = str(app.screen.query_one("#federation-summary", Static).content)
        assert "Mode: safe" in summary
        assert "Hosts: 40/40" in summary


def _write_log(service: SessionService, name: str, content: str) -> None:
    record = service.store.load(name)
    assert record is not None
    log_path = service.paths.logs_dir / f"{record.record_id}.log"
    log_path.write_text(content, encoding="utf-8")


@pytest.mark.asyncio
async def test_search_output_finds_matches_across_sessions(
    service: SessionService,
    fake_backend: FakeBackend,
) -> None:
    logged = service.create(
        CreateRequest(name="logged-one", tool=Tool.SHELL, cwd=Path("/tmp"), logging_enabled=True)
    )
    service.create(
        CreateRequest(name="unlogged-one", tool=Tool.SHELL, cwd=Path("/tmp"), logging_enabled=False)
    )
    _write_log(service, logged.name, "before\nneedle appears here\nafter\n")
    app = WsApp(service, monochrome=False, onboarding=False, no_animation=True)

    async with app.run_test(size=(120, 35)) as pilot:
        await pilot.press("s")
        screen = await wait_for_search_output_screen(pilot, app)
        for char in "needle":
            await pilot.press(char)
        await wait_for_output_search(pilot, screen)

        options = screen.query_one("#search-output-results", OptionList)
        option_ids = {options.get_option_at_index(i).id for i in range(options.option_count)}
        assert f"search-output-result:{logged.name}" in option_ids
        assert not any(
            id_ and id_.startswith("search-output-result:unlogged") for id_ in option_ids
        )
        detail = str(screen.query_one("#search-output-detail", Static).content)
        assert "needle appears here" in detail
        assert "before" in detail
        assert "after" in detail


@pytest.mark.asyncio
async def test_search_output_shows_no_matches_message(
    service: SessionService,
    fake_backend: FakeBackend,
) -> None:
    logged = service.create(
        CreateRequest(name="logged-two", tool=Tool.SHELL, cwd=Path("/tmp"), logging_enabled=True)
    )
    _write_log(service, logged.name, "nothing interesting here\n")
    app = WsApp(service, monochrome=False, onboarding=False, no_animation=True)

    async with app.run_test(size=(120, 35)) as pilot:
        await pilot.press("s")
        screen = await wait_for_search_output_screen(pilot, app)
        for char in "zzznomatch":
            await pilot.press(char)
        await wait_for_output_search(pilot, screen)

        options = screen.query_one("#search-output-results", OptionList)
        prompts = [str(options.get_option_at_index(i).prompt) for i in range(options.option_count)]
        assert any("No matches" in prompt for prompt in prompts)


@pytest.mark.asyncio
async def test_search_output_short_query_does_not_search(
    service: SessionService,
    fake_backend: FakeBackend,
) -> None:
    logged = service.create(
        CreateRequest(name="logged-three", tool=Tool.SHELL, cwd=Path("/tmp"), logging_enabled=True)
    )
    _write_log(service, logged.name, "a single needle line\n")
    app = WsApp(service, monochrome=False, onboarding=False, no_animation=True)

    async with app.run_test(size=(120, 35)) as pilot:
        await pilot.press("s")
        screen = await wait_for_search_output_screen(pilot, app)
        await pilot.press("n")
        await pilot.pause(0.5)

        options = screen.query_one("#search-output-results", OptionList)
        option_ids = {options.get_option_at_index(i).id for i in range(options.option_count)}
        assert option_ids == {"search-output-empty"}


@pytest.mark.asyncio
async def test_search_output_closes_and_restores_dashboard(
    service: SessionService,
    fake_backend: FakeBackend,
) -> None:
    service.create(CreateRequest(name="dash-session", tool=Tool.SHELL, cwd=Path("/tmp")))
    app = WsApp(service, monochrome=False, onboarding=False, no_animation=True)

    async with app.run_test(size=(120, 35)) as pilot:
        await pilot.press("s")
        await wait_for_search_output_screen(pilot, app)
        await pilot.press("escape")
        await pilot.pause()
        assert app.interaction_mode is InteractionMode.NORMAL
        assert not isinstance(app.screen, SearchOutputScreen)
