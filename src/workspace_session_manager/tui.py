"""Responsive Textual dashboard for persistent workflow sessions."""

from __future__ import annotations

import locale
import os
import re
import shlex
import socket
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from time import perf_counter
from typing import Any, ClassVar, Literal, NamedTuple

from rich.text import Text
from textual import events, on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.command import CommandPalette, DiscoveryHit, Hit, Hits, Provider
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.theme import Theme
from textual.timer import Timer
from textual.widget import Widget
from textual.widgets import (
    Button,
    Checkbox,
    Input,
    Label,
    LoadingIndicator,
    OptionList,
    Select,
    Static,
    Switch,
    TextArea,
)
from textual.widgets.option_list import Option

from workspace_session_manager import __version__
from workspace_session_manager.errors import WsError
from workspace_session_manager.models import (
    AgentState,
    CreateRequest,
    DoctorReport,
    HealthCheck,
    HealthStatus,
    InputState,
    InterfacePreferences,
    OutputSource,
    RuntimeState,
    SessionDetails,
    SessionView,
    TaskState,
    Tool,
    normalize_tags,
)
from workspace_session_manager.notifier import send_telegram
from workspace_session_manager.service import (
    LogSearchResult,
    LogSearchSummary,
    SessionService,
    TailResult,
    normalized_session_name,
)
from workspace_session_manager.store import InterfacePreferencesStore

BindingSpec = Binding | tuple[str, str] | tuple[str, str, str]
RECENT_WINDOW = timedelta(hours=24)
GroupingMode = Literal["attention", "runtime", "agent", "project", "warning", "recent"]
DensityMode = Literal["compact", "comfortable"]
GROUPING_MODES: tuple[GroupingMode, ...] = (
    "attention",
    "runtime",
    "agent",
    "project",
    "warning",
    "recent",
)
GROUPING_LABELS = {
    "attention": "Attention",
    "runtime": "Runtime",
    "agent": "Agent",
    "project": "Project",
    "warning": "Warnings",
    "recent": "Recent activity",
}
THEME_MODES = (
    "ithaca",
    "dark",
    "light",
    "monochrome",
    "midnight",
    "cyberpunk",
    "terminal",
    "paper",
)


class _ThemePalette(NamedTuple):
    primary: str
    accent: str
    background: str
    surface: str
    panel: str
    foreground: str
    dark: bool
    warning: str | None = None
    error: str | None = None
    success: str | None = None


def _build_theme_definitions() -> dict[str, Theme]:
    """Base tokens for each named theme, reusing the palettes shipped previously.

    warning/error/success stay a shared neutral triad across themes (they're
    semantic status colors, not identity colors) except monochrome, which
    intentionally collapses everything to gray for its no-color mode.
    """
    neutral_warning, neutral_error, neutral_success = "#e9b44c", "#ef6b73", "#72c78e"
    palettes: dict[str, _ThemePalette] = {
        "ithaca": _ThemePalette(
            primary="#3f8a9d",
            accent="#e06c75",
            background="#282c34",
            surface="#2c3038",
            panel="#333844",
            foreground="#c9d8e0",
            dark=True,
        ),
        "dark": _ThemePalette(
            primary="#3f6e9d",
            accent="#24598a",
            background="#101316",
            surface="#12171a",
            panel="#181d21",
            foreground="#e8edf0",
            dark=True,
        ),
        "midnight": _ThemePalette(
            primary="#4d8cff",
            accent="#4d8cff",
            background="#070b14",
            surface="#0a1220",
            panel="#0d1524",
            foreground="#dfe6f0",
            dark=True,
        ),
        "cyberpunk": _ThemePalette(
            primary="#00e5ff",
            accent="#ff2e88",
            background="#0d0221",
            surface="#170a2c",
            panel="#1a0d33",
            foreground="#f2f2f2",
            dark=True,
        ),
        "terminal": _ThemePalette(
            primary="#1fae4a",
            accent="#33ff66",
            background="#0a0f0a",
            surface="#0d130d",
            panel="#10160f",
            foreground="#33ff66",
            dark=True,
        ),
        "light": _ThemePalette(
            primary="#3f6e9d",
            accent="#24598a",
            background="#f4f6f7",
            surface="#edf1f3",
            panel="#e7ecef",
            foreground="#20272b",
            dark=False,
        ),
        "monochrome": _ThemePalette(
            primary="#9e9e9e",
            accent="#d7d7d7",
            background="#111111",
            surface="#111111",
            panel="#111111",
            foreground="#e6e6e6",
            warning="#d7d7d7",
            error="#d7d7d7",
            success="#d7d7d7",
            dark=True,
        ),
        "paper": _ThemePalette(
            primary="#9a6a35",
            accent="#8a5a2b",
            background="#f7f3e9",
            surface="#efe9d8",
            panel="#efe6d3",
            foreground="#2b2620",
            dark=False,
        ),
    }
    themes: dict[str, Theme] = {}
    for name, palette in palettes.items():
        themes[name] = Theme(
            name=name,
            primary=palette.primary,
            secondary=palette.primary,
            accent=palette.accent,
            background=palette.background,
            surface=palette.surface,
            panel=palette.panel,
            foreground=palette.foreground,
            warning=palette.warning or neutral_warning,
            error=palette.error or neutral_error,
            success=palette.success or neutral_success,
            dark=palette.dark,
        )
    return themes


THEME_DEFINITIONS = _build_theme_definitions()
ATTENTION_PREVIEW_LINES = 20
ATTENTION_PREVIEW_BYTES = 8_192
USAGE_LIMIT_PATTERN = re.compile(
    r"(?im)^(?P<line>[^\n]*(?:(?:usage|session|rate)\s+limit\s+"
    r"(?:has been\s+|was\s+)?(?:reached|exceeded)|"
    r"you(?:'|\u2019)ve\s+hit\s+your\s+(?:session|usage)\s+limit)[^\n]*)$"
)
RETRY_PATTERN = re.compile(
    r"(?im)^(?:retry(?: available)?|try again|available again|resets?)"
    r"\s*(?:at|after|:)?\s*(?P<when>[^\n]+)$"
)
RAW_TASK_PATTERN = re.compile(
    r"(?i)^(?:claude|codex|hermes)\s+task:\s*(?P<task>.+?)(?:\s+\([^)]*\))?$"
)
TOOL_LABELS = {
    Tool.CLAUDE: "Claude Code",
    Tool.CODEX: "Codex",
    Tool.HERMES: "Hermes",
    Tool.SHELL: "Shell",
}
TOOL_STYLES = {
    Tool.CLAUDE: "bold #c792ea",
    Tool.CODEX: "bold #66aaff",
    Tool.HERMES: "bold #e9b44c",
    Tool.SHELL: "bold #72c78e",
}
RUNTIME_STYLES = {
    RuntimeState.ATTACHED: "#72c78e",
    RuntimeState.DETACHED: "#9aa6ad",
    RuntimeState.STOPPED: "#e9b44c",
    RuntimeState.FAILED: "bold #ef6b73",
    RuntimeState.UNKNOWN: "#9aa6ad",
}
# Monochrome/NO_COLOR mode strips hue but keeps brightness contrast so tool and
# runtime states stay scannable without color.
MONO_TOOL_STYLE = "bold #e6e6e6"
MONO_RUNTIME_STYLES = {
    RuntimeState.ATTACHED: "#e6e6e6",
    RuntimeState.DETACHED: "#9e9e9e",
    RuntimeState.STOPPED: "#b8b8b8",
    RuntimeState.FAILED: "bold #ffffff",
    RuntimeState.UNKNOWN: "#9e9e9e",
}


def tool_style(tool: Tool, *, monochrome: bool = False) -> str:
    return MONO_TOOL_STYLE if monochrome else TOOL_STYLES[tool]


def runtime_style(runtime: RuntimeState, *, monochrome: bool = False) -> str:
    return MONO_RUNTIME_STYLES[runtime] if monochrome else RUNTIME_STYLES[runtime]


def display_path(path: Path) -> str:
    """Render a home-relative path without producing the invalid `~/.` form."""
    try:
        relative = path.expanduser().relative_to(Path.home())
    except ValueError:
        return str(path)
    return "~" if relative == Path(".") else f"~/{relative}"


def display_state(value: str) -> str:
    return value.replace("_", " ").capitalize()


def display_input(value: InputState) -> str:
    return "Required" if value is InputState.REQUIRED else "Not required"


def humanize_task(value: str) -> str:
    """Turn assessed legacy task labels into conservative, readable descriptions."""
    task = value.strip()
    match = RAW_TASK_PATTERN.fullmatch(task)
    if not match:
        return task
    identifier = match.group("task").strip()
    identifier = re.sub(r"^(?:https?|www)[-_]", "", identifier, flags=re.IGNORECASE)
    words = re.sub(r"[-_]+", " ", identifier).split()
    if not words:
        return ""
    return f"Work on {' '.join(words)}"


@dataclass(frozen=True, slots=True)
class ActivityNotice:
    level: str
    title: str
    detail: str
    agent_state: AgentState
    kind: str = "none"

    @property
    def warning(self) -> bool:
        return self.level in {"warning", "error"}


@dataclass(frozen=True, slots=True)
class AttentionScanRequest:
    session: SessionView
    notice_revision: int


@dataclass(frozen=True, slots=True)
class AttentionScanResult:
    name: str
    session_id: str
    preview: str = ""
    error: str = ""


def detect_activity(session: SessionView, output: str) -> ActivityNotice:
    if session.runtime is RuntimeState.FAILED:
        return ActivityNotice(
            "error",
            "Session failed",
            "The active pane exited with a failure status.",
            AgentState.FAILED,
            "runtime-failed",
        )
    if session.runtime is RuntimeState.STOPPED:
        return ActivityNotice(
            "warning",
            "Session stopped",
            "Restart the tool from Manage when you are ready to continue.",
            AgentState.STOPPED,
            "runtime-stopped",
        )
    usage = USAGE_LIMIT_PATTERN.search(output)
    if usage:
        line = usage.group("line")
        tool = next(
            (
                TOOL_LABELS[item]
                for item in (Tool.CLAUDE, Tool.CODEX, Tool.HERMES)
                if item.value in line.casefold()
            ),
            TOOL_LABELS[session.tool],
        )
        limit_kind = "session" if "session limit" in line.casefold() else "usage"
        retry = RETRY_PATTERN.search(output)
        detail = f"Retry available: {retry.group('when').strip()}" if retry else "Try again later."
        return ActivityNotice(
            "warning",
            f"{tool} {limit_kind} limit reached",
            detail,
            AgentState.PAUSED,
            "usage-limit",
        )
    if session.input_state is InputState.REQUIRED or session.task_state is TaskState.NEEDS_INPUT:
        return ActivityNotice(
            "warning",
            "Input required",
            "This status was explicitly set for the session.",
            AgentState.WAITING,
            "input-required",
        )
    if session.task_state is TaskState.BLOCKED:
        return ActivityNotice(
            "warning",
            "Task blocked",
            "Review the task note before continuing.",
            AgentState.WAITING,
            "task-blocked",
        )
    if session.task_state is TaskState.WAITING:
        return ActivityNotice(
            "neutral", "Waiting", "No user input is currently required.", AgentState.WAITING
        )
    if session.task_state is TaskState.COMPLETED:
        return ActivityNotice(
            "neutral", "Task completed", "No action is required.", AgentState.COMPLETED
        )
    return ActivityNotice(
        "neutral", "No action required", "The session can continue normally.", AgentState.ACTIVE
    )


def summarize_output(output: str, notice: ActivityNotice, warning_color: str = "yellow") -> Text:
    """Render a conservative activity summary without exposing CLI chrome by default."""
    summary = Text()
    if notice.kind == "usage-limit":
        summary.append(notice.title, f"bold {warning_color}")
        summary.append(f"\n{notice.detail}\n")
        summary.append("The tmux session remains active, but the agent cannot continue yet.")
        return summary
    ignored = re.compile(
        r"(?i)^(?:tokens?|context|model|working directory|approval|session id|[-=]{3,}|[>$#]\s*)"
    )
    useful = [
        line.strip()
        for line in output.splitlines()
        if line.strip() and not ignored.match(line.strip())
    ][-4:]
    if useful:
        summary.append("Last meaningful output\n", "bold")
        summary.append("\n".join(useful))
    else:
        summary.append(notice.title, "bold")
        summary.append(f"\n{notice.detail}")
    summary.append("\n\n[l] Open full output", "dim")
    return summary


def relative_activity(value: datetime | None, *, now: datetime | None = None) -> str:
    if value is None:
        return "unknown"
    current = now or datetime.now(UTC)
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    seconds = max(0, int((current - value).total_seconds()))
    if seconds < 60:
        return "<1m"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h"
    days = hours // 24
    return f"{days}d"


def truncate(value: str, width: int, *, ascii_only: bool = False) -> str:
    if len(value) <= width:
        return value
    marker = "..." if ascii_only else "…"
    return f"{value[: max(1, width - len(marker))]}{marker}"


def is_warning(session: SessionView, notice: ActivityNotice | None = None) -> bool:
    return (notice or detect_activity(session, "")).warning


def session_group(session: SessionView, *, now: datetime | None = None) -> str:
    """Assign each session to exactly one dashboard group."""
    if not session.owned:
        return "Unmanaged"
    if session.input_state is InputState.REQUIRED or session.task_state is TaskState.NEEDS_INPUT:
        return "Needs Input"
    if session.pinned:
        return "Pinned"
    if session.runtime is RuntimeState.ATTACHED:
        return "Attached"
    if session.runtime is RuntimeState.FAILED:
        return "Failed"
    if session.runtime is RuntimeState.STOPPED:
        return "Stopped"
    return "Detached"


def session_option_id(name: str, session_id: str) -> str:
    """Return the unique Textual option ID for a tmux session.

    tmux session IDs are only unique within a tmux server.  The dashboard can
    show sessions from multiple tool backends, so use the session name as well
    to avoid collisions such as Claude's ``$1`` and Codex's ``$1``.
    """
    return f"session:{name}:{session_id}"


def build_session_groups(
    sessions: Sequence[SessionView],
    *,
    grouping: GroupingMode,
    notices: Callable[[SessionView], ActivityNotice],
    now: datetime | None = None,
) -> list[tuple[str, list[SessionView]]]:
    """Build stable, exclusive dashboard groups with visible counts.

    Pinned sessions are intentionally sorted within their selected grouping instead
    of forming a competing group. This keeps grouping mode predictable while
    retaining pinning as a useful personal priority signal.
    """
    current = now or datetime.now(UTC)
    buckets: dict[str, list[SessionView]] = {}
    order: list[str] = []

    def add(label: str, session: SessionView) -> None:
        if label not in buckets:
            buckets[label] = []
            order.append(label)
        buckets[label].append(session)

    for session in sessions:
        notice = notices(session)
        if grouping == "attention":
            if not session.owned:
                label = "UNMANAGED"
            elif (
                session.input_state is InputState.REQUIRED
                or session.task_state is TaskState.NEEDS_INPUT
            ):
                label = "WAITING FOR INPUT"
            elif notice.warning:
                label = "WARNINGS"
            elif session.runtime is RuntimeState.ATTACHED:
                label = "ATTACHED"
            elif session.runtime in {RuntimeState.STOPPED, RuntimeState.FAILED}:
                label = "STOPPED"
            else:
                label = "DETACHED"
        elif grouping == "runtime":
            label = (
                "UNMANAGED"
                if not session.owned
                else "ATTACHED"
                if session.runtime is RuntimeState.ATTACHED
                else "STOPPED"
                if session.runtime in {RuntimeState.STOPPED, RuntimeState.FAILED}
                else "DETACHED"
            )
        elif grouping == "agent":
            label = "UNMANAGED" if not session.owned else TOOL_LABELS[session.tool].upper()
        elif grouping == "project":
            label = "NO PROJECT" if not session.project else session.project.upper()
        elif grouping == "warning":
            label = "WARNINGS" if notice.warning else "CLEAR"
        else:
            activity = session.last_active_at
            if activity is None:
                label = "NO RECORDED ACTIVITY"
            else:
                if activity.tzinfo is None:
                    activity = activity.replace(tzinfo=UTC)
                elapsed = current - activity
                label = (
                    "ACTIVE NOW"
                    if elapsed <= timedelta(hours=1)
                    else "ACTIVE TODAY"
                    if elapsed <= RECENT_WINDOW
                    else "EARLIER"
                )
        add(label, session)

    for items in buckets.values():
        items.sort(key=lambda item: (not item.pinned, item.display_name or item.name))
    return [(label, buckets[label]) for label in order]


ACTIVITY_HISTORY_LENGTH = 8
# A single completed scan cycle isn't a meaningful trend yet, and showing a
# spark the instant one sample lands would make its appearance depend on
# background scan timing rather than actual accumulated history.
ACTIVITY_SPARK_MIN_SAMPLES = 3
SPARKLINE_GLYPHS = "▁▂▃▄▅▆▇█"
SPARKLINE_GLYPHS_ASCII = "_.-:=+*#"


def sparkline(history: deque[int] | None, *, ascii_only: bool = False) -> str:
    """Render a small bar-chart of recent activity magnitude, one glyph per sample."""
    if not history:
        return ""
    glyphs = SPARKLINE_GLYPHS_ASCII if ascii_only else SPARKLINE_GLYPHS
    peak = max(history)
    if peak <= 0:
        return glyphs[0] * len(history)
    chars = []
    for value in history:
        level = min(len(glyphs) - 1, int((value / peak) * (len(glyphs) - 1)))
        chars.append(glyphs[level])
    return "".join(chars)


def status_badge(
    label: str,
    *,
    kind: str,
    ascii_only: bool,
    monochrome: bool,
) -> Text:
    """Return a text-plus-symbol status badge that remains meaningful without colour."""
    symbols = {
        "active": ("●", "[A]"),
        "info": ("▸", "[>]"),
        "waiting": ("?", "[?]"),
        "warning": ("!", "[!]"),
        "error": ("✕", "[X]"),
        "inactive": ("○", "[-]"),
        "agent": ("◆", "[+]"),
    }
    styles = {
        "active": "green",
        "info": "cyan",
        "waiting": "yellow",
        "warning": "yellow",
        "error": "red",
        "inactive": "dim",
        "agent": "#c792ea",
    }
    marker = symbols[kind][1 if ascii_only else 0]
    style = "bold" if monochrome else f"bold {styles[kind]}"
    return Text.assemble((f"{marker} {label}", style))


def session_status_badges(
    session: SessionView,
    notice: ActivityNotice,
    *,
    ascii_only: bool,
    monochrome: bool,
) -> Text:
    """Compose concise state badges for session rows and inspector summaries."""
    result = Text()
    runtime_kind = (
        "active"
        if session.runtime is RuntimeState.ATTACHED
        else "error"
        if session.runtime is RuntimeState.FAILED
        else "inactive"
    )
    result.append_text(
        status_badge(
            display_state(session.runtime.value),
            kind=runtime_kind,
            ascii_only=ascii_only,
            monochrome=monochrome,
        )
    )
    if session.input_state is InputState.REQUIRED:
        result.append("  ")
        result.append_text(
            status_badge(
                "Input required", kind="waiting", ascii_only=ascii_only, monochrome=monochrome
            )
        )
    elif notice.warning:
        result.append("  ")
        result.append_text(
            status_badge(
                notice.title, kind=notice.level, ascii_only=ascii_only, monochrome=monochrome
            )
        )
    elif session.task_state is TaskState.COMPLETED:
        result.append("  ")
        result.append_text(
            status_badge("Completed", kind="active", ascii_only=ascii_only, monochrome=monochrome)
        )
    elif session.task_state is TaskState.IN_PROGRESS:
        result.append("  ")
        result.append_text(
            status_badge("In progress", kind="info", ascii_only=ascii_only, monochrome=monochrome)
        )
    return result


def session_row(
    session: SessionView,
    width: int,
    *,
    ascii_only: bool = False,
    notice: ActivityNotice | None = None,
    pulse_dim: bool = False,
    warning_color: str = "yellow",
    warning_dim_color: str = "#8a7a2a",
    monochrome: bool = False,
    activity_spark: str = "",
    bulk_mode: bool = False,
    marked: bool = False,
) -> Text:
    """Build a stable two-line row sized to its current pane."""
    alert = notice or detect_activity(session, "")
    marker = ("*" if ascii_only else "★") if session.pinned else "!" if alert.warning else " "
    tool = session.tool.value.upper()
    when = relative_activity(session.last_active_at)
    checkbox_width = 4 if bulk_mode else 0
    name_width = max(10, width - len(tool) - len(when) - 7 - checkbox_width)
    name = truncate(session.name, name_width, ascii_only=ascii_only)
    first = Text()
    if bulk_mode:
        checkbox = "[x] " if marked else "[ ] "
        first.append(checkbox, style="bold" if marked else "dim")
    marker_style = (
        ""
        if marker == " "
        else warning_dim_color
        if (marker == "!" and pulse_dim)
        else warning_color
    )
    first.append(f"{marker} ", style=marker_style)
    first.append(f"{tool:<7}", style=tool_style(session.tool, monochrome=monochrome))
    first.append(f"{name:<{name_width}} ", style="bold")
    first.append(when, style="dim")
    separator = " / " if ascii_only else " · "
    statuses = [display_state(session.task_state.value)]
    if not session.owned:
        statuses.append("Unmanaged")
    second_value = truncate(separator.join(statuses), max(12, width - 3), ascii_only=ascii_only)
    first.append("\n  ")
    first.append_text(
        session_status_badges(session, alert, ascii_only=ascii_only, monochrome=monochrome)
    )
    if second_value:
        first.append(f"  {second_value}", style="dim")
    if activity_spark:
        first.append("  ")
        first.append(activity_spark, style="dim")
    return first


def _format_marked_names(names: Sequence[str], *, limit: int = 5) -> str:
    shown = ", ".join(names[:limit])
    remaining = len(names) - limit
    return shown + (f", and {remaining} more" if remaining > 0 else "")


def section_title(label: str) -> Text:
    return Text(label, style="bold dim")


def labeled_values(values: list[tuple[str, str]]) -> Text:
    result = Text()
    for label, value in values:
        if not value:
            continue
        result.append(f"{label:<14}", style="dim")
        result.append(value)
        result.append("\n")
    return result


def animate_modal_open(screen: Screen[Any]) -> None:
    """Apply one restrained modal transition when motion is enabled."""
    motion = getattr(screen.app, "motion", "off")
    if motion == "off":
        return
    dialog = screen.query_one(".dialog")
    duration = 0.18 if motion == "full" else 0.14
    dialog.styles.offset = (0, 0)
    final_offset = dialog.styles.offset
    dialog.styles.opacity = 0.0
    dialog.styles.offset = (0, 1)
    screen.call_after_refresh(dialog.styles.animate, "opacity", 1.0, duration=duration)
    screen.call_after_refresh(dialog.styles.animate, "offset", final_offset, duration=duration)


def animate_modal_shake(screen: Screen[Any]) -> None:
    """Keep validation feedback stable; forms already render focused error text."""
    del screen


@dataclass(frozen=True, slots=True)
class OrganizationEditResult:
    name: str
    display_name: str
    project: str
    tags: list[str]


@dataclass(frozen=True, slots=True)
class StatusEditResult:
    task_state: TaskState
    input_state: InputState


@dataclass(frozen=True, slots=True)
class ManageAction:
    action_id: str
    category: str
    label: str
    description: str
    shortcut: str
    enabled: bool = True
    disabled_reason: str = ""
    destructive: bool = False


@dataclass(frozen=True, slots=True)
class ManageListState:
    query: str = ""
    highlighted_action: str = "identity"
    scroll_y: int = 0


@dataclass(frozen=True, slots=True)
class ManageSelection:
    action: str
    state: ManageListState


@dataclass(frozen=True, slots=True)
class CreateFormResult:
    request: CreateRequest
    start_attached: bool = False


class CreateSessionScreen(ModalScreen[CreateFormResult | None]):
    BINDINGS: ClassVar[list[BindingSpec]] = [
        Binding("escape", "cancel", "Cancel"),
        Binding("ctrl+enter", "submit", "Create"),
    ]

    def __init__(
        self,
        default_cwd: Path,
        service: SessionService | None = None,
        default_tool: Tool = Tool.CLAUDE,
        template: SessionView | None = None,
    ) -> None:
        super().__init__()
        self.default_cwd = default_cwd
        self.service = service
        self.default_tool = default_tool
        self.template = template
        self._detected_project = ""
        self._touched: set[str] = set()
        self._advanced = False
        self._advanced_focus_id: str | None = None
        self._validation_timer: Timer | None = None
        self._validation_serial = 0
        self._validated_signature: tuple[object, ...] | None = None
        self._validated_request: CreateRequest | None = None
        self._normalized_name = ""
        self._project_user_edited = False

    def _recent_directories(self) -> list[tuple[str, str]]:
        directories = [self.default_cwd]
        if self.service:
            for session in self.service.list_sessions():
                if session.cwd not in directories:
                    directories.append(session.cwd)
        repository_roots = {Path.home() / "workspace" / "projects"}
        try:
            self.default_cwd.parent.relative_to(Path.home())
        except ValueError:
            pass
        else:
            repository_roots.add(self.default_cwd.parent)
        detected_repositories: list[Path] = []
        for root in repository_roots:
            try:
                children = sorted(root.iterdir())
            except OSError:
                continue
            for child in children:
                try:
                    is_repository = child.is_dir() and (child / ".git").exists()
                except OSError:
                    continue
                if is_repository and child not in directories:
                    detected_repositories.append(child)
        options: list[tuple[str, str]] = []
        for index, path in enumerate(directories[:8]):
            kind = "Current directory" if index == 0 else "Recently used"
            options.append((f"{kind}: {display_path(path)}", str(path)))
        for path in detected_repositories[:5]:
            options.append((f"Git repository: {display_path(path)}", str(path)))
        options.append(("Browse or enter another path", "__browse__"))
        return options

    def compose(self) -> ComposeResult:
        disclosure = ">" if getattr(self.app, "ascii_only", False) else "▸"
        with Vertical(id="create-dialog", classes="dialog create-dialog"):
            yield Label("Create Session", classes="dialog-title")
            with Vertical(id="create-basic"):
                if self.service and self.service.list_presets():
                    with Horizontal(classes="form-row"):
                        yield Label("Preset", classes="field-label")
                        yield Select(
                            [(preset.name, preset.name) for preset in self.service.list_presets()],
                            prompt="Load preset (optional)",
                            allow_blank=True,
                            compact=True,
                            id="create-preset",
                        )
                with Horizontal(classes="form-row"):
                    yield Label("Tool", classes="field-label")
                    yield Select(
                        [(TOOL_LABELS[tool], tool.value) for tool in Tool],
                        value=self.default_tool.value,
                        allow_blank=False,
                        compact=True,
                        id="create-tool",
                    )
                yield Static("", id="create-tool-status", classes="field-status")
                with Horizontal(classes="form-row"):
                    yield Label("Session name", classes="field-label")
                    yield Input(
                        placeholder="api_refactor",
                        compact=True,
                        max_length=200,
                        id="create-name",
                    )
                yield Static("", id="create-name-status", classes="field-status")
                with Horizontal(classes="form-row task-row"):
                    yield Label("Task", classes="field-label")
                    yield TextArea(
                        placeholder="Improve API authentication",
                        compact=True,
                        soft_wrap=True,
                        tab_behavior="focus",
                        id="create-note",
                    )
                with Horizontal(classes="form-row"):
                    yield Label("Working directory", classes="field-label")
                    yield Input(value=display_path(self.default_cwd), compact=True, id="create-cwd")
                    yield Select(
                        self._recent_directories(),
                        prompt="Recent",
                        allow_blank=True,
                        compact=True,
                        id="create-recent-dir",
                    )
                yield Static("", id="create-cwd-status", classes="field-status")
                with Horizontal(classes="form-row"):
                    yield Label("Project", classes="field-label")
                    yield Input(
                        placeholder="Optional",
                        compact=True,
                        max_length=200,
                        id="create-project",
                    )
                    yield Button("Use home workspace", id="create-home-project", compact=True)
                yield Static("", id="create-project-status", classes="field-status")
                with Horizontal(id="logging-row"):
                    yield Label("Logging", classes="field-label")
                    yield Switch(value=True, id="create-logging")
                    yield Static("Enabled", id="logging-state")
                yield Static("", id="logging-hint")
                yield Button(
                    f"{disclosure} Advanced options",
                    id="create-advanced-toggle",
                    compact=True,
                )
            with (
                VerticalScroll(id="create-advanced", classes="collapsed"),
                Vertical(id="create-advanced-content"),
            ):
                with Horizontal(classes="form-row"):
                    yield Label("Tags", classes="field-label")
                    yield Input(placeholder="backend, urgent", compact=True, id="create-tags")
                yield Static("", id="create-tags-status", classes="field-status")
                with Horizontal(classes="form-row"):
                    yield Label("Command arguments", classes="field-label")
                    yield Input(
                        value="From tool configuration",
                        compact=True,
                        disabled=True,
                        id="create-command-args",
                    )
                yield Static(
                    "Edit the tool profile in config.toml to change arguments.",
                    classes="field-help",
                )
                with Horizontal(classes="form-row"):
                    yield Label("Environment", classes="field-label")
                    yield Input(
                        value="Default profile",
                        compact=True,
                        disabled=True,
                        id="create-environment",
                    )
                with Horizontal(classes="form-row"):
                    yield Label("Log retention", classes="field-label")
                    yield Input(
                        value="Size-limited",
                        compact=True,
                        disabled=True,
                        id="create-retention",
                    )
                with Horizontal(classes="form-row"):
                    yield Label("Executable", classes="field-label")
                    yield Input(compact=True, disabled=True, id="create-executable")
                with Horizontal(classes="form-row"):
                    yield Label("tmux window", classes="field-label")
                    yield Input(value="main", compact=True, disabled=True, id="create-window-name")
                with Horizontal(classes="form-row"):
                    yield Label("Initial status", classes="field-label")
                    yield Select(
                        [(display_state(state.value), state.value) for state in TaskState],
                        value=TaskState.IN_PROGRESS.value,
                        allow_blank=False,
                        compact=True,
                        id="create-task-state",
                    )
                with Horizontal(classes="form-row"):
                    yield Label("Startup", classes="field-label")
                    yield Select(
                        [("Start detached", "detached"), ("Start and attach", "attached")],
                        value="detached",
                        allow_blank=False,
                        compact=True,
                        id="create-startup",
                    )
                with Horizontal(classes="form-row"):
                    yield Label("Tool prefix", classes="field-label")
                    yield Switch(value=True, id="create-prefix")
                    yield Static("Automatic", id="prefix-state")
            with Horizontal(classes="preview-row"):
                yield Label("Command", classes="field-label")
                yield Static("", id="command-preview")
            yield Static(
                "FORM  Tab Next   Shift+Tab Previous   Ctrl+Enter Create   Esc Cancel",
                id="create-form-help",
            )
            yield Static("", id="create-summary")
            yield Static("Enter a session name to continue.", id="create-submit-reason")
            with Horizontal(classes="dialog-actions"):
                yield Button("Cancel", id="create-cancel")
                yield Button("Create Session", variant="primary", id="create-submit", disabled=True)

    def on_mount(self) -> None:
        animate_modal_open(self)
        self.set_class(self.size.width < 100, "narrow-form")
        self.set_class(self.size.width < 120 or self.size.height < 35, "compact-form")
        self._touched.update(("tool", "cwd"))
        log_root = "ws state/logs"
        if self.service:
            try:
                self.service.paths.logs_dir.relative_to(Path.home())
            except ValueError:
                log_root = "$WS_STATE/logs"
            else:
                log_root = display_path(self.service.paths.logs_dir)
        self.query_one("#logging-hint", Static).update(
            f"Output is sanitized, owner-only, and size-limited.\nStorage: {log_root}/"
        )
        if self.template is not None:
            self._apply_template(
                tool=self.template.tool,
                cwd=self.template.cwd,
                project=self.template.project,
                tags=self.template.tags,
                logging_enabled=self.template.logging_enabled,
            )
        self._schedule_validation()
        self.query_one("#create-name", Input).focus()

    def on_resize(self, event: events.Resize) -> None:
        self.set_class(event.size.width < 100, "narrow-form")
        self.set_class(event.size.width < 120 or event.size.height < 35, "compact-form")

    def _parse_tags(self) -> list[str]:
        value = self.query_one("#create-tags", Input).value
        return normalize_tags([item for item in re.split(r"[\s,]+", value) if item])

    def _signature(self) -> tuple[object, ...]:
        return (
            self.query_one("#create-tool", Select).value,
            self.query_one("#create-name", Input).value,
            self.query_one("#create-cwd", Input).value,
            self.query_one("#create-project", Input).value,
            self.query_one("#create-note", TextArea).text,
            self.query_one("#create-tags", Input).value,
            self.query_one("#create-logging", Switch).value,
            self.query_one("#create-prefix", Switch).value,
            self.query_one("#create-task-state", Select).value,
            self.query_one("#create-startup", Select).value,
        )

    def _schedule_validation(self) -> None:
        self._validation_serial += 1
        if self._validation_timer is not None:
            self._validation_timer.stop()
        self._validated_signature = None
        self._validated_request = None
        self.query_one("#create-submit", Button).disabled = True
        for field in ("name", "cwd"):
            if field in self._touched:
                self._render_field_status(f"#create-{field}-status", "checking", "Checking...")
        self._render_readiness()
        serial = self._validation_serial
        self._validation_timer = self.set_timer(0.2, lambda: self._validate(serial))

    def _validate(self, serial: int | None = None) -> None:
        if serial is not None and serial != self._validation_serial:
            return
        signature = self._signature()
        tool = Tool(str(self.query_one("#create-tool", Select).value))
        cwd = Path(self.query_one("#create-cwd", Input).value).expanduser()
        name = self.query_one("#create-name", Input).value.strip()
        automatic_prefix = self.query_one("#create-prefix", Switch).value
        if self.service:
            validation = self.service.validate_create(
                tool, name, cwd, automatic_prefix=automatic_prefix
            )
            command = validation.command
            name_error = validation.name_error
            cwd_error = validation.cwd_error
            tool_error = validation.tool_error
            detected_project = validation.detected_project
            resolved_cwd = validation.cwd
            normalized = validation.normalized_name
        else:
            try:
                normalized = normalized_session_name(tool, name, automatic_prefix=automatic_prefix)
                name_error = ""
            except WsError as error:
                normalized = ""
                name_error = str(error)
            resolved_cwd = cwd.resolve() if cwd.is_dir() else None
            cwd_error = "" if resolved_cwd else f"working directory does not exist: {cwd}"
            tool_error = ""
            detected_project = ""
            command = (tool.value,)

        project_input = self.query_one("#create-project", Input)
        if not self._project_user_edited and project_input.value in ("", self._detected_project):
            self._detected_project = detected_project
            with self.prevent(Input.Changed):
                project_input.value = detected_project
        self.query_one("#create-home-project", Button).display = bool(
            resolved_cwd and resolved_cwd == Path.home().resolve()
        )
        project_status = self.query_one("#create-project-status", Static)
        project_status.update(
            f"Detected project: {detected_project}" if detected_project else "Project not detected"
        )
        project_status.set_class(bool(detected_project), "valid")

        tag_error = ""
        try:
            tags = self._parse_tags()
        except ValueError as validation_error:
            tags = []
            tag_error = str(validation_error)

        self.query_one("#command-preview", Static).update(shlex.join(command))
        with self.prevent(Input.Changed):
            self.query_one("#create-executable", Input).value = command[0] if command else ""
        signature = self._signature()
        self._normalized_name = normalized
        self._render_name_status(name, normalized, name_error)
        self._render_field_status(
            "#create-cwd-status",
            "invalid" if cwd_error else "valid",
            cwd_error or f"Directory exists: {display_path(resolved_cwd or cwd)}",
            visible="cwd" in self._touched,
        )
        self._render_field_status(
            "#create-tool-status",
            "invalid" if tool_error else "valid",
            tool_error or "Available",
        )
        self._render_field_status(
            "#create-tags-status",
            "invalid" if tag_error else "valid",
            tag_error or "Tags are valid",
            visible="tags" in self._touched,
        )
        errors = [issue for issue in (name_error, cwd_error, tool_error, tag_error) if issue]
        request: CreateRequest | None = None
        if not errors and resolved_cwd is not None:
            try:
                request = CreateRequest(
                    name=name,
                    display_name=name,
                    tool=tool,
                    cwd=resolved_cwd,
                    project=project_input.value.strip(),
                    note=self.query_one("#create-note", TextArea).text.strip(),
                    tags=tags,
                    task_state=TaskState(str(self.query_one("#create-task-state", Select).value)),
                    logging_enabled=self.query_one("#create-logging", Switch).value,
                    automatic_prefix=automatic_prefix,
                )
            except ValueError as validation_error:
                errors.append(str(validation_error))
        if signature == self._signature():
            self._validated_signature = signature
            self._validated_request = request
            self.query_one("#create-submit", Button).disabled = request is None
        self._render_readiness(errors)

    def _render_field_status(
        self,
        selector: str,
        state: str,
        message: str,
        *,
        visible: bool = True,
    ) -> None:
        target = self.query_one(selector, Static)
        target.display = visible
        target.remove_class("valid", "warning", "invalid", "checking")
        if not visible:
            target.update("")
            return
        markers = {"valid": "+", "warning": "!", "invalid": "x", "checking": "..."}
        target.update(f"{markers.get(state, '')} {message}".strip())
        target.add_class(state)

    def _render_name_status(self, entered: str, normalized: str, error: str) -> None:
        if "name" not in self._touched:
            self._render_field_status("#create-name-status", "neutral", "", visible=False)
            return
        if error:
            self._render_field_status("#create-name-status", "invalid", error)
            return
        message = Text()
        changed = entered.strip() != normalized
        if changed:
            message.append("! Normalized for tmux/ws\n", "#e9b44c")
        message.append(f"Display name  {entered.strip()}\n", "dim")
        message.append(f"+ Available as {normalized}", "#72c78e")
        target = self.query_one("#create-name-status", Static)
        target.display = True
        target.remove_class("valid", "warning", "invalid", "checking")
        target.add_class("warning" if changed else "valid")
        target.update(message)

    def _render_readiness(self, errors: list[str] | None = None) -> None:
        summary = self.query_one("#create-summary", Static)
        reason = self.query_one("#create-submit-reason", Static)
        request = self._validated_request
        if request is not None and self._normalized_name:
            summary.update(
                labeled_values(
                    [
                        ("Ready", "to create"),
                        ("Tool", TOOL_LABELS[request.tool]),
                        ("Session", self._normalized_name),
                        ("Directory", display_path(request.cwd)),
                        ("Command", str(self.query_one("#command-preview", Static).content)),
                        ("Logging", "Enabled" if request.logging_enabled else "Disabled"),
                    ]
                )
            )
            summary.display = True
            reason.display = False
            return
        summary.display = False
        reason.display = True
        reason.remove_class("invalid")
        if "name" not in self._touched or not self.query_one("#create-name", Input).value:
            reason.update("Enter a session name to continue.")
        elif errors:
            reason.update(f"Create Session unavailable: {errors[0]}")
            reason.add_class("invalid")
        else:
            reason.update("Checking validation...")

    @on(Select.Changed, "#create-tool")
    def tool_changed(self) -> None:
        self._touched.add("tool")
        self._schedule_validation()

    @on(Select.Changed, "#create-recent-dir")
    def recent_directory_changed(self, event: Select.Changed) -> None:
        if event.value is not Select.NULL:
            if str(event.value) == "__browse__":
                self.query_one("#create-cwd", Input).focus()
                self.query_one("#create-recent-dir", Select).value = Select.NULL
                return
            selected = display_path(Path(str(event.value)))
            if self.query_one("#create-cwd", Input).value == selected:
                return
            self._touched.add("cwd")
            self.query_one("#create-cwd", Input).value = selected
            self.query_one("#create-recent-dir", Select).value = Select.NULL

    @on(Select.Changed, "#create-preset")
    def preset_selected(self, event: Select.Changed) -> None:
        if event.value is Select.NULL or not self.service:
            return
        preset = next(
            (item for item in self.service.list_presets() if item.name == str(event.value)),
            None,
        )
        self.query_one("#create-preset", Select).value = Select.NULL
        if preset is None:
            return
        self._apply_template(
            tool=preset.tool,
            cwd=preset.cwd,
            project=preset.project,
            tags=preset.tags,
            logging_enabled=preset.logging_enabled,
        )

    def _apply_template(
        self,
        *,
        tool: Tool,
        cwd: Path,
        project: str,
        tags: Sequence[str],
        logging_enabled: bool,
    ) -> None:
        self.query_one("#create-tool", Select).value = tool.value
        self._touched.add("tool")
        self.query_one("#create-cwd", Input).value = display_path(cwd)
        self._touched.add("cwd")
        self.query_one("#create-project", Input).value = project
        self._project_user_edited = True
        self.query_one("#create-tags", Input).value = ", ".join(tags)
        self._touched.add("tags")
        self.query_one("#create-logging", Switch).value = logging_enabled
        self.query_one("#logging-state", Static).update(
            "Enabled" if logging_enabled else "Disabled"
        )
        self._schedule_validation()

    @on(Select.Changed, "#create-task-state, #create-startup")
    def advanced_select_changed(self) -> None:
        self._schedule_validation()

    @on(Input.Changed)
    def input_changed(self, event: Input.Changed) -> None:
        if event.input.id in {"create-name", "create-cwd", "create-project", "create-tags"}:
            if event.input.id == "create-name":
                self._touched.add("name")
            elif event.input.id == "create-cwd":
                self._touched.add("cwd")
            elif event.input.id == "create-tags":
                self._touched.add("tags")
            elif event.input.id == "create-project":
                self._project_user_edited = True
            self._schedule_validation()

    @on(TextArea.Changed, "#create-note")
    def task_changed(self) -> None:
        self._schedule_validation()

    @on(Switch.Changed, "#create-logging, #create-prefix")
    def switch_changed(self, event: Switch.Changed) -> None:
        if event.switch.id == "create-logging":
            self.query_one("#logging-state", Static).update(
                "Enabled" if event.value else "Disabled"
            )
        else:
            self.query_one("#prefix-state", Static).update(
                "Automatic" if event.value else "Disabled"
            )
        self._schedule_validation()

    @on(Button.Pressed)
    def button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "create-cancel":
            self.dismiss(None)
            return
        if event.button.id == "create-advanced-toggle":
            self._set_advanced(not self._advanced)
            return
        if event.button.id == "create-home-project":
            self.query_one("#create-project", Input).value = "Home workspace"
            self._project_user_edited = True
            return
        if event.button.id != "create-submit":
            return
        self.action_submit()

    def action_submit(self) -> None:
        button = self.query_one("#create-submit", Button)
        if button.disabled or self._validated_signature != self._signature():
            animate_modal_shake(self)
            return
        request = self._validated_request
        if request is None:
            return
        self.dismiss(
            CreateFormResult(
                request=request,
                start_attached=str(self.query_one("#create-startup", Select).value) == "attached",
            )
        )

    def _set_advanced(self, expanded: bool) -> None:
        focused = self.app.focused
        advanced = self.query_one("#create-advanced", VerticalScroll)
        if not expanded and focused is not None and advanced in focused.ancestors:
            self._advanced_focus_id = focused.id
        self._advanced = expanded
        advanced.set_class(not expanded, "collapsed")
        self.query_one("#create-dialog").set_class(expanded, "advanced")
        toggle = self.query_one("#create-advanced-toggle", Button)
        if getattr(self.app, "ascii_only", False):
            toggle.label = "v Advanced options" if expanded else "> Advanced options"
        else:
            toggle.label = "▾ Advanced options" if expanded else "▸ Advanced options"
        if not expanded and focused is not None and advanced in focused.ancestors:
            toggle.focus()
        elif expanded and self._advanced_focus_id:
            self.call_after_refresh(self.query_one(f"#{self._advanced_focus_id}").focus)

    def action_cancel(self) -> None:
        if self._advanced:
            self._set_advanced(False)
            self.query_one("#create-advanced-toggle", Button).focus()
            return
        self.dismiss(None)


class CreateFailureScreen(ModalScreen[str | None]):
    BINDINGS: ClassVar[list[BindingSpec]] = [Binding("escape", "close", "Close")]

    def __init__(self, session_name: str, error: str, *, metadata_exists: bool) -> None:
        super().__init__()
        self.session_name = session_name
        self.error = error
        self.metadata_exists = metadata_exists

    def compose(self) -> ComposeResult:
        with Vertical(id="create-failure-dialog", classes="dialog small-dialog danger-dialog"):
            yield Label("Session Startup Failed", classes="dialog-title danger-title")
            yield Static(
                f"{self.session_name} was not started.\n\n{self.error}",
                classes="confirm-copy",
            )
            yield Static(
                "ws preserved the validated request. Retry after correcting the environment, "
                "or inspect the failure details.",
                classes="dialog-context",
            )
            with Horizontal(classes="dialog-actions failure-actions"):
                yield Button("Retry", variant="primary", id="create-failure-retry")
                yield Button("Open Details", id="create-failure-details")
                yield Button(
                    "Remove Metadata",
                    variant="error",
                    id="create-failure-remove",
                    disabled=not self.metadata_exists,
                )
                yield Button("Close", id="create-failure-close")

    def on_mount(self) -> None:
        animate_modal_open(self)
        self.query_one("#create-failure-close", Button).focus()

    @on(Button.Pressed)
    def button_pressed(self, event: Button.Pressed) -> None:
        actions = {
            "create-failure-retry": "retry",
            "create-failure-details": "details",
            "create-failure-remove": "remove",
            "create-failure-close": "close",
        }
        if event.button.id in actions:
            self.dismiss(actions[event.button.id])

    def action_close(self) -> None:
        self.dismiss("close")


class IdentityOrganizationScreen(ModalScreen[OrganizationEditResult | None]):
    BINDINGS: ClassVar[list[BindingSpec]] = [
        Binding("escape", "cancel", "Cancel"),
        Binding("ctrl+enter", "submit", "Save"),
    ]

    def __init__(self, service: SessionService, session: SessionView) -> None:
        super().__init__()
        self.service = service
        self.session = session
        self._validation_timer: Timer | None = None
        self._validation_serial = 0
        self._validated_result: OrganizationEditResult | None = None

    def compose(self) -> ComposeResult:
        with Vertical(id="identity-dialog", classes="dialog identity-dialog"):
            yield Label("Identity & Organization", classes="dialog-title")
            yield Static(self.session.name, classes="dialog-context")
            yield Label("Display name", classes="field-label")
            yield Input(
                value=self.session.display_name or self.session.name,
                max_length=200,
                id="identity-display-name",
            )
            yield Static("", id="identity-display-status", classes="field-status")
            yield Label("Session ID", classes="field-label")
            yield Input(value=self.session.name, max_length=200, id="identity-name")
            yield Static("", id="identity-name-status", classes="field-status")
            yield Label("Project", classes="field-label")
            yield Input(value=self.session.project, max_length=200, id="identity-project")
            yield Label("Tags", classes="field-label")
            yield Input(value=", ".join(self.session.tags), id="identity-tags")
            yield Static("", id="identity-tags-status", classes="field-status")
            yield Static(
                "FORM  Tab Next   Shift+Tab Previous   Ctrl+Enter Save   Esc Cancel",
                classes="mode-help",
            )
            with Horizontal(classes="dialog-actions"):
                yield Button("Cancel", id="identity-cancel")
                yield Button("Save", variant="primary", id="identity-submit", disabled=True)

    def on_mount(self) -> None:
        animate_modal_open(self)
        self._schedule_validation()
        self.query_one("#identity-display-name", Input).focus()

    def _signature(self) -> tuple[str, str, str, str]:
        return (
            self.query_one("#identity-name", Input).value,
            self.query_one("#identity-display-name", Input).value,
            self.query_one("#identity-project", Input).value,
            self.query_one("#identity-tags", Input).value,
        )

    def _schedule_validation(self) -> None:
        self._validation_serial += 1
        if self._validation_timer is not None:
            self._validation_timer.stop()
        self._validated_result = None
        self.query_one("#identity-submit", Button).disabled = True
        status = self.query_one("#identity-name-status", Static)
        status.update("... Checking...")
        status.remove_class("valid", "warning", "invalid")
        status.add_class("checking")
        serial = self._validation_serial
        self._validation_timer = self.set_timer(0.2, lambda: self._validate(serial))

    def _validate(self, serial: int) -> None:
        if serial != self._validation_serial:
            return
        signature = self._signature()
        requested_name, display_name, project, raw_tags = signature
        try:
            validation = self.service.validate_rename(self.session.name, requested_name.strip())
        except WsError as error:
            normalized = ""
            name_error = str(error)
        else:
            normalized = validation.normalized_name
            name_error = validation.name_error
        try:
            tags = normalize_tags([item for item in re.split(r"[\s,]+", raw_tags) if item])
        except ValueError as error:
            tags = []
            tags_error = str(error)
        else:
            tags_error = ""
        metadata_error = ""
        if not display_name.strip():
            metadata_error = "display name is required"
        elif len(project.strip()) > 200:
            metadata_error = "project must be 200 characters or fewer"

        name_status = self.query_one("#identity-name-status", Static)
        name_status.remove_class("checking", "valid", "warning", "invalid")
        if name_error:
            name_status.update(f"x {name_error}")
            name_status.add_class("invalid")
        elif normalized == self.session.name:
            name_status.update("+ Session ID unchanged")
            name_status.add_class("valid")
        else:
            name_status.update(f"! tmux/ws session will be renamed to {normalized}")
            name_status.add_class("warning")

        tags_status = self.query_one("#identity-tags-status", Static)
        tags_status.remove_class("valid", "invalid")
        if tags_error:
            tags_status.update(f"x {tags_error}")
            tags_status.add_class("invalid")
        else:
            tags_status.update("+ Tags are valid" if tags else "")
            tags_status.add_class("valid")

        display_status = self.query_one("#identity-display-status", Static)
        display_status.remove_class("valid", "invalid")
        if metadata_error:
            display_status.update(f"x {metadata_error}")
            display_status.add_class("invalid")
        else:
            display_status.update("+ Display name is valid")
            display_status.add_class("valid")

        if not name_error and not tags_error and not metadata_error and normalized:
            self._validated_result = OrganizationEditResult(
                name=normalized,
                display_name=display_name.strip(),
                project=project.strip(),
                tags=tags,
            )
        if signature == self._signature():
            self.query_one("#identity-submit", Button).disabled = self._validated_result is None

    @on(Input.Changed)
    def input_changed(self) -> None:
        self._schedule_validation()

    @on(Button.Pressed)
    def button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "identity-cancel":
            self.dismiss(None)
        elif event.button.id == "identity-submit":
            self.action_submit()

    def action_submit(self) -> None:
        if self.query_one("#identity-submit", Button).disabled:
            animate_modal_shake(self)
            return
        if self._validated_result is not None:
            self.dismiss(self._validated_result)

    def action_cancel(self) -> None:
        self.dismiss(None)


EditSessionScreen = IdentityOrganizationScreen


class NoteScreen(ModalScreen[str | None]):
    BINDINGS: ClassVar[list[BindingSpec]] = [
        Binding("escape", "cancel", "Cancel"),
        Binding("ctrl+enter", "submit", "Save"),
    ]

    def __init__(self, session: SessionView) -> None:
        super().__init__()
        self.session = session

    def compose(self) -> ComposeResult:
        with Vertical(id="note-dialog", classes="dialog task-dialog"):
            yield Label("Edit Task", classes="dialog-title")
            yield Static(self.session.name, classes="dialog-context")
            yield TextArea(
                self.session.note,
                soft_wrap=True,
                tab_behavior="focus",
                id="note-value",
            )
            yield Static("", id="note-status", classes="field-status")
            yield Static(
                "FORM  Enter New line   Ctrl+Enter Save   Esc Cancel",
                classes="mode-help",
            )
            with Horizontal(classes="dialog-actions"):
                yield Button("Cancel", id="note-cancel")
                yield Button("Save", variant="primary", id="note-submit")

    def on_mount(self) -> None:
        animate_modal_open(self)
        self.query_one("#note-value", TextArea).focus()
        self._validate()

    @on(TextArea.Changed, "#note-value")
    def task_changed(self) -> None:
        self._validate()

    def _validate(self) -> None:
        note = self.query_one("#note-value", TextArea).text
        status = self.query_one("#note-status", Static)
        button = self.query_one("#note-submit", Button)
        if len(note) > 2000:
            status.update(f"x Task is {len(note) - 2000} characters over the limit")
            status.add_class("invalid")
            button.disabled = True
        else:
            status.remove_class("invalid")
            status.update(f"{len(note)}/2000 characters")
            button.disabled = False

    @on(Button.Pressed)
    def button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "note-cancel":
            self.dismiss(None)
        elif event.button.id == "note-submit":
            self.action_submit()

    def action_submit(self) -> None:
        if self.query_one("#note-submit", Button).disabled:
            animate_modal_shake(self)
            return
        self.dismiss(self.query_one("#note-value", TextArea).text)

    def action_cancel(self) -> None:
        self.dismiss(None)


class StatusScreen(ModalScreen[StatusEditResult | None]):
    BINDINGS: ClassVar[list[BindingSpec]] = [
        Binding("escape", "cancel", "Cancel"),
        Binding("ctrl+enter", "submit", "Save"),
    ]

    def __init__(self, session: SessionView) -> None:
        super().__init__()
        self.session = session

    def compose(self) -> ComposeResult:
        with Vertical(id="status-dialog", classes="dialog small-dialog status-dialog"):
            yield Label("Task & Input Status", classes="dialog-title")
            yield Static(self.session.name, classes="dialog-context")
            yield Label("Task state", classes="field-label")
            yield Select(
                [(display_state(state.value), state.value) for state in TaskState],
                value=self.session.task_state.value,
                allow_blank=False,
                id="status-task-state",
            )
            yield Label("User input", classes="field-label")
            yield Select(
                [(display_input(state), state.value) for state in InputState],
                value=self.session.input_state.value,
                allow_blank=False,
                id="status-input-state",
            )
            yield Static(
                "FORM  Tab Next   Shift+Tab Previous   Ctrl+Enter Save   Esc Cancel",
                classes="mode-help",
            )
            with Horizontal(classes="dialog-actions"):
                yield Button("Cancel", id="status-cancel")
                yield Button("Save", variant="primary", id="status-submit")

    def on_mount(self) -> None:
        animate_modal_open(self)
        self.query_one("#status-task-state", Select).focus()

    @on(Button.Pressed)
    def button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "status-cancel":
            self.dismiss(None)
        elif event.button.id == "status-submit":
            self.action_submit()

    def action_submit(self) -> None:
        self.dismiss(
            StatusEditResult(
                task_state=TaskState(str(self.query_one("#status-task-state", Select).value)),
                input_state=InputState(str(self.query_one("#status-input-state", Select).value)),
            )
        )

    def action_cancel(self) -> None:
        self.dismiss(None)


def session_manage_actions(session: SessionView) -> tuple[ManageAction, ...]:
    stopped = session.runtime is RuntimeState.STOPPED
    stopped_reason = "Session is stopped"
    return (
        ManageAction(
            "identity",
            "General",
            "Identity & organization",
            "Rename the session ID or update its display name, project, and tags.",
            "e",
        ),
        ManageAction("task", "General", "Edit task", "Update the session task note.", "n"),
        ManageAction(
            "status",
            "General",
            "Set task and input status",
            "Set task progress and whether user input is required.",
            "s",
        ),
        ManageAction(
            "pin",
            "General",
            "Unpin session" if session.pinned else "Pin session",
            "Remove this session from Pinned."
            if session.pinned
            else "Keep this session in Pinned.",
            "*",
        ),
        ManageAction(
            "logging",
            "General",
            "Disable logging" if session.logging_enabled else "Enable logging",
            "Disable sanitized persistent output logging."
            if session.logging_enabled
            else "Enable sanitized, owner-only, size-limited output logging.",
            "g",
            enabled=not stopped,
            disabled_reason=stopped_reason if stopped else "",
        ),
        ManageAction(
            "advanced",
            "General",
            "Advanced details",
            "Inspect raw identifiers and runtime metadata.",
            "a",
        ),
        ManageAction(
            "clone",
            "General",
            "Clone session",
            "Create a new session from this one's tool, directory, project, tags, and logging.",
            "c",
        ),
        ManageAction(
            "restart",
            "Runtime",
            "Restart tool",
            "Restart the configured tool, recreating the tmux session if needed.",
            "r",
        ),
        ManageAction(
            "stop-command",
            "Runtime",
            "Stop command",
            "Send Ctrl+C to the active pane while retaining the tmux session.",
            "x",
            enabled=not stopped,
            disabled_reason=stopped_reason if stopped else "",
        ),
        ManageAction(
            "stop-session",
            "Danger",
            "Stop tmux session",
            "Stop tmux while retaining ws metadata and sanitized logs.",
            "t",
            enabled=not stopped,
            disabled_reason=stopped_reason if stopped else "",
            destructive=True,
        ),
        ManageAction(
            "remove-metadata",
            "Danger",
            "Remove ws metadata",
            "Leave tmux running but remove this session from managed ws views.",
            "m",
            destructive=True,
        ),
        ManageAction(
            "delete-logs",
            "Danger",
            "Delete sanitized logs",
            "Permanently remove persisted sanitized output logs.",
            "l",
            destructive=True,
        ),
        ManageAction(
            "delete",
            "Danger",
            "Delete session and metadata",
            "Stop tmux and permanently remove metadata and logs.",
            "d",
            destructive=True,
        ),
    )


class ManageSessionScreen(ModalScreen[ManageSelection | None]):
    BINDINGS: ClassVar[list[BindingSpec]] = [
        Binding("escape", "cancel", "Close"),
        Binding("/", "find", "Find"),
        Binding("ctrl+u", "clear_find", "Clear", show=False, priority=True),
        Binding("j", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
        Binding("e", "choose('identity')", "Identity", show=False),
        Binding("n", "choose('task')", "Task", show=False),
        Binding("s", "choose('status')", "Status", show=False),
        Binding("asterisk", "choose('pin')", "Pin", show=False),
        Binding("g", "choose('logging')", "Logging", show=False),
        Binding("a", "choose('advanced')", "Advanced", show=False),
        Binding("c", "choose('clone')", "Clone", show=False),
        Binding("r", "choose('restart')", "Restart", show=False),
        Binding("x", "choose('stop-command')", "Stop command", show=False),
        Binding("t", "choose('stop-session')", "Stop tmux", show=False),
        Binding("m", "choose('remove-metadata')", "Remove metadata", show=False),
        Binding("l", "choose('delete-logs')", "Delete logs", show=False),
        Binding("d", "choose('delete')", "Delete", show=False),
    ]

    def __init__(self, session: SessionView, *, state: ManageListState | None = None) -> None:
        super().__init__()
        self.session = session
        self.state = state or ManageListState()
        self.actions = session_manage_actions(session)
        self._actions_by_id = {action.action_id: action for action in self.actions}
        self._query = self.state.query
        self._query_before = self._query
        self._highlighted_action = self.state.highlighted_action
        self._finding = False

    def compose(self) -> ComposeResult:
        with Vertical(id="more-dialog", classes="dialog manage-dialog"):
            yield Label("Manage Session", classes="dialog-title")
            yield Static(self.session.name, classes="dialog-context")
            yield Input(placeholder="Find actions", compact=True, id="manage-search")
            yield OptionList(id="manage-actions")
            yield Static("", id="manage-detail")
            yield Static(
                "MANAGE  Up/Down or j/k Navigate   Enter Select   / Find   Esc Close",
                id="manage-help",
                classes="mode-help",
            )
            with Horizontal(id="manage-close-row"):
                yield Button("Close", id="more-cancel")

    def on_mount(self) -> None:
        animate_modal_open(self)
        self.set_class(self.size.width < 100, "narrow-manage")
        self._render_actions()
        options = self.query_one("#manage-actions", OptionList)
        self.call_after_refresh(options.scroll_to, y=self.state.scroll_y, animate=False, force=True)
        options.focus()

    def on_resize(self, event: events.Resize) -> None:
        self.set_class(event.size.width < 100, "narrow-manage")

    def _matching_actions(self) -> tuple[ManageAction, ...]:
        query = self._query.casefold().strip()
        if not query:
            return self.actions
        return tuple(
            action
            for action in self.actions
            if query
            in " ".join(
                (action.category, action.label, action.description, action.disabled_reason)
            ).casefold()
        )

    def _action_prompt(self, action: ManageAction) -> Text:
        marker = "! " if action.destructive else "  "
        if action.enabled:
            status = action.shortcut
        elif action.disabled_reason == "Session is stopped":
            status = "Unavailable: stopped"
        else:
            status = "Unavailable"
        available = 56 if self.size.width >= 100 else max(24, self.size.width - 22)
        if len(action.label) <= available:
            label = action.label
        elif getattr(self.app, "ascii_only", False):
            label = f"{action.label[: available - 3]}..."
        else:
            label = f"{action.label[: available - 1]}…"
        prompt = Text(f"{marker}{label}")
        padding = max(2, 40 - len(label))
        prompt.append(" " * padding)
        prompt.append(status, "dim" if action.enabled else "#7f8a90")
        if action.destructive:
            danger_color = getattr(self.app, "_theme_colors", {}).get("text-error", "#ef8a91")
            prompt.stylize(danger_color, 0, len(marker) + len(label))
        return prompt

    def _render_actions(self) -> None:
        options = self.query_one("#manage-actions", OptionList)
        old_scroll = options.scroll_offset.y
        options.clear_options()
        matches = self._matching_actions()
        for category in ("General", "Runtime", "Danger"):
            category_actions = [action for action in matches if action.category == category]
            if not category_actions:
                continue
            header = Text(category.upper(), style="bold #8fa0a9")
            if category == "Danger":
                danger_color = getattr(self.app, "_theme_colors", {}).get("text-error", "#d9757d")
                header.stylize(f"bold {danger_color}")
            options.add_option(
                Option(header, id=f"manage-category:{category.casefold()}", disabled=True)
            )
            for action in category_actions:
                options.add_option(
                    Option(
                        self._action_prompt(action),
                        id=f"manage-action:{action.action_id}",
                        disabled=not action.enabled,
                    )
                )
        if not matches:
            options.add_option(Option("No matching actions", id="manage-empty", disabled=True))

        option_ids = {
            options.get_option_at_index(index).id for index in range(options.option_count)
        }
        target_id = f"manage-action:{self._highlighted_action}"
        if target_id not in option_ids or not self._actions_by_id[self._highlighted_action].enabled:
            target = next((action for action in matches if action.enabled), None)
            if target is not None:
                self._highlighted_action = target.action_id
                target_id = f"manage-action:{target.action_id}"
        if target_id in option_ids:
            options.highlighted = options.get_option_index(target_id)
            self._render_detail(self._actions_by_id[self._highlighted_action])
        else:
            self.query_one("#manage-detail", Static).update("No actions match this filter.")
        self.call_after_refresh(options.scroll_to, y=old_scroll, animate=False, force=True)

    def _render_detail(self, action: ManageAction) -> None:
        detail = Text(action.description)
        if not action.enabled and action.disabled_reason:
            detail.append(f"\nUnavailable: {action.disabled_reason}", "#d9a441")
        elif action.destructive:
            detail.append("\nProtected confirmation required.", "#d9757d")
        self.query_one("#manage-detail", Static).update(detail)

    def _current_state(self, action_id: str | None = None) -> ManageListState:
        return ManageListState(
            query=self._query,
            highlighted_action=action_id or self._highlighted_action,
            scroll_y=self.query_one("#manage-actions", OptionList).scroll_offset.y,
        )

    @on(OptionList.OptionHighlighted, "#manage-actions")
    def option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        option_id = event.option.id or ""
        if not option_id.startswith("manage-action:"):
            return
        self._highlighted_action = option_id.removeprefix("manage-action:")
        self._render_detail(self._actions_by_id[self._highlighted_action])

    @on(OptionList.OptionSelected, "#manage-actions")
    def option_selected(self, event: OptionList.OptionSelected) -> None:
        option_id = event.option.id or ""
        if option_id.startswith("manage-action:"):
            self.action_choose(option_id.removeprefix("manage-action:"))

    @on(Input.Changed, "#manage-search")
    def search_changed(self, event: Input.Changed) -> None:
        if self._finding:
            self._query = event.value
            self._render_actions()

    @on(Input.Submitted, "#manage-search")
    def search_submitted(self) -> None:
        self._exit_find(commit=True)

    @on(Button.Pressed, "#more-cancel")
    def close_button(self) -> None:
        self.dismiss(None)

    def action_find(self) -> None:
        if self._finding:
            return
        self._finding = True
        self._query_before = self._query
        self.add_class("finding")
        search = self.query_one("#manage-search", Input)
        search.value = self._query
        search.focus()
        self.query_one("#manage-help", Static).update(
            "FIND  Type to filter   Enter Apply   Ctrl+U Clear   Esc Cancel"
        )

    def _exit_find(self, *, commit: bool) -> None:
        if not commit:
            self._query = self._query_before
            with self.prevent(Input.Changed):
                self.query_one("#manage-search", Input).value = self._query
            self._render_actions()
        self._finding = False
        self.remove_class("finding")
        self.query_one("#manage-help", Static).update(
            "MANAGE  Up/Down or j/k Navigate   Enter Select   / Find   Esc Close"
        )
        self.query_one("#manage-actions", OptionList).focus()

    def action_clear_find(self) -> None:
        if self._finding:
            self.query_one("#manage-search", Input).value = ""

    def action_cursor_down(self) -> None:
        if not self._finding:
            self.query_one("#manage-actions", OptionList).action_cursor_down()

    def action_cursor_up(self) -> None:
        if not self._finding:
            self.query_one("#manage-actions", OptionList).action_cursor_up()

    def action_choose(self, action_id: str) -> None:
        if self._finding:
            return
        action = self._actions_by_id[action_id]
        if not action.enabled:
            self.notify(
                action.disabled_reason, title=f"{action.label} unavailable", severity="warning"
            )
            return
        self.dismiss(ManageSelection(action_id, self._current_state(action_id)))

    def action_cancel(self) -> None:
        if self._finding:
            self._exit_find(commit=False)
        else:
            self.dismiss(None)


MoreActionsScreen = ManageSessionScreen


class DeleteSessionScreen(ModalScreen[bool]):
    BINDINGS: ClassVar[list[BindingSpec]] = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, session_name: str) -> None:
        super().__init__()
        self.session_name = session_name

    def compose(self) -> ComposeResult:
        with Vertical(id="delete-dialog", classes="dialog danger-dialog"):
            yield Label("Delete session and metadata", classes="dialog-title danger-title")
            yield Static(
                "This stops the tmux session. Type the exact session name to continue.",
                classes="confirm-copy",
            )
            yield Static(self.session_name, classes="confirm-name")
            yield Input(id="delete-confirm")
            yield Static(
                "CONFIRMATION  Type the session name   Esc Back",
                classes="mode-help",
            )
            with Horizontal(classes="dialog-actions"):
                yield Button("Cancel", id="delete-cancel")
                yield Button("Delete", variant="error", id="delete-submit")

    def on_mount(self) -> None:
        self.query_one("#delete-cancel", Button).focus()

    @on(Button.Pressed)
    def button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "delete-cancel":
            self.dismiss(False)
        elif event.button.id == "delete-submit":
            confirmed = self.query_one("#delete-confirm", Input).value == self.session_name
            if not confirmed:
                self.notify("Session name does not match", severity="error")
                return
            self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)


class ConfirmActionScreen(ModalScreen[bool]):
    BINDINGS: ClassVar[list[BindingSpec]] = [Binding("escape", "cancel", "Cancel")]

    def __init__(
        self,
        title: str,
        session_name: str,
        consequence: str,
        *,
        confirm_label: str = "Confirm",
    ) -> None:
        super().__init__()
        self.confirm_title = title
        self.session_name = session_name
        self.consequence = consequence
        self.confirm_label = confirm_label

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-dialog", classes="dialog danger-dialog small-dialog"):
            yield Label(self.confirm_title, classes="dialog-title danger-title")
            yield Static(self.session_name, classes="confirm-name")
            yield Static(self.consequence, classes="confirm-copy")
            yield Static(
                "CONFIRMATION  Tab Switch   Enter Activate   Esc Back",
                classes="mode-help",
            )
            with Horizontal(classes="dialog-actions"):
                yield Button("Cancel", id="confirm-cancel")
                yield Button(self.confirm_label, variant="error", id="confirm-submit")

    def on_mount(self) -> None:
        animate_modal_open(self)
        self.query_one("#confirm-cancel", Button).focus()

    @on(Button.Pressed)
    def button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm-submit")

    def action_cancel(self) -> None:
        self.dismiss(False)


BULK_ACTIONS: tuple[tuple[str, str], ...] = (
    ("stop", "Stop sessions"),
    ("delete", "Delete sessions and metadata"),
    ("add-tag", "Add tag"),
    ("clear-marks", "Clear marks"),
)


class BulkActionsScreen(ModalScreen[str | None]):
    BINDINGS: ClassVar[list[BindingSpec]] = [Binding("escape", "cancel", "Close")]

    def __init__(self, count: int) -> None:
        super().__init__()
        self.count = count

    def compose(self) -> ComposeResult:
        with Vertical(id="bulk-actions-dialog", classes="dialog manage-dialog"):
            yield Label("Bulk Actions", classes="dialog-title")
            yield Static(f"{self.count} session(s) marked", classes="dialog-context")
            options = OptionList(id="bulk-actions-list")
            for action_id, label in BULK_ACTIONS:
                options.add_option(Option(label, id=f"bulk-action:{action_id}"))
            yield options
            yield Static(
                "BULK ACTIONS  Up/Down Navigate   Enter Select   Esc Close",
                classes="mode-help",
            )
            with Horizontal(classes="dialog-actions"):
                yield Button("Close", id="bulk-actions-cancel")

    def on_mount(self) -> None:
        animate_modal_open(self)
        self.query_one("#bulk-actions-list", OptionList).focus()

    @on(OptionList.OptionSelected, "#bulk-actions-list")
    def option_selected(self, event: OptionList.OptionSelected) -> None:
        option_id = event.option.id
        if option_id is None:
            return
        self.dismiss(option_id.removeprefix("bulk-action:"))

    @on(Button.Pressed, "#bulk-actions-cancel")
    def cancel_pressed(self) -> None:
        self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class BulkTagScreen(ModalScreen[str | None]):
    BINDINGS: ClassVar[list[BindingSpec]] = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, count: int) -> None:
        super().__init__()
        self.count = count

    def compose(self) -> ComposeResult:
        with Vertical(id="bulk-tag-dialog", classes="dialog small-dialog"):
            yield Label("Add Tag", classes="dialog-title")
            yield Static(f"Applies to {self.count} marked session(s)", classes="dialog-context")
            yield Input(placeholder="tag-name", id="bulk-tag-value")
            yield Static("", id="bulk-tag-status", classes="field-status")
            yield Static(
                "FORM  Enter Add   Esc Cancel",
                classes="mode-help",
            )
            with Horizontal(classes="dialog-actions"):
                yield Button("Cancel", id="bulk-tag-cancel")
                yield Button("Add", variant="primary", id="bulk-tag-submit")

    def on_mount(self) -> None:
        animate_modal_open(self)
        self.query_one("#bulk-tag-value", Input).focus()
        self._validate()

    def _validate(self) -> str | None:
        raw = self.query_one("#bulk-tag-value", Input).value
        status = self.query_one("#bulk-tag-status", Static)
        button = self.query_one("#bulk-tag-submit", Button)
        if not raw.strip():
            status.update("")
            button.disabled = True
            return None
        try:
            normalized = normalize_tags([raw])
        except ValueError as error:
            status.update(str(error))
            status.add_class("invalid")
            button.disabled = True
            return None
        status.remove_class("invalid")
        status.update(f"Will add: {normalized[0]}")
        button.disabled = False
        return normalized[0]

    @on(Input.Changed, "#bulk-tag-value")
    def tag_changed(self) -> None:
        self._validate()

    @on(Input.Submitted, "#bulk-tag-value")
    def tag_submitted(self) -> None:
        self.action_submit()

    @on(Button.Pressed)
    def button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "bulk-tag-cancel":
            self.dismiss(None)
        elif event.button.id == "bulk-tag-submit":
            self.action_submit()

    def action_submit(self) -> None:
        tag = self._validate()
        if tag is None:
            animate_modal_shake(self)
            return
        self.dismiss(tag)

    def action_cancel(self) -> None:
        self.dismiss(None)


class MessageScreen(ModalScreen[None]):
    BINDINGS: ClassVar[list[BindingSpec]] = [
        Binding("escape", "close", "Close"),
        Binding("q", "close", "Close"),
    ]

    def __init__(self, title: str, content: Text | str) -> None:
        super().__init__()
        self.message_title = title
        self.message_content = content

    def compose(self) -> ComposeResult:
        with Vertical(id="message-dialog", classes="dialog message-dialog"):
            yield Label(self.message_title, classes="dialog-title")
            yield Static(self.message_content, id="message-content")
            yield Button("Close", id="message-close")

    def on_mount(self) -> None:
        self.query_one("#message-close", Button).focus()

    @on(Button.Pressed, "#message-close")
    def close_button(self) -> None:
        self.dismiss(None)

    def action_close(self) -> None:
        self.dismiss(None)


@dataclass(frozen=True, slots=True)
class FilterState:
    tool: Tool | None = None
    runtime: RuntimeState | None = None
    task: TaskState | None = None
    tag: str | None = None
    project: str | None = None
    warnings_only: bool = False
    recent_only: bool = False

    @property
    def active(self) -> bool:
        return any(
            (
                self.tool,
                self.runtime,
                self.task,
                self.tag,
                self.project,
                self.warnings_only,
                self.recent_only,
            )
        )

    def labels(self) -> list[str]:
        values: list[str] = []
        if self.tool:
            values.append(TOOL_LABELS[self.tool])
        if self.runtime:
            values.append(display_state(self.runtime.value))
        if self.task:
            values.append(display_state(self.task.value))
        if self.tag:
            values.append(f"#{self.tag}")
        if self.project:
            values.append(self.project)
        if self.warnings_only:
            values.append("Warnings")
        if self.recent_only:
            values.append("Recent")
        return values


class FilterScreen(ModalScreen[FilterState | None]):
    BINDINGS: ClassVar[list[BindingSpec]] = [Binding("escape", "cancel", "Cancel")]

    def __init__(
        self,
        current: FilterState,
        *,
        available_tags: Sequence[str] = (),
        available_projects: Sequence[str] = (),
    ) -> None:
        super().__init__()
        self.current = current
        self.available_tags = tuple(available_tags)
        self.available_projects = tuple(available_projects)

    def compose(self) -> ComposeResult:
        with Vertical(id="filter-dialog", classes="dialog small-dialog"):
            yield Label("Filter Sessions", classes="dialog-title")
            yield Label("Tool", classes="field-label")
            yield Select(
                [("Any tool", "any"), *[(TOOL_LABELS[item], item.value) for item in Tool]],
                value=self.current.tool.value if self.current.tool else "any",
                allow_blank=False,
                id="filter-tool",
            )
            yield Label("Runtime", classes="field-label")
            yield Select(
                [
                    ("Any runtime", "any"),
                    *[(display_state(item.value), item.value) for item in RuntimeState],
                ],
                value=self.current.runtime.value if self.current.runtime else "any",
                allow_blank=False,
                id="filter-runtime",
            )
            yield Label("Task", classes="field-label")
            yield Select(
                [
                    ("Any task state", "any"),
                    *[(display_state(item.value), item.value) for item in TaskState],
                ],
                value=self.current.task.value if self.current.task else "any",
                allow_blank=False,
                id="filter-task",
            )
            yield Label("Tag", classes="field-label")
            yield Select(
                [("Any tag", "any"), *[(tag, tag) for tag in self.available_tags]],
                value=self.current.tag if self.current.tag in self.available_tags else "any",
                allow_blank=False,
                id="filter-tag",
            )
            yield Label("Project", classes="field-label")
            yield Select(
                [
                    ("Any project", "any"),
                    *[(project, project) for project in self.available_projects],
                ],
                value=(
                    self.current.project
                    if self.current.project in self.available_projects
                    else "any"
                ),
                allow_blank=False,
                id="filter-project",
            )
            yield Checkbox("Warnings only", self.current.warnings_only, id="filter-warnings")
            yield Checkbox(
                "Active in the last 24 hours",
                self.current.recent_only,
                id="filter-recent",
            )
            yield Static(
                "FILTER  Tab Next   Shift+Tab Previous   Enter Select   Esc Cancel",
                classes="mode-help",
            )
            with Horizontal(classes="dialog-actions"):
                yield Button("Cancel", id="filter-cancel")
                yield Button("Clear", id="filter-clear")
                yield Button("Apply", variant="primary", id="filter-apply")

    def on_mount(self) -> None:
        self.query_one("#filter-tool", Select).focus()

    @on(Button.Pressed)
    def button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "filter-cancel":
            self.dismiss(None)
        elif event.button.id == "filter-clear":
            self.dismiss(FilterState())
        elif event.button.id == "filter-apply":
            tool = str(self.query_one("#filter-tool", Select).value)
            runtime = str(self.query_one("#filter-runtime", Select).value)
            task = str(self.query_one("#filter-task", Select).value)
            tag = str(self.query_one("#filter-tag", Select).value)
            project = str(self.query_one("#filter-project", Select).value)
            self.dismiss(
                FilterState(
                    tool=None if tool == "any" else Tool(tool),
                    runtime=None if runtime == "any" else RuntimeState(runtime),
                    task=None if task == "any" else TaskState(task),
                    tag=None if tag == "any" else tag,
                    project=None if project == "any" else project,
                    warnings_only=self.query_one("#filter-warnings", Checkbox).value,
                    recent_only=self.query_one("#filter-recent", Checkbox).value,
                )
            )

    def action_cancel(self) -> None:
        self.dismiss(None)


def diagnostic_name(check: HealthCheck) -> str:
    labels = {
        "tmux": "tmux",
        "tool:claude": "Claude Code",
        "tool:codex": "Codex",
        "tool:hermes": "Hermes",
        "tool:shell": "Shell",
        "state": "State directory",
        "unmanaged-sessions": "Session ownership",
        "legacy-readonly": "Classic metadata",
        "disk-space": "Disk space",
        "reboot-required": "Reboot required",
        "apt-updates": "Apt updates",
        "docker-containers": "Docker",
        "git-dirty": "Dirty repos",
        "zombie-sessions": "Zombie sessions",
        "idle-sessions": "Idle sessions",
        "orphaned-logs": "Orphaned logs",
    }
    return labels.get(check.name, display_state(check.name))


def diagnostic_detail(check: HealthCheck, *, expanded: bool) -> str:
    if expanded:
        return check.detail.replace(str(Path.home()), "~")
    if check.name.startswith("tool:"):
        return "Available" if check.status is HealthStatus.PASS else "Unavailable"
    if check.name == "state":
        return "Writable" if check.status is HealthStatus.PASS else "Needs attention"
    if check.name == "legacy-readonly":
        return "Not detected" if check.detail.startswith("no legacy") else "Detected"
    return check.detail.replace(str(Path.home()), "~")


class DiagnosticsScreen(ModalScreen[None]):
    BINDINGS: ClassVar[list[BindingSpec]] = [
        Binding("escape", "close", "Close"),
        Binding("q", "close", "Close"),
        Binding("r", "run", "Run again"),
        Binding("e", "export", "Export"),
    ]

    def __init__(self, service: SessionService) -> None:
        super().__init__()
        self.service = service
        self.report = DoctorReport(checks=[])
        self.show_details = False
        self.running = False
        self.last_run_at: datetime | None = None
        self.duration_ms: int | None = None

    def compose(self) -> ComposeResult:
        with Vertical(id="diagnostics-dialog", classes="dialog diagnostics-dialog"):
            yield Label("System Diagnostics", classes="dialog-title")
            yield Static("Not run", id="diagnostics-meta")
            yield Static("", id="diagnostics-summary")
            yield LoadingIndicator(id="diagnostics-loading")
            with VerticalScroll(id="diagnostics-list"):
                yield Static("", id="diagnostics-content")
            yield Static(
                "DIAGNOSTICS  r Run again   e Export   Tab Navigate   Esc Close",
                classes="mode-help",
            )
            with Horizontal(classes="dialog-actions diagnostics-actions"):
                yield Button("Run Again", id="diagnostics-run")
                yield Button("Export Report", id="diagnostics-export")
                yield Button("Show Details", id="diagnostics-details")
                yield Button("Close", variant="primary", id="diagnostics-close")

    def on_mount(self) -> None:
        animate_modal_open(self)
        self.query_one("#diagnostics-close", Button).focus()
        self.action_run()

    def _render_report(self) -> None:
        passed = self.report.count(HealthStatus.PASS)
        warnings = self.report.count(HealthStatus.WARN)
        failed = self.report.count(HealthStatus.FAIL)
        information = self.report.count(HealthStatus.INFO)
        self.query_one("#diagnostics-summary", Static).update(
            f"{passed} passed   {warnings} warnings   {failed} failed   {information} information"
        )
        content = Text()
        theme_colors = getattr(self.app, "_theme_colors", {})
        for check in self.report.checks:
            styles = {
                HealthStatus.PASS: theme_colors.get("success", "green"),
                HealthStatus.WARN: theme_colors.get("warning", "yellow"),
                HealthStatus.FAIL: f"bold {theme_colors.get('error', 'red')}",
                HealthStatus.INFO: theme_colors.get("accent", "#66aaff"),
            }
            style = styles[check.status]
            content.append(f"{check.status.value.upper():<5}", style)
            content.append(f"{diagnostic_name(check):<22}", "bold")
            content.append(f"{diagnostic_detail(check, expanded=self.show_details)}\n")
            if check.status in {HealthStatus.WARN, HealthStatus.FAIL} and check.corrective_action:
                content.append(f"     Action: {check.corrective_action}\n", "dim")
        self.query_one("#diagnostics-content", Static).update(content)
        self.query_one("#diagnostics-details", Button).label = (
            "Hide Details" if self.show_details else "Show Details"
        )

    def _show_loading(self) -> None:
        if self.running:
            self.query_one("#diagnostics-loading", LoadingIndicator).display = True

    def action_run(self) -> None:
        if self.running:
            return
        self.running = True
        self.query_one("#diagnostics-loading", LoadingIndicator).display = False
        self.query_one("#diagnostics-summary", Static).update(
            "Running diagnostics... Checking tmux and tool availability"
        )
        self.query_one("#diagnostics-meta", Static).update("Running now")
        for selector in ("#diagnostics-run", "#diagnostics-export", "#diagnostics-details"):
            self.query_one(selector, Button).disabled = True
        self.set_timer(0.25, self._show_loading)
        self._run_diagnostics()

    @work(thread=True, exclusive=True, group="diagnostics")
    def _run_diagnostics(self) -> None:
        started = perf_counter()
        try:
            report = self.service.doctor()
        except (OSError, WsError) as error:
            report = DoctorReport(
                checks=[
                    HealthCheck(
                        name="diagnostics-run",
                        status=HealthStatus.FAIL,
                        detail=str(error),
                        corrective_action="Close diagnostics, verify the environment, and retry.",
                    )
                ]
            )
        duration_ms = max(1, round((perf_counter() - started) * 1000))
        self.app.call_from_thread(self._finish_diagnostics, report, duration_ms)

    def _finish_diagnostics(self, report: DoctorReport, duration_ms: int) -> None:
        self.report = report
        self.duration_ms = duration_ms
        self.last_run_at = datetime.now().astimezone()
        self.running = False
        self.query_one("#diagnostics-loading", LoadingIndicator).display = False
        for selector in ("#diagnostics-run", "#diagnostics-export", "#diagnostics-details"):
            self.query_one(selector, Button).disabled = False
        duration = "<1 second" if duration_ms < 1000 else f"{duration_ms / 1000:.1f} seconds"
        self.query_one("#diagnostics-meta", Static).update(
            f"Last run: just now   Completed in {duration}"
        )
        self._render_report()

    @on(Button.Pressed)
    def button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "diagnostics-close":
            self.dismiss(None)
        elif event.button.id == "diagnostics-run":
            self.action_run()
        elif event.button.id == "diagnostics-details":
            self.show_details = not self.show_details
            self._render_report()
        elif event.button.id == "diagnostics-export":
            try:
                destination = self.service.export_doctor_report(self.report)
            except (OSError, WsError) as error:
                self.notify(str(error), title="Export failed", severity="error")
            else:
                self.notify(display_path(destination), title="Privacy-safe report exported")

    def action_export(self) -> None:
        if not self.running:
            self.query_one("#diagnostics-export", Button).press()

    def action_close(self) -> None:
        self.dismiss(None)


class HealthAlertsScreen(ModalScreen[str | None]):
    BINDINGS: ClassVar[list[BindingSpec]] = [
        Binding("escape", "close", "Close"),
        Binding("q", "close", "Close"),
        Binding("r", "run", "Refresh now"),
        Binding("c", "copy", "Copy details"),
        Binding("d", "dismiss_until_refresh", "Dismiss"),
        Binding("v", "view_sessions", "Affected sessions"),
        Binding("f", "fix", "Fix issues"),
    ]

    def __init__(self, service: SessionService) -> None:
        super().__init__()
        self.service = service
        self.checks: list[HealthCheck] = service.cached_health_alerts()
        self.running = False

    def compose(self) -> ComposeResult:
        with Vertical(id="diagnostics-dialog", classes="dialog diagnostics-dialog"):
            yield Label("System Health", classes="dialog-title")
            yield Static("", id="health-alerts-summary")
            yield LoadingIndicator(id="health-alerts-loading")
            with VerticalScroll(id="health-alerts-list"):
                yield Static("", id="health-alerts-content")
            yield Static(
                "HEALTH  r Refresh   f Fix issues   v Session warnings   "
                "c Copy details   Esc Close",
                classes="mode-help",
            )
            with Horizontal(classes="dialog-actions diagnostics-actions"):
                yield Button("Refresh Now", id="health-alerts-run")
                yield Button("Fix Issues", id="health-alerts-fix")
                yield Button("View Sessions", id="health-alerts-sessions")
                yield Button("Copy Details", id="health-alerts-copy")
                yield Button("Dismiss", id="health-alerts-dismiss")
                yield Button("Close", variant="primary", id="health-alerts-close")

    def on_mount(self) -> None:
        animate_modal_open(self)
        self.query_one("#health-alerts-close", Button).focus()
        self._render_report()
        self.action_run()

    def _render_report(self) -> None:
        warnings = sum(check.status is HealthStatus.WARN for check in self.checks)
        failures = sum(check.status is HealthStatus.FAIL for check in self.checks)
        information = sum(check.status is HealthStatus.INFO for check in self.checks)
        passed = sum(check.status is HealthStatus.PASS for check in self.checks)
        self.query_one("#health-alerts-summary", Static).update(
            f"{passed} ok   {warnings} warnings   {failures} failed   {information} information"
        )
        content = Text()
        theme_colors = getattr(self.app, "_theme_colors", {})
        styles = {
            HealthStatus.PASS: theme_colors.get("success", "green"),
            HealthStatus.WARN: theme_colors.get("warning", "yellow"),
            HealthStatus.FAIL: f"bold {theme_colors.get('error', 'red')}",
            HealthStatus.INFO: theme_colors.get("accent", "#66aaff"),
        }
        for check in self.checks:
            content.append(f"{check.status.value.upper():<5}", styles[check.status])
            content.append(f"{diagnostic_name(check):<22}", "bold")
            content.append(f"{diagnostic_detail(check, expanded=True)}\n")
            if check.status in {HealthStatus.WARN, HealthStatus.FAIL} and check.corrective_action:
                hint = " (press f to clean up)" if check.fixable else ""
                content.append(f"     Action: {check.corrective_action}{hint}\n", "dim")
        self.query_one("#health-alerts-content", Static).update(content)
        self.query_one("#health-alerts-fix", Button).disabled = not self._fixable_names()

    def _fixable_names(self) -> list[str]:
        return [
            check.name
            for check in self.checks
            if check.fixable and check.status in {HealthStatus.WARN, HealthStatus.FAIL}
        ]

    def action_run(self) -> None:
        if self.running:
            return
        self.running = True
        self.query_one("#health-alerts-loading", LoadingIndicator).display = True
        for selector in ("#health-alerts-run", "#health-alerts-fix"):
            self.query_one(selector, Button).disabled = True
        self._run_refresh()

    @work(thread=True, exclusive=True, group="health-alerts-detail")
    def _run_refresh(self) -> None:
        checks = self.service.refresh_health_alerts(force=True)
        self.app.call_from_thread(self._finish_refresh, checks)

    def _finish_refresh(self, checks: list[HealthCheck]) -> None:
        self.checks = checks
        self.running = False
        self.query_one("#health-alerts-loading", LoadingIndicator).display = False
        self.query_one("#health-alerts-run", Button).disabled = False
        self._render_report()

    def action_fix(self) -> None:
        if self.running:
            return
        names = self._fixable_names()
        if not names:
            return
        self.running = True
        self.query_one("#health-alerts-loading", LoadingIndicator).display = True
        for selector in ("#health-alerts-run", "#health-alerts-fix"):
            self.query_one(selector, Button).disabled = True
        self._run_fix(names)

    @work(thread=True, exclusive=True, group="health-alerts-detail")
    def _run_fix(self, names: list[str]) -> None:
        fixed: list[str] = []
        for name in names:
            check = self.service.apply_health_fix(name)
            if check.status is HealthStatus.PASS:
                fixed.append(name)
        checks = self.service.refresh_health_alerts(force=True)
        self.app.call_from_thread(self._finish_fix, checks, fixed)

    def _finish_fix(self, checks: list[HealthCheck], fixed: list[str]) -> None:
        self.checks = checks
        self.running = False
        self.query_one("#health-alerts-loading", LoadingIndicator).display = False
        self.query_one("#health-alerts-run", Button).disabled = False
        self._render_report()
        if fixed:
            by_name = {check.name: check for check in checks}
            labels = [diagnostic_name(by_name[name]) if name in by_name else name for name in fixed]
            self.notify(f"Cleaned up: {', '.join(labels)}")
        else:
            self.notify("Nothing to fix")

    @on(Button.Pressed)
    def button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "health-alerts-close":
            self.dismiss(None)
        elif event.button.id == "health-alerts-run":
            self.action_run()
        elif event.button.id == "health-alerts-fix":
            self.action_fix()
        elif event.button.id == "health-alerts-sessions":
            self.action_view_sessions()
        elif event.button.id == "health-alerts-copy":
            self.action_copy()
        elif event.button.id == "health-alerts-dismiss":
            self.action_dismiss_until_refresh()

    def action_close(self) -> None:
        self.dismiss(None)

    def action_copy(self) -> None:
        details = "\n".join(
            f"{check.status.value.upper()} {diagnostic_name(check)}: {check.detail}"
            for check in self.checks
        )
        self.app.copy_to_clipboard(details)
        self.notify("Health details copied")

    def action_view_sessions(self) -> None:
        self.dismiss("view-sessions")

    def action_dismiss_until_refresh(self) -> None:
        self.dismiss("dismiss")


class OnboardingScreen(ModalScreen[str | None]):
    BINDINGS: ClassVar[list[BindingSpec]] = [Binding("escape", "close", "Close")]

    STEPS: ClassVar[tuple[tuple[str, str], ...]] = (
        (
            "1 of 3 - Sessions",
            "Sessions continue running through tmux after your SSH connection disconnects.",
        ),
        (
            "2 of 3 - Status",
            "ws keeps tmux runtime, task progress, agent state, and input requirements separate.",
        ),
        (
            "3 of 3 - Safety",
            "Stop and delete operations always require a protected confirmation.",
        ),
    )

    def __init__(self) -> None:
        super().__init__()
        self.step = 0

    def compose(self) -> ComposeResult:
        with Vertical(id="onboarding-dialog", classes="dialog small-dialog"):
            yield Label(self.STEPS[0][0], id="onboarding-title", classes="dialog-title")
            yield Static(self.STEPS[0][1], id="onboarding-copy")
            with Horizontal(classes="dialog-actions"):
                yield Button("Skip", id="onboarding-close")
                yield Button("Next", variant="primary", id="onboarding-next")

    def on_mount(self) -> None:
        animate_modal_open(self)
        self.query_one("#onboarding-close", Button).focus()

    def _render_step(self) -> None:
        title, copy = self.STEPS[self.step]
        self.query_one("#onboarding-title", Label).update(title)
        self.query_one("#onboarding-copy", Static).update(copy)
        self.query_one("#onboarding-next", Button).label = (
            "Start using ws" if self.step == len(self.STEPS) - 1 else "Next"
        )

    @on(Button.Pressed)
    def button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "onboarding-close":
            self.dismiss(None)
        elif event.button.id == "onboarding-next":
            if self.step == len(self.STEPS) - 1:
                self.dismiss(None)
            else:
                self.step += 1
                self._render_step()

    def action_close(self) -> None:
        self.dismiss(None)


def advanced_document(session: SessionView) -> Text:
    return labeled_values(
        [
            ("Session ID", session.session_id),
            ("Raw task", session.note),
            ("Command", session.current_command),
            ("Directory", display_path(session.cwd)),
            ("Tool", session.tool.value),
            ("Runtime", session.runtime.value),
            ("Task state", session.task_state.value),
            ("Input state", session.input_state.value),
            ("Logging", "enabled" if session.logging_enabled else "disabled"),
            ("Ownership", "workspace-session-manager" if session.owned else "unmanaged"),
        ]
    )


class LogScreen(Screen[str | None]):
    CSS_PATH = "wf.tcss"
    BINDINGS: ClassVar[list[BindingSpec]] = [
        Binding("escape", "close", "Back"),
        Binding("q", "close", "Back"),
        Binding("r", "refresh", "Refresh"),
        Binding("enter", "attach", "Attach"),
        Binding("f", "toggle_follow", "Follow"),
        Binding("t", "toggle_time", "Time"),
        Binding("c", "copy", "Copy"),
        Binding("/", "find", "Find"),
        Binding("ctrl+u", "clear_find", "Clear", show=False, priority=True),
        Binding("shift+enter", "previous_match", "Previous", show=False),
    ]

    def __init__(self, service: SessionService, session: SessionView) -> None:
        super().__init__()
        self.add_class("logs-workspace")
        self.service = service
        self.session = session
        self.follow_output = True
        self.show_absolute_time = False
        self.output_source = (
            OutputSource.SAVED if session.runtime is RuntimeState.STOPPED else OutputSource.PANE
        )
        self.available_sources: tuple[OutputSource, ...] = (
            () if session.runtime is RuntimeState.STOPPED else (OutputSource.PANE,)
        )
        self.rendered_output = ""
        self.captured_at: datetime | None = None
        self._preview_truncated = False
        self.refreshing = False
        self.error_message = ""
        self.finding = False
        self.find_query = ""
        self.matches: list[tuple[tuple[int, int], tuple[int, int]]] = []
        self.match_index = -1
        self._refresh_timer: Timer | None = None
        self._refresh_generation = 0
        self._tail_offset = 0
        self._tailing = False
        self._viewports: dict[
            OutputSource,
            tuple[tuple[int, int], tuple[int, int], int],
        ] = {}

    def compose(self) -> ComposeResult:
        yield Static("", id="log-header")
        yield Static("", id="log-status")
        with Horizontal(id="log-controls"):
            yield Button("Live", id="log-source-pane", classes="log-source", compact=True)
            yield Button("Saved", id="log-source-saved", classes="log-source", compact=True)
            yield Button("Following", id="log-follow", compact=True)
            yield Static("", id="log-output-meta")
        yield Static("", id="log-alert")
        with Horizontal(id="log-find"):
            yield Static("FIND", id="log-find-label")
            yield Input(placeholder="Find in sanitized output", compact=True, id="log-find-input")
            yield Static("", id="log-find-count")
        yield Static("", id="log-error")
        yield TextArea(
            "",
            read_only=True,
            soft_wrap=True,
            show_cursor=True,
            highlight_cursor_line=False,
            placeholder="Loading sanitized output...",
            id="log-output",
        )
        yield Static("", id="log-action-bar")
        yield Static("", id="log-small-terminal")

    def on_mount(self) -> None:
        self.query_one("#log-output", TextArea).cursor_blink = False
        self._set_layout_classes(self.size.width, self.size.height)
        self._render_workspace()
        if isinstance(self.app, WsApp):
            self.app._suspend_dashboard_refresh()
        self._refresh_timer = self.set_interval(
            self.service.config.refresh_interval,
            self._poll_if_following,
        )
        self.action_refresh()
        self.query_one("#log-output", TextArea).focus()

    def on_unmount(self) -> None:
        self._refresh_generation += 1
        if self._refresh_timer is not None:
            self._refresh_timer.stop()
            self._refresh_timer = None
        if isinstance(self.app, WsApp):
            self.app._resume_dashboard_refresh()

    def on_resize(self, event: events.Resize) -> None:
        self._set_layout_classes(event.size.width, event.size.height)
        self._render_workspace()

    def _set_layout_classes(self, width: int, height: int) -> None:
        self.set_class(width < 100, "log-narrow")
        self.set_class(100 <= width < 120, "log-medium")
        self.set_class(width < 40 or height < 15, "log-too-small")
        if width < 40 or height < 15:
            self.query_one("#log-small-terminal", Static).update(
                "ws Logs requires a terminal of at least 40x15.\n\n"
                f"Current: {width}x{height}\n\nEsc  Back"
            )

    def _poll_if_following(self) -> None:
        if self.follow_output and not self.finding and not self.refreshing:
            self._start_refresh(self.output_source)

    def action_refresh(self) -> None:
        if self.finding or self.refreshing:
            return
        self._remember_viewport(self.output_source)
        self._start_refresh(self.output_source)

    def _start_refresh(self, source: OutputSource) -> None:
        self._refresh_generation += 1
        generation = self._refresh_generation
        self.refreshing = True
        self.error_message = ""
        self._render_workspace()
        requested_source = (
            None
            if source is OutputSource.SAVED
            and self.session.runtime is RuntimeState.STOPPED
            and OutputSource.SAVED not in self.available_sources
            else source
        )
        if requested_source is OutputSource.SAVED and self.session.logging_enabled:
            self._load_tail(generation, self._tail_offset)
        else:
            self._tailing = False
            self._load_output(generation, requested_source)

    @work(thread=True, exclusive=True, group="log-refresh")
    def _load_output(self, generation: int, source: OutputSource | None) -> None:
        try:
            details = self.service.logs(self.session.name, source=source)
        except (OSError, WsError) as error:
            self.app.call_from_thread(self._finish_refresh, generation, None, str(error))
            return
        self.app.call_from_thread(self._finish_refresh, generation, details, "")

    @work(thread=True, exclusive=True, group="log-refresh")
    def _load_tail(self, generation: int, offset: int) -> None:
        try:
            session = self.service.get(self.session.name)
            result = self.service.tail_log(session.name, offset)
        except (OSError, WsError) as error:
            self.app.call_from_thread(self._finish_tail, generation, None, None, str(error), offset)
            return
        self.app.call_from_thread(self._finish_tail, generation, session, result, "", offset)

    def _finish_tail(
        self,
        generation: int,
        session: SessionView | None,
        result: TailResult | None,
        error: str,
        requested_offset: int,
    ) -> None:
        if generation != self._refresh_generation or not self.is_mounted:
            return
        self.refreshing = False
        if error:
            self.follow_output = False
            self.error_message = error
            self._render_workspace()
            return
        assert session is not None and result is not None
        if session.session_id != self.session.session_id:
            self.follow_output = False
            self.error_message = (
                "Session identity changed. Return to the dashboard and inspect the new session."
            )
            self._render_workspace()
            return
        self.session = session
        self.output_source = OutputSource.SAVED
        self.available_sources = (
            *((OutputSource.PANE,) if session.runtime is not RuntimeState.STOPPED else ()),
            OutputSource.SAVED,
        )
        self._tailing = True
        if result.rotated or requested_offset == 0:
            self.rendered_output = result.text
            self._preview_truncated = result.truncated
        elif result.text:
            self.rendered_output = self._append_bounded(self.rendered_output, result.text)
            if result.truncated:
                self._preview_truncated = True
        self._tail_offset = result.offset
        self.captured_at = datetime.now(UTC)
        self._load_text()
        self._render_workspace()
        if self.follow_output:
            self._scroll_to_end()
        else:
            self._restore_viewport(self.output_source)

    def _append_bounded(self, existing: str, new_text: str) -> str:
        combined = existing + new_text
        cap = self.service.config.log_bytes
        encoded = combined.encode("utf-8")
        if len(encoded) <= cap:
            return combined
        self._preview_truncated = True
        trimmed = encoded[-cap:].decode("utf-8", errors="ignore")
        newline = trimmed.find("\n")
        return trimmed[newline + 1 :] if newline != -1 else trimmed

    def _finish_refresh(
        self,
        generation: int,
        details: SessionDetails | None,
        error: str,
    ) -> None:
        if generation != self._refresh_generation or not self.is_mounted:
            return
        self.refreshing = False
        if error:
            self.follow_output = False
            self.error_message = error
            self._render_workspace()
            return
        assert details is not None
        if details.session.session_id != self.session.session_id:
            self.follow_output = False
            self.error_message = (
                "Session identity changed. Return to the dashboard and inspect the new session."
            )
            self._render_workspace()
            return
        self.session = details.session
        self.output_source = details.output_source
        self.available_sources = details.available_sources
        self.rendered_output = details.preview
        self.captured_at = datetime.now(UTC)
        self._preview_truncated = details.preview_truncated
        self._load_text()
        self._render_workspace()
        if self.follow_output:
            self._scroll_to_end()
        else:
            self._restore_viewport(self.output_source)

    def _remember_viewport(self, source: OutputSource) -> None:
        if not self.query("#log-output"):
            return
        area = self.query_one("#log-output", TextArea)
        self._viewports[source] = (
            area.selection.start,
            area.selection.end,
            area.scroll_offset.y,
        )

    def _restore_viewport(self, source: OutputSource) -> None:
        area = self.query_one("#log-output", TextArea)
        viewport = self._viewports.get(source)
        if viewport is None:
            area.move_cursor((0, 0))
            self.call_after_refresh(area.scroll_home, animate=False)
            return
        start, end, scroll_y = viewport
        document_end = area.document.end

        def clamp(location: tuple[int, int]) -> tuple[int, int]:
            row = min(location[0], document_end[0])
            line = area.document.get_line(row)
            return row, min(location[1], len(line))

        area.move_cursor(clamp(start))
        area.move_cursor(clamp(end), select=start != end)
        self.call_after_refresh(area.scroll_to, y=scroll_y, animate=False, force=True)

    def _scroll_to_end(self) -> None:
        area = self.query_one("#log-output", TextArea)
        area.move_cursor(area.document.end)
        self.call_after_refresh(area.scroll_end, animate=False)

    def _load_text(self) -> None:
        self.query_one("#log-output", TextArea).load_text(self.rendered_output)
        if self.find_query:
            self._find_matches(select_first=False)

    def _capture_label(self) -> str:
        if self.captured_at is None:
            return "Not updated"
        local = self.captured_at.astimezone()
        if self.show_absolute_time:
            return f"Captured {local:%H:%M:%S %Z}"
        age = max(0, int((datetime.now(UTC) - self.captured_at).total_seconds()))
        return "Updated now" if age < 1 else f"Updated {age}s ago"

    def _render_workspace(self) -> None:
        if not self.query("#log-header"):
            return
        self.set_class(bool(self.error_message), "has-log-error")
        notice = detect_activity(self.session, self.rendered_output)
        self.set_class(notice.warning, "has-log-alert")
        header = Text("ws  Logs  ", "bold")
        header.append(
            TOOL_LABELS[self.session.tool].upper(),
            tool_style(self.session.tool, monochrome=getattr(self.app, "monochrome", False)),
        )
        header.append(f"  {self.session.name}", "bold")
        self.query_one("#log-header", Static).update(header)

        agent = notice.agent_state.value
        source = (
            "Live pane"
            if self.output_source is OutputSource.PANE
            else "Saved log (live tail)"
            if self._tailing
            else "Saved log (snapshot)"
        )
        status = (
            f"{display_state(self.session.runtime.value)}  |  "
            f"{display_state(self.session.task_state.value)}  |  "
            f"Agent {display_state(agent)}  |  {source}  |  {self._capture_label()}"
        )
        if self.session.input_state is InputState.REQUIRED:
            status = f"{status}  |  Input required"
        if self.refreshing:
            status = f"{status}  |  Refreshing..."
        self.query_one("#log-status", Static).update(status)

        pane = self.query_one("#log-source-pane", Button)
        saved = self.query_one("#log-source-saved", Button)
        pane.disabled = OutputSource.PANE not in self.available_sources
        saved.disabled = OutputSource.SAVED not in self.available_sources
        pane.set_class(self.output_source is OutputSource.PANE, "active")
        saved.set_class(self.output_source is OutputSource.SAVED, "active")
        follow = self.query_one("#log-follow", Button)
        follow.label = "Following" if self.follow_output else "Paused"
        follow.set_class(self.follow_output, "active")

        theme_colors = getattr(self.app, "_theme_colors", {})
        alert = Text(
            notice.title,
            f"bold {theme_colors.get('warning', 'yellow')}"
            if notice.level == "warning"
            else f"bold {theme_colors.get('error', 'red')}",
        )
        alert.append(f"  {notice.detail}")
        self.query_one("#log-alert", Static).update(alert if notice.warning else "")
        self.query_one("#log-error", Static).update(
            f"OUTPUT UNAVAILABLE  {self.error_message}  Press r to retry."
            if self.error_message
            else ""
        )
        lines = len(self.rendered_output.splitlines())
        bounded = "Older output truncated" if self._preview_truncated else "Complete"
        self.query_one("#log-output-meta", Static).update(f"{lines} sanitized lines  |  {bounded}")
        output = self.query_one("#log-output", TextArea)
        output.tooltip = f"{lines} sanitized lines; {bounded.casefold()}; {source}"
        output.placeholder = (
            "Refreshing sanitized output..."
            if self.refreshing
            else "Output unavailable. Press r to retry."
            if self.error_message
            else "No sanitized output available."
        )
        self._render_find_status()
        self._render_footer()

    def _render_find_status(self) -> None:
        count = self.query_one("#log-find-count", Static)
        if not self.find_query:
            count.update("Type to find")
        elif not self.matches:
            count.update("No matches")
        else:
            count.update(f"{self.match_index + 1}/{len(self.matches)}")

    def _render_footer(self) -> None:
        footer = self.query_one("#log-action-bar", Static)
        if self.finding:
            footer.update("FIND  Enter Next   Shift+Enter Previous   Ctrl+U Clear   Esc Done")
            return
        follow = "Pause" if self.follow_output else "Follow"
        attach = (
            "Attach unavailable"
            if self.session.runtime is RuntimeState.STOPPED or self.error_message
            else "Enter Attach"
        )
        if self.has_class("log-narrow"):
            footer.update(f"Esc Back   / Find   f {follow}   r Refresh   c Copy   {attach}")
        else:
            footer.update(
                f"LOGS  Up/Down Scroll   / Find   f {follow}   r Refresh   "
                f"c Copy   t Time   {attach}   Esc Back"
            )

    def action_toggle_follow(self) -> None:
        if self.finding:
            return
        self.follow_output = not self.follow_output
        self._render_workspace()
        if self.follow_output:
            self._scroll_to_end()
            self.action_refresh()
        else:
            self._remember_viewport(self.output_source)

    def action_toggle_time(self) -> None:
        if self.finding:
            return
        self.show_absolute_time = not self.show_absolute_time
        self._render_workspace()

    def action_copy(self) -> None:
        if self.finding:
            return
        area = self.query_one("#log-output", TextArea)
        content = area.selected_text or self.rendered_output
        if not content:
            return
        self.app.copy_to_clipboard(content)
        self.notify("Selected output copied" if area.selected_text else "Sanitized output copied")

    @on(Button.Pressed, ".log-source")
    def source_pressed(self, event: Button.Pressed) -> None:
        source = OutputSource.PANE if event.button.id == "log-source-pane" else OutputSource.SAVED
        if source is self.output_source or source not in self.available_sources:
            return
        self._remember_viewport(self.output_source)
        self.find_query = ""
        self.matches = []
        self.match_index = -1
        self.finding = False
        self.remove_class("finding")
        self.output_source = source
        self._start_refresh(source)

    @on(Button.Pressed, "#log-follow")
    def follow_pressed(self) -> None:
        self.action_toggle_follow()

    def action_find(self) -> None:
        if self.refreshing or self.has_class("log-too-small"):
            return
        self.follow_output = False
        self.finding = True
        self.add_class("finding")
        search = self.query_one("#log-find-input", Input)
        search.value = self.find_query
        search.focus()
        self._render_workspace()

    @on(Input.Changed, "#log-find-input")
    def find_changed(self, event: Input.Changed) -> None:
        if not self.finding:
            return
        self.find_query = event.value
        self._find_matches(select_first=True)
        self._render_find_status()

    @on(Input.Submitted, "#log-find-input")
    def find_submitted(self) -> None:
        self.action_next_match()

    def _find_matches(self, *, select_first: bool) -> None:
        query = self.find_query.casefold()
        self.matches = []
        self.match_index = -1
        if not query:
            return
        searchable = self.rendered_output.casefold()
        offset = 0
        while (found := searchable.find(query, offset)) >= 0:
            self.matches.append(
                (
                    self._offset_to_location(found),
                    self._offset_to_location(found + len(query)),
                )
            )
            offset = found + max(1, len(query))
        if self.matches and select_first:
            self.match_index = 0
            self._select_match()

    def _offset_to_location(self, offset: int) -> tuple[int, int]:
        prefix = self.rendered_output[:offset]
        row = prefix.count("\n")
        last_break = prefix.rfind("\n")
        return row, offset if last_break < 0 else offset - last_break - 1

    def _select_match(self) -> None:
        if self.match_index < 0 or not self.matches:
            return
        start, end = self.matches[self.match_index]
        area = self.query_one("#log-output", TextArea)
        area.move_cursor(start)
        area.move_cursor(end, select=True, center=True)
        self._render_find_status()

    def action_next_match(self) -> None:
        if not self.finding or not self.matches:
            return
        self.match_index = (self.match_index + 1) % len(self.matches)
        self._select_match()

    def action_previous_match(self) -> None:
        if not self.finding or not self.matches:
            return
        self.match_index = (self.match_index - 1) % len(self.matches)
        self._select_match()

    def action_clear_find(self) -> None:
        if self.finding:
            self.query_one("#log-find-input", Input).value = ""

    def action_attach(self) -> None:
        if self.finding:
            self.action_next_match()
            return
        if self.session.runtime is RuntimeState.STOPPED or self.error_message:
            self.notify("Attach is unavailable for this session state.", severity="warning")
            return
        try:
            current = self.service.get(self.session.name)
        except WsError as error:
            self.notify(str(error), severity="warning")
            return
        if current.session_id != self.session.session_id:
            self.follow_output = False
            self.error_message = "Session identity changed; attach was blocked."
            self._render_workspace()
            return
        self.dismiss(self.session.name)

    def action_close(self) -> None:
        if self.finding:
            self.finding = False
            self.remove_class("finding")
            self.query_one("#log-output", TextArea).focus()
            self._render_workspace()
            return
        self.dismiss(None)


class InteractionMode(StrEnum):
    NORMAL = "normal"
    SEARCH = "search"
    FILTER = "filter"
    FORM = "form"
    PALETTE = "command_palette"
    MANAGE = "manage"
    CONFIRMATION = "confirmation"


@dataclass(frozen=True, slots=True)
class DashboardModeContext:
    mode: InteractionMode
    searching: bool
    filter_query: str
    filters: FilterState
    search_value: str
    selected_name: str | None
    selected_session_id: str | None
    highlighted_option_id: str | None
    scroll_y: int
    narrow_detail_open: bool
    inspector_scroll_y: int
    output_scroll_y: int
    focused_id: str | None


SEARCH_OUTPUT_DEBOUNCE_SECONDS = 0.3
SEARCH_OUTPUT_MIN_QUERY_LENGTH = 2


class SearchOutputScreen(ModalScreen[None]):
    BINDINGS: ClassVar[list[BindingSpec]] = [
        Binding("escape", "cancel", "Close"),
        Binding("down", "cursor_down", "Down", show=False, priority=True),
        Binding("up", "cursor_up", "Up", show=False, priority=True),
    ]

    def __init__(self, service: SessionService, sessions: Sequence[SessionView]) -> None:
        super().__init__()
        self.service = service
        self.sessions = tuple(sessions)
        self._query = ""
        self._debounce_timer: Timer | None = None
        self._search_generation = 0
        self._results: tuple[LogSearchResult, ...] = ()
        self._results_by_name: dict[str, LogSearchResult] = {}
        self._skipped_no_log = 0

    def compose(self) -> ComposeResult:
        with Vertical(id="search-output-dialog", classes="dialog search-output-dialog"):
            yield Label("Search Output", classes="dialog-title")
            yield Input(placeholder="Search saved output across sessions", id="search-output-input")
            yield OptionList(id="search-output-results")
            with VerticalScroll(id="search-output-detail-scroll"):
                yield Static("", id="search-output-detail")
            yield Static(
                "SEARCH OUTPUT  Type to search   Up/Down Navigate   Esc Close",
                id="search-output-help",
                classes="mode-help",
            )
            with Horizontal(id="search-output-close-row"):
                yield Button("Close", id="search-output-cancel")

    def on_mount(self) -> None:
        animate_modal_open(self)
        self.set_class(self.size.width < 100, "narrow-search-output")
        self._render_empty("Type at least two characters to search.")
        self.query_one("#search-output-input", Input).focus()

    def on_resize(self, event: events.Resize) -> None:
        self.set_class(event.size.width < 100, "narrow-search-output")

    def _render_empty(self, message: str) -> None:
        options = self.query_one("#search-output-results", OptionList)
        options.clear_options()
        options.add_option(Option(message, id="search-output-empty", disabled=True))
        self.query_one("#search-output-detail", Static).update("")

    @on(Input.Changed, "#search-output-input")
    def search_changed(self, event: Input.Changed) -> None:
        self._query = event.value
        if self._debounce_timer is not None:
            self._debounce_timer.stop()
            self._debounce_timer = None
        query = self._query.strip()
        if len(query) < SEARCH_OUTPUT_MIN_QUERY_LENGTH:
            self._search_generation += 1
            self._render_empty("Type at least two characters to search.")
            return
        self._debounce_timer = self.set_timer(SEARCH_OUTPUT_DEBOUNCE_SECONDS, self._start_search)

    def _start_search(self) -> None:
        self._debounce_timer = None
        query = self._query.strip()
        if len(query) < SEARCH_OUTPUT_MIN_QUERY_LENGTH:
            return
        self._search_generation += 1
        generation = self._search_generation
        options = self.query_one("#search-output-results", OptionList)
        options.clear_options()
        options.add_option(Option("Searching…", id="search-output-loading", disabled=True))
        self.query_one("#search-output-detail", Static).update("")
        self._run_search(generation, query, self.sessions)

    @work(thread=True, exclusive=True, group="output-search")
    def _run_search(self, generation: int, query: str, sessions: tuple[SessionView, ...]) -> None:
        summary = self.service.search_logs(query, sessions)
        self.app.call_from_thread(self._finish_search, generation, query, summary)

    def _finish_search(self, generation: int, query: str, summary: LogSearchSummary) -> None:
        if generation != self._search_generation:
            return
        self._results = summary.results
        self._results_by_name = {result.name: result for result in summary.results}
        self._skipped_no_log = summary.skipped_no_log
        self._render_results(query)

    def _render_results(self, query: str) -> None:
        options = self.query_one("#search-output-results", OptionList)
        options.clear_options()
        if not self._results:
            message = f'No matches for "{query}".'
            if self._skipped_no_log:
                noun = "session" if self._skipped_no_log == 1 else "sessions"
                message += f" ({self._skipped_no_log} {noun} had no captured output)"
            options.add_option(Option(message, id="search-output-empty", disabled=True))
            self.query_one("#search-output-detail", Static).update("")
            return
        for result in self._results:
            count = len(result.matches)
            noun = "match" if count == 1 else "matches"
            options.add_option(
                Option(
                    f"{result.display_name}  ({count} {noun})",
                    id=f"search-output-result:{result.name}",
                )
            )
        if self._skipped_no_log:
            noun = "session" if self._skipped_no_log == 1 else "sessions"
            options.add_option(
                Option(
                    f"{self._skipped_no_log} {noun} had no captured output",
                    id="search-output-skipped",
                    disabled=True,
                )
            )
        options.highlighted = 0
        self._render_detail(self._results[0])

    def _render_detail(self, result: LogSearchResult) -> None:
        text = Text()
        for index, match in enumerate(result.matches):
            if index:
                text.append("\n\n")
            for line in match.context_before:
                text.append(f"    {line}\n", "dim")
            text.append(f"{match.line_number:>6}  ")
            text.append(f"{match.line}\n", "bold")
            for line in match.context_after:
                text.append(f"    {line}\n", "dim")
        self.query_one("#search-output-detail", Static).update(text)

    @on(OptionList.OptionHighlighted, "#search-output-results")
    def option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        option_id = event.option.id or ""
        if not option_id.startswith("search-output-result:"):
            return
        result = self._results_by_name.get(option_id.removeprefix("search-output-result:"))
        if result is not None:
            self._render_detail(result)

    @on(Button.Pressed, "#search-output-cancel")
    def close_button(self) -> None:
        self.dismiss(None)

    def action_cursor_down(self) -> None:
        self.query_one("#search-output-results", OptionList).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one("#search-output-results", OptionList).action_cursor_up()

    def action_cancel(self) -> None:
        self.dismiss(None)


class WsCommandProvider(Provider):
    """Session-aware commands for Textual's built-in fuzzy palette."""

    def _commands(self) -> list[tuple[str, str, Callable[[], object]]]:
        app = self.app
        if not isinstance(app, WsApp):
            return []
        selected = app._selected()

        def unavailable() -> None:
            app.notify("Select a session before using this command.", severity="warning")

        selected_command = unavailable if selected is None else app.action_open
        edit_command = unavailable if selected is None else app.action_edit
        note_command = unavailable if selected is None else app.action_note
        logs_command = unavailable if selected is None else app.action_logs
        pin_command = unavailable if selected is None else app.action_toggle_pin
        manage_command = unavailable if selected is None else app.action_manage
        availability = "Unavailable: no selected session" if selected is None else "Available"
        return [
            (
                "Create · Claude Code session                       c",
                "Create a persistent Claude Code session",
                app.action_create,
            ),
            (
                "Create · Codex session",
                "Create a persistent Codex session",
                lambda: app.action_create(Tool.CODEX),
            ),
            (
                "Create · Shell session",
                "Create a persistent Linux shell session",
                app.action_shell,
            ),
            (
                "Selected · Attach session                    Enter",
                availability,
                selected_command,
            ),
            (
                "Selected · Edit session                          e",
                availability,
                edit_command,
            ),
            (
                "Selected · Edit task                             n",
                availability,
                note_command,
            ),
            (
                "Selected · Open logs                             l",
                availability,
                logs_command,
            ),
            (
                "Selected · Toggle pin                            *",
                availability,
                pin_command,
            ),
            (
                "Selected · Manage session                        d",
                availability,
                manage_command,
            ),
            (
                "Dashboard · Search sessions                      /",
                "Search names, tasks, projects, tools, and tags",
                app.action_search,
            ),
            (
                "Dashboard · Filter sessions                      f",
                "Filter by tool, runtime, task, warning, or activity",
                app.action_filter,
            ),
            (
                "Dashboard · Search output                        s",
                "Search saved output across every session",
                app.action_search_output,
            ),
            (
                "Dashboard · Attention",
                "Temporarily show sessions with known warnings",
                app.action_attention,
            ),
            (
                "Dashboard · Show all tmux sessions                  u",
                "Include sessions ws doesn't manage (or hide them again)",
                app.action_toggle_unmanaged,
            ),
            (
                "Dashboard · Refresh sessions                     r",
                "Refresh tmux and metadata state",
                app.action_refresh,
            ),
            (
                "System · Run diagnostics",
                "Check tmux, tools, state, and storage",
                app.action_diagnostics,
            ),
            (
                "System · Health alerts",
                "Disk space, apt updates, reboot flag, dirty repos, Docker",
                app.action_health_alerts,
            ),
            (
                "Interface · Switch theme                         t",
                "Cycle dark, light, and monochrome themes",
                app.action_cycle_theme,
            ),
            (
                "Help · Keyboard reference                        ?",
                "Open contextual keyboard help",
                app.action_help,
            ),
        ]

    async def discover(self) -> Hits:
        for display, help_text, command in self._commands():
            yield DiscoveryHit(display, command, help=help_text)

    async def search(self, query: str) -> Hits:
        matcher = self.matcher(query)
        for display, help_text, command in self._commands():
            if (score := matcher.match(display)) > 0:
                yield Hit(score, matcher.highlight(display), command, help=help_text)


class WsApp(App[str | None]):
    """Operational session dashboard; it returns the selected attach target."""

    CSS_PATH = "wf.tcss"
    TITLE = "Workspace"
    ENABLE_COMMAND_PALETTE = True
    COMMANDS: ClassVar[set[type[Provider] | Callable[[], type[Provider]]]] = {WsCommandProvider}
    BINDINGS: ClassVar[list[BindingSpec]] = [
        Binding("q", "quit", "Quit"),
        Binding("enter", "open", "Open"),
        Binding("c", "create", "Create"),
        Binding("j", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
        Binding("n", "note", "Note"),
        Binding("e", "edit", "Edit"),
        Binding("l", "logs", "Logs"),
        Binding("asterisk", "toggle_pin", "Pin"),
        Binding("d", "manage", "Manage"),
        Binding("r", "refresh", "Refresh"),
        Binding("/", "search", "Search"),
        Binding("f", "filter", "Filter"),
        Binding("g", "cycle_grouping", "Group"),
        Binding("z", "toggle_density", "Density"),
        Binding("h", "health_alerts", "Health"),
        Binding("s", "search_output", "Search output"),
        Binding("u", "toggle_unmanaged", "All sessions", show=False),
        Binding("p", "command_palette", "Palette"),
        Binding("a", "advanced_details", "Advanced", show=False),
        Binding("t", "cycle_theme", "Theme", show=False),
        Binding("space", "toggle_mark", "Mark", show=False),
        Binding("b", "bulk_actions", "Bulk actions", show=False),
        Binding("question_mark", "help", "Help"),
        Binding("escape", "escape", "Cancel", show=False),
    ]

    def __init__(
        self,
        service: SessionService,
        *,
        monochrome: bool | None = None,
        hostname: str | None = None,
        theme_mode: str | None = None,
        onboarding: bool = True,
        default_cwd: Path | None = None,
        no_animation: bool = False,
    ) -> None:
        super().__init__()
        self.service = service
        self.sessions: list[SessionView] = []
        self.visible_sessions: list[SessionView] = []
        self.show_unmanaged = False
        self.selected_name: str | None = None
        self.selected_session_id: str | None = None
        self.filter_query = ""
        self.filters = FilterState()
        self._search_before = ""
        self._detail_generation = 0
        self._detail_refreshing = False
        self._rendering_options = False
        self._expected_option_id: str | None = None
        self._option_sessions: dict[str, SessionView] = {}
        self._option_actions: dict[str, str] = {}
        self._marked: set[tuple[str, str]] = set()
        self._alerts: dict[tuple[str, str], ActivityNotice] = {}
        self._activity_history: dict[tuple[str, str], deque[int]] = {}
        self._last_preview: dict[tuple[str, str], str] = {}
        self._attention_scanned_at: dict[tuple[str, str], datetime] = {}
        self._attention_notice_revisions: dict[tuple[str, str], int] = {}
        self._attention_scan_generation = 0
        self._attention_scanning = False
        self._attention_baseline_established = False
        self._attention_scan_error = ""
        self._attention_scan_error_notified = False
        self._attention_dots = 0
        self._attention_dots_timer: Timer | None = None
        self._marker_pulse_phase = False
        self._marker_pulse_timer: Timer | None = None
        self._health_checks: list[HealthCheck] = []
        self._health_scanning = False
        self._health_scan_generation = 0
        self._health_scan_error = ""
        self._health_dismissed_until_refresh = False
        self._attention_context: DashboardModeContext | None = None
        self.interaction_mode = InteractionMode.NORMAL
        self._mode_context: DashboardModeContext | None = None
        self.narrow_detail_open = False
        self._detail_viewports: dict[tuple[str, str], tuple[int, int]] = {}
        self._failed_create_result: CreateFormResult | None = None
        self.output_mode = "summary"
        self.tmux_connected = True
        self.refresh_error = ""
        self.last_refreshed_at: datetime | None = None
        self._dashboard_refresh_timer: Timer | None = None
        self._dashboard_refresh_suspensions = 0
        self._session_refresh_generation = 0
        self._session_refreshing = False
        self._pending_session_refresh = False
        self._onboarding_enabled = onboarding
        self._onboarding_checked = False
        self.default_cwd = default_cwd or Path.cwd()
        self._interface_preferences_store = InterfacePreferencesStore(self.service.paths)
        defaults = InterfacePreferences(
            grouping=self.service.config.interface.default_grouping,
            density=self.service.config.interface.default_density,
        )
        try:
            preferences = (
                self._interface_preferences_store.load()
                if self.service.paths.interface_preferences_file.exists()
                else defaults
            )
        except (OSError, WsError):
            preferences = defaults
        self.grouping: GroupingMode = preferences.grouping
        self.density: DensityMode = preferences.density
        encoding = locale.getpreferredencoding(False).lower()
        self.ascii_only = os.environ.get("WS_ASCII") == "1" or "utf" not in encoding
        no_color = bool(os.environ.get("NO_COLOR"))
        self._no_color_forced = no_color and monochrome is None
        self.monochrome = no_color if monochrome is None else monochrome
        self.ui_theme = theme_mode or ("monochrome" if self.monochrome else "ithaca")
        self._theme_colors: dict[str, str] = {}
        for theme in THEME_DEFINITIONS.values():
            self.register_theme(theme)
        self.theme = self.ui_theme
        self.hostname = hostname or socket.gethostname()
        configured_motion: str = self.service.config.interface.animations
        env_motion = os.environ.get("WS_MOTION", "").strip().lower()
        if env_motion in {"off", "subtle", "full"}:
            configured_motion = env_motion
        self.motion = (
            "off"
            if no_animation
            or self.service.config.interface.reduce_motion
            or configured_motion == "off"
            or self.monochrome
            else configured_motion
        )

    def _persist_interface_preferences(self) -> None:
        try:
            self._interface_preferences_store.save(
                InterfacePreferences(grouping=self.grouping, density=self.density)
            )
        except (OSError, WsError) as error:
            self.notify(str(error), title="Unable to save interface preference", severity="warning")

    def _capture_dashboard_context(self) -> DashboardModeContext:
        options = self.query_one("#sessions", OptionList)
        inspector = self.query_one("#inspector-scroll", VerticalScroll)
        output = self.query_one("#recent-output-scroll", VerticalScroll)
        highlighted_option_id: str | None = None
        if options.highlighted is not None:
            highlighted_option_id = options.get_option_at_index(options.highlighted).id
        focused = self.focused
        return DashboardModeContext(
            mode=self.interaction_mode,
            searching=self.has_class("searching"),
            filter_query=self.filter_query,
            filters=self.filters,
            search_value=self.query_one("#search", Input).value,
            selected_name=self.selected_name,
            selected_session_id=self.selected_session_id,
            highlighted_option_id=highlighted_option_id,
            scroll_y=options.scroll_offset.y,
            narrow_detail_open=self.narrow_detail_open,
            inspector_scroll_y=inspector.scroll_offset.y,
            output_scroll_y=output.scroll_offset.y,
            focused_id=focused.id if focused is not None else None,
        )

    def _set_narrow_detail_state(self, visible: bool) -> None:
        self.narrow_detail_open = visible
        self.set_class(visible, "narrow-detail")

    def _remember_detail_viewport(self) -> None:
        if not self.narrow_detail_open or self.selected_name is None:
            return
        if self.selected_session_id is None:
            return
        self._detail_viewports[(self.selected_name, self.selected_session_id)] = (
            self.query_one("#inspector-scroll", VerticalScroll).scroll_offset.y,
            self.query_one("#recent-output-scroll", VerticalScroll).scroll_offset.y,
        )

    def _restore_detail_viewport(self) -> None:
        if self.selected_name is None or self.selected_session_id is None:
            return
        identity = (self.selected_name, self.selected_session_id)
        inspector_y, output_y = self._detail_viewports.get(identity, (0, 0))
        inspector = self.query_one("#inspector-scroll", VerticalScroll)
        output = self.query_one("#recent-output-scroll", VerticalScroll)
        self.call_after_refresh(inspector.scroll_to, y=inspector_y, animate=False, force=True)
        self.call_after_refresh(output.scroll_to, y=output_y, animate=False, force=True)

    def _open_narrow_detail(self) -> None:
        if not self.has_class("narrow") or self._selected() is None:
            return
        self._set_narrow_detail_state(True)
        self._restore_detail_viewport()
        self.call_after_refresh(self.query_one("#inspector-scroll", VerticalScroll).focus)
        self._render_header()
        self._render_action_bar()

    def _close_narrow_detail(self, *, restore_focus: bool = True) -> None:
        if not self.narrow_detail_open:
            return
        self._remember_detail_viewport()
        self._set_narrow_detail_state(False)
        if restore_focus:
            self.call_after_refresh(self.query_one("#sessions", OptionList).focus)
        self._render_header()
        self._render_action_bar()

    def _set_interaction_mode(self, mode: InteractionMode) -> None:
        self.interaction_mode = mode
        for candidate in InteractionMode:
            self.set_class(candidate is mode, f"mode-{candidate.value}")
        overlay_active = mode not in {InteractionMode.NORMAL, InteractionMode.SEARCH}
        self.set_class(overlay_active, "overlay-active")
        if mode is not InteractionMode.SEARCH:
            self.remove_class("searching")
        self._render_action_bar()

    def _begin_overlay(self, mode: InteractionMode) -> None:
        if self._mode_context is None:
            self._mode_context = self._capture_dashboard_context()
        self._set_interaction_mode(mode)

    def _restore_dashboard_mode(self, *, filters: FilterState | None = None) -> None:
        context = self._mode_context
        self._mode_context = None
        if context is None:
            self._set_interaction_mode(InteractionMode.NORMAL)
            return

        self.filter_query = context.filter_query
        self.filters = context.filters if filters is None else filters
        selection_is_visible = any(
            session.name == context.selected_name
            and session.session_id == context.selected_session_id
            for session in self.visible_sessions
        )
        self.selected_name = context.selected_name if selection_is_visible else None
        self.selected_session_id = context.selected_session_id if selection_is_visible else None
        search = self.query_one("#search", Input)
        with self.prevent(Input.Changed):
            search.value = context.search_value
        self._set_interaction_mode(
            InteractionMode.SEARCH if context.searching else InteractionMode.NORMAL
        )
        if context.searching:
            self.add_class("searching")
        restore_detail = (
            context.narrow_detail_open and selection_is_visible and self.has_class("narrow")
        )
        self._set_narrow_detail_state(restore_detail)

        options = self.query_one("#sessions", OptionList)
        option_ids = {
            options.get_option_at_index(index).id for index in range(options.option_count)
        }
        if (
            context.highlighted_option_id is not None
            and context.highlighted_option_id in option_ids
        ):
            options.highlighted = options.get_option_index(context.highlighted_option_id)
        self.call_after_refresh(options.scroll_to, y=context.scroll_y, animate=False, force=True)
        inspector = self.query_one("#inspector-scroll", VerticalScroll)
        output = self.query_one("#recent-output-scroll", VerticalScroll)
        if restore_detail:
            self.call_after_refresh(
                inspector.scroll_to,
                y=context.inspector_scroll_y,
                animate=False,
                force=True,
            )
            self.call_after_refresh(
                output.scroll_to,
                y=context.output_scroll_y,
                animate=False,
                force=True,
            )
        focus_target: Widget = options
        if context.focused_id:
            matches = self.query(f"#{context.focused_id}")
            if matches:
                focus_target = matches.first()
        self.call_after_refresh((inspector if restore_detail else focus_target).focus)
        self._render_header()
        self._render_action_bar()

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        """Expose only actions that belong to the current interaction mode."""
        del parameters
        if len(self.screen_stack) > 1:
            return False
        if self.interaction_mode is InteractionMode.SEARCH:
            return action == "escape"
        if self.interaction_mode is not InteractionMode.NORMAL:
            return False
        selection_actions = {
            "open",
            "edit",
            "note",
            "logs",
            "toggle_pin",
            "manage",
            "advanced_details",
        }
        if action in selection_actions and self._selected() is None:
            return None
        return True

    def compose(self) -> ComposeResult:
        with Vertical(id="header-band"):
            yield Static("", id="app-header")
            yield Static("", id="health-row")
        with Horizontal(id="search-mode"):
            yield Static("Search", id="search-label")
            yield Input(placeholder="name, tool, task, project, tag", id="search")
            yield Static("Enter apply  Esc cancel", id="search-hint")
        with Horizontal(id="workspace"):
            with Vertical(id="session-pane"):
                with Horizontal(id="session-toolbar"):
                    yield Button("+ Create", id="toolbar-create", compact=True)
                    yield Button("Resume", id="toolbar-resume", compact=True)
                    yield Button("Filter", id="toolbar-filter", compact=True)
                    yield Button("Group", id="toolbar-group", compact=True)
                    yield Button("Health", id="toolbar-health", compact=True)
                yield Static("", id="session-toolbar-meta")
                yield OptionList(id="sessions")
            with Vertical(id="detail-pane"):
                yield Static("Select a session", id="identity")
                with VerticalScroll(id="inspector-scroll", can_focus=True):
                    with Horizontal(id="overview-status-row"):
                        with Vertical(id="overview-card", classes="inspector-card"):
                            yield Static("OVERVIEW", classes="section-title")
                            yield Static("", id="overview", classes="section-body")
                        with Vertical(id="status-card", classes="inspector-card"):
                            yield Static("STATUS", classes="section-title")
                            yield Static("", id="runtime-status", classes="section-body")
                    with Vertical(id="activity-card", classes="inspector-card"):
                        yield Static("ACTIVITY", classes="section-title")
                        yield Static("", id="activity", classes="section-body")
                    with Vertical(id="output-card", classes="inspector-card output-card"):
                        with Horizontal(id="output-heading"):
                            yield Static("RECENT OUTPUT", classes="section-title")
                            yield Button(
                                "Summary", id="output-summary", classes="output-mode active"
                            )
                            yield Button("Raw", id="output-raw", classes="output-mode")
                            yield Static("", id="output-meta")
                        with VerticalScroll(id="recent-output-scroll"):
                            yield Static("", id="recent-output", classes="section-body output-body")
                with Vertical(id="actions-card", classes="inspector-card"):
                    yield Static("ACTIONS", id="inspector-actions-title", classes="section-title")
                    with Horizontal(id="session-action-buttons"):
                        yield Button(
                            "↗ Attach",
                            id="action-open",
                            classes="session-action-button",
                            compact=True,
                        )
                        yield Button(
                            "▶ Resume",
                            id="action-resume",
                            classes="session-action-button",
                            compact=True,
                        )
                        yield Button(
                            "Logs",
                            id="action-logs",
                            classes="session-action-button",
                            compact=True,
                        )
                        yield Button(
                            "Pin",
                            id="action-pin",
                            classes="session-action-button",
                            compact=True,
                        )
                        yield Button(
                            "Manage",
                            id="action-manage",
                            classes="session-action-button",
                            compact=True,
                        )
        yield Static("", id="action-bar")
        yield Static("", id="small-terminal")

    def on_mount(self) -> None:
        self.set_class(self.motion == "off", "motion-off")
        if self.ascii_only:
            self.query_one("#action-open", Button).label = "> Attach"
            self.query_one("#action-resume", Button).label = "> Resume"
        self._set_interaction_mode(InteractionMode.NORMAL)
        self._set_layout_classes(self.size.width, self.size.height)
        self._refresh_theme_colors()
        self.refresh_sessions()
        self.query_one("#sessions", OptionList).focus()
        self._dashboard_refresh_timer = self.set_interval(
            self.service.config.refresh_interval,
            self.refresh_sessions,
        )
        # Cache-only read: never blocks first paint on a live health check.
        self._health_checks = self.service.cached_health_alerts()
        self._render_health_row()
        self._start_health_scan()
        self.set_class(self.density == "compact", "compact-density")
        self.call_after_refresh(self._maybe_show_onboarding)

    def on_unmount(self) -> None:
        self._attention_scan_generation += 1
        self._stop_attention_dots()
        if self._marker_pulse_timer is not None:
            self._marker_pulse_timer.stop()
            self._marker_pulse_timer = None

    def _suspend_dashboard_refresh(self) -> None:
        self._dashboard_refresh_suspensions += 1
        if self._dashboard_refresh_suspensions == 1 and self._dashboard_refresh_timer:
            self._dashboard_refresh_timer.pause()

    def _resume_dashboard_refresh(self) -> None:
        if self._dashboard_refresh_suspensions == 0:
            return
        self._dashboard_refresh_suspensions -= 1
        if self._dashboard_refresh_suspensions == 0 and self._dashboard_refresh_timer:
            self._dashboard_refresh_timer.resume()
            self.call_after_refresh(self.refresh_sessions)

    def on_resize(self, event: events.Resize) -> None:
        detail_was_open = self.narrow_detail_open
        self._set_layout_classes(event.size.width, event.size.height)
        if detail_was_open and not self.has_class("narrow"):
            self._close_narrow_detail()
        if not self.has_class("too-small"):
            self._render_options()

    def _set_layout_classes(self, width: int, height: int) -> None:
        for name in (
            "very-wide",
            "wide",
            "medium",
            "narrow",
            "very-narrow",
            "short",
            "too-small",
        ):
            self.remove_class(name)
        if width < 40 or height < 15:
            self.add_class("too-small")
            self.query_one("#small-terminal", Static).update(
                "The terminal is too small for the ws interface.\n\n"
                f"Minimum: 40x15\nCurrent: {width}x{height}\n\n"
                "Use:\nws list\nws --classic"
            )
        elif width >= 140:
            self.add_class("very-wide", "wide")
        elif width >= 120:
            self.add_class("wide")
        elif width >= 100:
            self.add_class("medium")
        elif width >= 80:
            self.add_class("narrow")
        else:
            self.add_class("narrow", "very-narrow")
        if not self.has_class("too-small") and height <= 35:
            self.add_class("short")

    def _refresh_theme_colors(self) -> None:
        variables = self.get_css_variables()
        for key in (
            "warning",
            "warning-muted",
            "text-warning",
            "error",
            "text-error",
            "accent",
            "primary",
            "primary-muted",
            "success",
        ):
            value = variables.get(key)
            if value:
                self._theme_colors[key] = value

    def _maybe_show_onboarding(self) -> None:
        if self._onboarding_checked:
            return
        self._onboarding_checked = True
        if self._onboarding_enabled and not self.sessions and not self.service.onboarding_seen():
            self._begin_overlay(InteractionMode.FORM)
            self.push_screen(OnboardingScreen(), self._finish_onboarding)

    def _finish_onboarding(self, action: str | None) -> None:
        self._restore_dashboard_mode()
        try:
            self.service.mark_onboarding_seen()
        except (OSError, WsError) as error:
            self.notify(str(error), title="Unable to save onboarding state", severity="warning")
        if action == "create":
            self.call_after_refresh(self.action_create)
        elif action == "help":
            self.call_after_refresh(self.action_help)

    def _notify_success(self, message: str, *, title: str = "Completed") -> None:
        marker = "OK" if self.ascii_only else "✓"
        self.notify(f"{marker} {message}", title=title)

    def _row_width(self) -> int:
        if self.has_class("wide"):
            return max(28, int(self.size.width * 0.36) - 5)
        if self.has_class("medium"):
            return max(28, int(self.size.width * 0.42) - 5)
        return max(28, self.size.width - 5)

    @staticmethod
    def _attention_eligible(session: SessionView) -> bool:
        return session.tool is not Tool.SHELL and session.runtime not in {
            RuntimeState.STOPPED,
            RuntimeState.FAILED,
        }

    def _attention_progress(self) -> tuple[int, int]:
        identities = {
            (session.name, session.session_id)
            for session in self.sessions
            if self._attention_eligible(session)
        }
        return len(identities & self._attention_scanned_at.keys()), len(identities)

    def _attention_complete(self) -> bool:
        scanned, eligible = self._attention_progress()
        return scanned == eligible and not self._attention_scan_error

    def _attention_batch(self) -> tuple[AttentionScanRequest, ...]:
        selected_identity = (self.selected_name, self.selected_session_id)
        candidates = [
            session
            for session in self.sessions
            if self._attention_eligible(session)
            and (
                (session.name, session.session_id) != selected_identity
                or (session.name, session.session_id) not in self._attention_scanned_at
            )
        ]
        if not candidates:
            return ()
        minimum = datetime.min.replace(tzinfo=UTC)

        def scan_key(session: SessionView) -> tuple[datetime, datetime, str]:
            identity = (session.name, session.session_id)
            return (
                self._attention_scanned_at.get(identity, minimum),
                session.last_active_at or session.created_at or minimum,
                session.name,
            )

        budget = min(self.service.config.attention_scan_budget, len(candidates))
        priority = sorted(
            (
                session
                for session in candidates
                if session.runtime is RuntimeState.ATTACHED or self._notice_for(session).warning
            ),
            key=scan_key,
        )
        priority_slots = 0 if budget == 1 else budget // 2
        selected = priority[:priority_slots]
        selected_identities = {(session.name, session.session_id) for session in selected}
        general = sorted(
            (
                session
                for session in candidates
                if (session.name, session.session_id) not in selected_identities
            ),
            key=scan_key,
        )
        selected.extend(general[: budget - len(selected)])
        return tuple(
            AttentionScanRequest(
                session=session,
                notice_revision=self._attention_notice_revisions.get(
                    (session.name, session.session_id), 0
                ),
            )
            for session in selected
        )

    def _start_attention_scan(self) -> None:
        if (
            self._attention_scanning
            or self._dashboard_refresh_suspensions
            or len(self.screen_stack) > 1
        ):
            return
        requests = self._attention_batch()
        if not requests:
            if self._attention_complete():
                self._attention_baseline_established = True
                self._stop_attention_dots()
            self._render_header()
            self._render_attention_action()
            return
        self._attention_scan_generation += 1
        self._attention_scanning = True
        self._start_attention_dots()
        self._scan_attention(self._attention_scan_generation, requests)
        self._render_header()
        self._render_attention_action()

    def _start_attention_dots(self) -> None:
        """Keep a stable checking label; repeated animation is hostile to SSH and focus."""
        self._attention_dots = 0

    def _tick_attention_dots(self) -> None:
        if not self.is_running:
            return
        self._attention_dots = (self._attention_dots + 1) % 3
        self._render_attention_action()

    def _stop_attention_dots(self) -> None:
        if self._attention_dots_timer is not None:
            self._attention_dots_timer.stop()
            self._attention_dots_timer = None
        self._attention_dots = 0

    def _start_marker_pulse(self) -> None:
        """Retained as a compatibility no-op; warnings now use stable badges."""
        return

    def _tick_marker_pulse(self) -> None:
        if not self.is_running:
            return
        self._marker_pulse_phase = not self._marker_pulse_phase
        options = self.query_one("#sessions", OptionList)
        for session in self.sessions:
            if session.pinned or not is_warning(session, self._notice_for(session)):
                continue
            option_id = session_option_id(session.name, session.session_id)
            if option_id not in self._option_sessions:
                continue
            prompt = session_row(
                session,
                self._row_width(),
                ascii_only=self.ascii_only,
                monochrome=self.monochrome,
                activity_spark=self._activity_spark_for(session),
                warning_color=self._theme_colors.get("warning", "yellow"),
                warning_dim_color=self._theme_colors.get("warning-muted", "#8a7a2a"),
                notice=self._notice_for(session),
                pulse_dim=self._marker_pulse_phase,
                bulk_mode=bool(self._marked),
                marked=self._is_marked(session),
            )
            options.replace_option_prompt(option_id, prompt)

    @work(thread=True, exclusive=True, group="attention-scan")
    def _scan_attention(
        self,
        generation: int,
        requests: tuple[AttentionScanRequest, ...],
    ) -> None:
        results: list[AttentionScanResult] = []
        for request in requests:
            session = request.session
            try:
                details = self.service.inspect_snapshot(
                    session,
                    preview_lines=ATTENTION_PREVIEW_LINES,
                    preview_bytes=ATTENTION_PREVIEW_BYTES,
                )
            except (OSError, ValueError, WsError) as error:
                results.append(
                    AttentionScanResult(session.name, session.session_id, error=str(error))
                )
                continue
            results.append(AttentionScanResult(session.name, session.session_id, details.preview))
        self.call_from_thread(
            self._finish_attention_scan,
            generation,
            requests,
            tuple(results),
        )

    def _finish_attention_scan(
        self,
        generation: int,
        requests: tuple[AttentionScanRequest, ...],
        results: tuple[AttentionScanResult, ...],
    ) -> None:
        if generation != self._attention_scan_generation:
            return
        self._attention_scanning = False
        request_by_identity = {
            (request.session.name, request.session.session_id): request for request in requests
        }
        errors: list[str] = []
        new_warnings: list[str] = []
        warning_membership_changed = False
        baseline_was_established = self._attention_baseline_established
        observed_at = datetime.now(UTC)
        for result in results:
            identity = (result.name, result.session_id)
            current = next(
                (
                    session
                    for session in self.sessions
                    if (session.name, session.session_id) == identity
                ),
                None,
            )
            request = request_by_identity.get(identity)
            if current is None or request is None:
                continue
            if identity == (self.selected_name, self.selected_session_id):
                continue
            if self._attention_notice_revisions.get(identity, 0) != request.notice_revision:
                continue
            if result.error:
                errors.append(f"{result.name}: {result.error}")
                continue
            previous = self._notice_for(current)
            notice = detect_activity(current, result.preview)
            self._store_notice(current, notice, observed_at=observed_at)
            self._record_activity_sample(identity, result.preview)
            if previous.warning != notice.warning:
                warning_membership_changed = True
            if baseline_was_established and notice.warning and notice.kind != previous.kind:
                new_warnings.append(current.display_name or current.name)

        if errors:
            self._attention_scan_error = errors[0]
            if not self._attention_scan_error_notified:
                self.notify(
                    "Some session alerts could not be checked. Press r to retry.",
                    title="Attention scan delayed",
                    severity="warning",
                )
                self._attention_scan_error_notified = True
        else:
            self._attention_scan_error = ""
            self._attention_scan_error_notified = False
        if self._attention_complete():
            self._attention_baseline_established = True
            self._stop_attention_dots()
        if new_warnings:
            names = ", ".join(new_warnings[:3])
            if len(new_warnings) > 3:
                names = f"{names}, and {len(new_warnings) - 3} more"
            message = (
                f"{len(new_warnings)} session{'s' if len(new_warnings) != 1 else ''} "
                f"need attention: {names}"
            )
            self.notify(message, title="New session warning", severity="warning", timeout=8)
            self._send_telegram_alert(f"ws: {message}")
        if warning_membership_changed and self.filters.warnings_only:
            self._render_options()
        else:
            self._render_header()
            self._render_attention_action()

    @work(thread=True, group="telegram-alert")
    def _send_telegram_alert(self, text: str) -> None:
        send_telegram(self.service.config.notifications, text)

    def _start_health_scan(self, *, force: bool = False) -> None:
        if (
            self._health_scanning
            or self._dashboard_refresh_suspensions
            or len(self.screen_stack) > 1
        ):
            return
        if not self.service.config.health.enabled:
            return
        stale = self.service.health_stale_names(datetime.now(UTC))
        if not stale and not force:
            return
        self._health_scan_generation += 1
        self._health_scanning = True
        self._scan_health(self._health_scan_generation, None if force else frozenset(stale))

    @work(thread=True, exclusive=True, group="health-scan")
    def _scan_health(self, generation: int, only: frozenset[str] | None) -> None:
        try:
            checks = self.service.refresh_health_alerts(only=only)
            error = ""
        except (OSError, WsError) as exc:
            checks = self.service.cached_health_alerts()
            error = str(exc)
        self.call_from_thread(self._finish_health_scan, generation, checks, error)

    def _finish_health_scan(self, generation: int, checks: list[HealthCheck], error: str) -> None:
        if generation != self._health_scan_generation:
            return
        self._health_scanning = False
        self._health_scan_error = error
        self._health_checks = checks
        self._health_dismissed_until_refresh = False
        self._render_health_row()

    def _render_health_row(self) -> None:
        critical = [check for check in self._health_checks if check.status is HealthStatus.FAIL]
        if not critical or self._health_dismissed_until_refresh:
            self.set_class(False, "has-critical-alerts")
            return
        text = Text()
        text.append("  ! CRITICAL SYSTEM HEALTH", f"bold {self._theme_colors.get('error', 'red')}")
        summary = ", ".join(diagnostic_name(check) for check in critical[:3])
        text.append(f"  {summary}  Press h to review", self._theme_colors.get("error", "red"))
        self.query_one("#health-row", Static).update(text)
        self.set_class(True, "has-critical-alerts")

    def refresh_sessions(self) -> None:
        if not self.query("#app-header"):
            return
        if self._session_refreshing:
            self._pending_session_refresh = True
            return
        self._health_dismissed_until_refresh = False
        self._render_health_row()
        self._session_refreshing = True
        self._session_refresh_generation += 1
        self.add_class("refreshing")
        self._render_header()
        self._load_session_inventory(self._session_refresh_generation, self.show_unmanaged)

    @work(thread=True, exclusive=True, group="session-refresh")
    def _load_session_inventory(self, generation: int, include_unmanaged: bool) -> None:
        try:
            sessions = self.service.list_sessions(include_unmanaged=include_unmanaged)
            error = ""
        except WsError as exc:
            sessions = []
            error = str(exc)
        self.call_from_thread(self._finish_session_refresh, generation, sessions, error)

    def _finish_session_refresh(
        self, generation: int, sessions: list[SessionView], error: str
    ) -> None:
        if generation != self._session_refresh_generation or not self.is_running:
            return
        self._session_refreshing = False
        if error:
            self.tmux_connected = False
            self.refresh_error = error
            self.remove_class("refreshing")
            self._render_header()
            self.notify(
                f"{error}\nCheck tmux availability, then press r to retry.",
                title="Refresh failed",
                severity="error",
            )
            if self._pending_session_refresh:
                self._pending_session_refresh = False
                self.call_after_refresh(self.refresh_sessions)
            return
        self.tmux_connected = True
        self.refresh_error = ""
        if sessions == self.sessions:
            self.remove_class("refreshing")
            self.last_refreshed_at = datetime.now(UTC)
            self._render_header()
            self._start_attention_scan()
            self._start_health_scan()
            if self._pending_session_refresh:
                self._pending_session_refresh = False
                self.call_after_refresh(self.refresh_sessions)
            return
        detail_lost = False
        if self.selected_name is not None:
            current = next((item for item in sessions if item.name == self.selected_name), None)
            current_is_visible = current is not None and self._matches_query(current)
            if (
                current is None
                or current.session_id != self.selected_session_id
                or not current_is_visible
            ):
                if self.narrow_detail_open:
                    self._remember_detail_viewport()
                    self._set_narrow_detail_state(False)
                    detail_lost = True
                self.selected_name = None
                self.selected_session_id = None
        elif self.narrow_detail_open:
            self._set_narrow_detail_state(False)
        self.sessions = sessions
        valid_identities = {(session.name, session.session_id) for session in sessions}
        self._alerts = {
            identity: alert
            for identity, alert in self._alerts.items()
            if identity in valid_identities
        }
        self._attention_scanned_at = {
            identity: scanned_at
            for identity, scanned_at in self._attention_scanned_at.items()
            if identity in valid_identities
        }
        self._attention_notice_revisions = {
            identity: revision
            for identity, revision in self._attention_notice_revisions.items()
            if identity in valid_identities
        }
        self._detail_viewports = {
            identity: viewport
            for identity, viewport in self._detail_viewports.items()
            if identity in valid_identities
        }
        self._activity_history = {
            identity: history
            for identity, history in self._activity_history.items()
            if identity in valid_identities
        }
        self._last_preview = {
            identity: preview
            for identity, preview in self._last_preview.items()
            if identity in valid_identities
        }
        self._render_options()
        self.remove_class("refreshing")
        self.last_refreshed_at = datetime.now(UTC)
        self._render_header()
        if detail_lost:
            self.notify(
                "The selected session is no longer available in this view.",
                title="Returned to session list",
                severity="warning",
                timeout=0,
            )
        self._start_attention_scan()
        self._start_health_scan()
        if self._pending_session_refresh:
            self._pending_session_refresh = False
            self.call_after_refresh(self.refresh_sessions)

    def _notice_for(self, session: SessionView) -> ActivityNotice:
        derived = detect_activity(session, "")
        cached = self._alerts.get((session.name, session.session_id))
        if derived.kind in {"runtime-failed", "runtime-stopped"}:
            return derived
        if cached is not None and cached.kind == "usage-limit":
            return cached
        return derived

    def _record_activity_sample(self, identity: tuple[str, str], preview: str) -> None:
        previous = self._last_preview.get(identity, "")
        self._last_preview[identity] = preview
        limit = min(len(previous), len(preview))
        prefix_len = 0
        while prefix_len < limit and previous[prefix_len] == preview[prefix_len]:
            prefix_len += 1
        changed = max(0, len(preview) - prefix_len)
        history = self._activity_history.setdefault(identity, deque(maxlen=ACTIVITY_HISTORY_LENGTH))
        history.append(changed)

    def _is_marked(self, session: SessionView) -> bool:
        return (session.name, session.session_id) in self._marked

    def _activity_spark_for(self, session: SessionView) -> str:
        if not self.has_class("wide") and not self.has_class("very-wide"):
            return ""
        identity = (session.name, session.session_id)
        history = self._activity_history.get(identity)
        if history is None or len(history) < ACTIVITY_SPARK_MIN_SAMPLES:
            return ""
        return sparkline(history, ascii_only=self.ascii_only)

    def _store_notice(
        self,
        session: SessionView,
        notice: ActivityNotice,
        *,
        observed_at: datetime | None = None,
    ) -> None:
        identity = (session.name, session.session_id)
        previous = self._alerts.get(identity, detect_activity(session, ""))
        self._alerts[identity] = notice
        self._attention_notice_revisions[identity] = (
            self._attention_notice_revisions.get(identity, 0) + 1
        )
        if observed_at is not None and self._attention_eligible(session):
            self._attention_scanned_at[identity] = observed_at
        option_id = session_option_id(session.name, session.session_id)
        if option_id in self._option_sessions:
            prompt = session_row(
                session,
                self._row_width(),
                ascii_only=self.ascii_only,
                monochrome=self.monochrome,
                activity_spark=self._activity_spark_for(session),
                warning_color=self._theme_colors.get("warning", "yellow"),
                warning_dim_color=self._theme_colors.get("warning-muted", "#8a7a2a"),
                notice=notice,
                bulk_mode=bool(self._marked),
                marked=self._is_marked(session),
            )
            if self.motion != "off" and notice.warning and notice.kind != previous.kind:
                prompt.stylize("on #4b3b1f")
                self.set_timer(
                    0.45,
                    lambda: self._restore_session_row(session.name, session.session_id),
                )
            self.query_one("#sessions", OptionList).replace_option_prompt(option_id, prompt)

    def _restore_session_row(self, name: str, session_id: str) -> None:
        if not self.is_running:
            return
        session = next(
            (item for item in self.sessions if item.name == name and item.session_id == session_id),
            None,
        )
        if session is None:
            return
        option_id = session_option_id(session.name, session_id)
        if option_id not in self._option_sessions:
            return
        self.query_one("#sessions", OptionList).replace_option_prompt(
            option_id,
            session_row(
                session,
                self._row_width(),
                ascii_only=self.ascii_only,
                monochrome=self.monochrome,
                activity_spark=self._activity_spark_for(session),
                warning_color=self._theme_colors.get("warning", "yellow"),
                warning_dim_color=self._theme_colors.get("warning-muted", "#8a7a2a"),
                notice=self._notice_for(session),
                bulk_mode=bool(self._marked),
                marked=self._is_marked(session),
            ),
        )

    def _matches_query(self, session: SessionView) -> bool:
        if self.filters.tool is not None and session.tool is not self.filters.tool:
            return False
        if self.filters.runtime is not None and session.runtime is not self.filters.runtime:
            return False
        if self.filters.task is not None and session.task_state is not self.filters.task:
            return False
        if self.filters.tag is not None and self.filters.tag not in session.tags:
            return False
        if self.filters.project is not None and session.project != self.filters.project:
            return False
        if self.filters.warnings_only and not is_warning(session, self._notice_for(session)):
            return False
        if self.filters.recent_only:
            if session.last_active_at is None:
                return False
            current = datetime.now(UTC)
            activity = session.last_active_at
            if activity.tzinfo is None:
                activity = activity.replace(tzinfo=UTC)
            if current - activity > RECENT_WINDOW:
                return False
        if not self.filter_query:
            return True
        haystack = " ".join(
            (
                session.name,
                session.display_name,
                session.tool.value,
                session.runtime.value,
                session.task_state.value,
                session.input_state.value,
                session.project,
                session.note,
                str(session.cwd),
                *session.tags,
            )
        ).casefold()
        return self.filter_query.casefold().strip() in haystack

    def _heading_option(self, label: str) -> Option:
        return Option(section_title(label.upper()), id=f"heading:{label.lower()}", disabled=True)

    def _quick_option(self, label: str, action: str, symbol: str) -> Option:
        option_id = f"quick:{action}"
        self._option_actions[option_id] = action
        accent = self._theme_colors.get("accent", "#66aaff")
        return Option(Text.assemble((f"{symbol} ", f"bold {accent}"), label), id=option_id)

    def _warning_count(self) -> int:
        return sum(is_warning(session, self._notice_for(session)) for session in self.sessions)

    def _health_warning_count(self) -> int:
        return sum(
            check.status in {HealthStatus.WARN, HealthStatus.FAIL} for check in self._health_checks
        )

    def _attention_label(self) -> str:
        warnings = self._warning_count() + self._health_warning_count()
        if self._attention_scan_error:
            return f"Attention ({warnings} known, delayed)"
        if warnings:
            return f"Attention ({warnings})"
        if not self._attention_complete():
            if self.motion == "off":
                return "Attention (checking)"
            return f"Attention (checking{'.' * (self._attention_dots + 1)})"
        return "Attention"

    def _unmanaged_option(self) -> Option:
        option_id = "quick:toggle_unmanaged"
        self._option_actions[option_id] = "toggle_unmanaged"
        symbol = "x" if self.ascii_only else "◐"
        label = "Managed sessions only" if self.show_unmanaged else "Show all tmux sessions"
        accent = self._theme_colors.get("accent", "#66aaff")
        return Option(Text.assemble((f"{symbol} ", f"bold {accent}"), label), id=option_id)

    def _attention_style(self) -> str:
        accent = self._theme_colors.get("accent", "#66aaff")
        warning = self._theme_colors.get("warning", "#e9b44c")
        return f"bold {warning}" if self._warning_count() else f"bold {accent}"

    def _attention_option(self) -> Option:
        option_id = "quick:attention"
        self._option_actions[option_id] = "attention"
        return Option(
            Text.assemble(("! ", self._attention_style()), self._attention_label()), id=option_id
        )

    def _render_attention_action(self) -> None:
        option_id = "quick:attention"
        if option_id not in self._option_actions:
            return
        self.query_one("#sessions", OptionList).replace_option_prompt(
            option_id,
            Text.assemble(("! ", self._attention_style()), self._attention_label()),
        )

    def _render_options(self) -> None:
        options = self.query_one("#sessions", OptionList)
        old_scroll = options.scroll_offset.y
        old_identity = (self.selected_name, self.selected_session_id)
        self._rendering_options = True
        options.clear_options()
        self._option_sessions.clear()
        self._option_actions.clear()

        self.visible_sessions = [item for item in self.sessions if self._matches_query(item)]
        selected_index: int | None = None
        first_session_index: int | None = None
        bulk_mode = bool(self._marked)
        for group_name, group_sessions in build_session_groups(
            self.visible_sessions,
            grouping=self.grouping,
            notices=self._notice_for,
        ):
            if not group_sessions:
                continue
            options.add_option(self._heading_option(f"{group_name} ({len(group_sessions)})"))
            for session in group_sessions:
                option_id = session_option_id(session.name, session.session_id)
                self._option_sessions[option_id] = session
                options.add_option(
                    Option(
                        session_row(
                            session,
                            self._row_width(),
                            ascii_only=self.ascii_only,
                            monochrome=self.monochrome,
                            activity_spark=self._activity_spark_for(session),
                            warning_color=self._theme_colors.get("warning", "yellow"),
                            warning_dim_color=self._theme_colors.get("warning-muted", "#8a7a2a"),
                            notice=self._notice_for(session),
                            bulk_mode=bulk_mode,
                            marked=self._is_marked(session),
                        ),
                        id=option_id,
                    )
                )
                if first_session_index is None:
                    first_session_index = options.option_count - 1
                if (session.name, session.session_id) == old_identity:
                    selected_index = options.option_count - 1

        if selected_index is None:
            selected_index = first_session_index
        if selected_index is not None:
            options.highlighted = selected_index
            selected = options.get_option_at_index(selected_index)
            self._expected_option_id = selected.id
            self._select_option(selected.id)
        else:
            self.selected_name = None
            self.selected_session_id = None
            self._render_empty_state()
        self._rendering_options = False
        self.call_after_refresh(options.scroll_to, y=old_scroll, animate=False, force=True)
        self._render_session_toolbar()
        self._render_header()
        self._render_action_bar()

    def _render_session_toolbar(self) -> None:
        if not self.query("#session-toolbar-meta"):
            return
        separator = " / " if self.ascii_only else " · "
        toolbar = self.query_one("#session-toolbar-meta", Static)
        text = (
            f"Sessions{separator}Group: {GROUPING_LABELS[self.grouping]} (g)"
            f"{separator}Density: {self.density.title()} (z)"
        )
        if self._marked:
            text += f"{separator}{len(self._marked)} marked (b bulk actions)"
        toolbar.update(text)

    def _render_header(self) -> None:
        attached = sum(session.runtime is RuntimeState.ATTACHED for session in self.sessions)
        detached = sum(session.runtime is RuntimeState.DETACHED for session in self.sessions)
        stopped = sum(
            session.runtime in {RuntimeState.STOPPED, RuntimeState.FAILED}
            for session in self.sessions
        )
        warnings = self._warning_count()
        if not self.tmux_connected:
            warnings += 1
        session_label = "session" if len(self.sessions) == 1 else "sessions"
        scanned, eligible = self._attention_progress()
        if self._attention_scan_error:
            warning_text = (
                f"{warnings} known warning{'s' if warnings != 1 else ''} / alerts delayed"
            )
        elif scanned < eligible:
            known = (
                "No known warnings"
                if warnings == 0
                else f"{warnings} known warning{'s' if warnings != 1 else ''}"
            )
            warning_text = f"{known} / alerts {scanned}/{eligible} checked"
        else:
            warning_text = (
                "No warnings"
                if warnings == 0
                else f"{warnings} warning{'s' if warnings != 1 else ''}"
            )
        separator = " | " if self.ascii_only else " • "
        counts = separator.join(
            (
                f"{len(self.sessions)} {session_label}",
                f"{attached} attached",
                f"{detached} detached",
                f"{stopped} stopped",
                warning_text,
            )
        )
        active_filters = (
            ["Attention"] if self.has_class("attention-view") else self.filters.labels()
        )
        if self.filter_query:
            active_filters.insert(0, f'Search "{self.filter_query}"')
        filter_text = f"{separator}{', '.join(active_filters)}" if active_filters else ""
        connection = "tmux connected" if self.tmux_connected else "tmux unavailable"
        if self.has_class("refreshing"):
            connection = "Refreshing sessions..."
        elif self.last_refreshed_at is not None:
            age = int((datetime.now(UTC) - self.last_refreshed_at).total_seconds())
            updated = "Updated now" if age < 1 else f"Updated {age}s ago"
            connection = f"{connection}{separator}{updated}"
        text = Text()
        text.append("ws", "bold #72c78e")
        environment_display = self.service.config.interface.environment_display
        environment = (
            self.service.config.interface.environment_label
            if environment_display == "label"
            else self.hostname
            if environment_display == "hostname"
            else ""
        )
        selected = self._selected()
        if self.narrow_detail_open and selected is not None:
            text.append("  Session  ", "dim")
            text.append(
                selected.tool.value.upper(), tool_style(selected.tool, monochrome=self.monochrome)
            )
            warning = is_warning(selected, self._notice_for(selected))
            available = max(12, self.size.width - 23 - (3 if warning else 0))
            text.append(
                f"  {truncate(selected.name, available, ascii_only=self.ascii_only)}", "bold"
            )
            if warning:
                text.append("  !", f"bold {self._theme_colors.get('warning', 'yellow')}")
        elif self.has_class("very-wide"):
            product = truncate("Workspace Session Manager", 60, ascii_only=self.ascii_only)
            text.append(f"  {product}  v{__version__}")
            text.append(f"    {counts}{filter_text}", "dim")
            if environment:
                text.append(f"    {truncate(environment, 22, ascii_only=self.ascii_only)}")
            text.append(
                f"{separator}{connection}",
                self._theme_colors.get("success", "green")
                if self.tmux_connected
                else self._theme_colors.get("error", "red"),
            )
        elif self.has_class("wide"):
            product = truncate("Workspace Session Manager", 60, ascii_only=self.ascii_only)
            text.append(f"  {product}")
            text.append(f"    {counts}{filter_text}", "dim")
            if environment:
                text.append(f"    {truncate(environment, 18, ascii_only=self.ascii_only)}")
            text.append(
                f"{separator}{connection}",
                self._theme_colors.get("success", "green")
                if self.tmux_connected
                else self._theme_colors.get("error", "red"),
            )
        elif self.has_class("medium"):
            product = truncate("Workspace Session Manager", 60, ascii_only=self.ascii_only)
            text.append(f"  {product}")
            text.append(f"\n{counts}{filter_text}", "dim")
        else:
            text.append(f"  {counts}{filter_text}", "dim")
        self.query_one("#app-header", Static).update(text)

    def _render_action_bar(self) -> None:
        navigation = "Up/Down/jk" if self.ascii_only else "↑↓/jk"
        if self.has_class("searching"):
            query = self.query_one("#search", Input).value
            value = f"SEARCH  {query}_   Enter Apply   Esc Cancel   Ctrl+U Clear"
        elif self.has_class("attention-view"):
            value = (
                f"ATTENTION  {navigation} Nav   Enter Open   Esc Back   "
                "f Filter   r Refresh   ? Help   q Quit"
            )
        elif self.narrow_detail_open:
            selected = self._selected()
            primary = (
                "Enter Manage"
                if selected is not None and selected.runtime is RuntimeState.STOPPED
                else "Enter Attach"
            )
            value = f"Esc Back  {primary}  e Edit  n Task  l Logs  r Reload  * Pin  d Manage"
        elif self.has_class("narrow"):
            value = (
                f"{navigation} Nav   Enter Open   c Create   f Filter   g Group   h Health   ? Help"
            )
        elif self.has_class("medium"):
            value = (
                f"{navigation} Nav   Enter Attach   c Create   / Search   f Filter   "
                "g Group   h Health   d Manage   ? Help"
            )
        else:
            value = (
                f"{navigation} Navigate   Enter Attach   c Create   / Search   "
                "f Filter   g Group   z Density   h Health   p Palette   d Manage   ? Help"
            )
        self.query_one("#action-bar", Static).update(value)

    def action_cursor_down(self) -> None:
        if self.narrow_detail_open:
            self.query_one("#inspector-scroll", VerticalScroll).action_scroll_down()
        else:
            self.query_one("#sessions", OptionList).action_cursor_down()

    def action_cursor_up(self) -> None:
        if self.narrow_detail_open:
            self.query_one("#inspector-scroll", VerticalScroll).action_scroll_up()
        else:
            self.query_one("#sessions", OptionList).action_cursor_up()

    def _select_option(self, option_id: str | None) -> None:
        if option_id is None or option_id not in self._option_sessions:
            return
        session = self._option_sessions[option_id]
        self.selected_name = session.name
        self.selected_session_id = session.session_id
        self._render_details(session.name)

    @on(OptionList.OptionHighlighted, "#sessions")
    def option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        if self._expected_option_id is not None:
            if event.option.id != self._expected_option_id:
                return
            self._expected_option_id = None
        if not self._rendering_options and event.option_index == event.option_list.highlighted:
            self._select_option(event.option.id)

    @on(OptionList.OptionSelected, "#sessions")
    def option_selected(self, event: OptionList.OptionSelected) -> None:
        option_id = event.option.id
        if option_id in self._option_sessions:
            self._select_option(option_id)
            self.action_open()
            return
        action = self._option_actions.get(option_id or "")
        if action:
            getattr(self, f"action_{action}")()

    def _selected(self) -> SessionView | None:
        if self.selected_name is None:
            return None
        return next(
            (
                item
                for item in self.visible_sessions
                if item.name == self.selected_name and item.session_id == self.selected_session_id
            ),
            None,
        )

    def _set_action_buttons_enabled(
        self, enabled: bool, *, pinned: bool = False, resumable: bool = False
    ) -> None:
        for button in self.query(".session-action-button"):
            button.disabled = not enabled
        if enabled:
            self.query_one("#action-pin", Button).label = "Unpin" if pinned else "Pin"
            self.query_one("#action-resume", Button).disabled = not resumable
            self.query_one("#action-open", Button).disabled = resumable

    def _render_empty_state(self) -> None:
        if self.has_class("attention-view"):
            scanned, eligible = self._attention_progress()
            checking = scanned < eligible or bool(self._attention_scan_error)
            self.query_one("#identity", Static).update(
                "Checking session alerts" if checking else "No sessions need attention"
            )
            self.query_one("#overview", Static).update(
                f"Checked {scanned} of {eligible} eligible agent sessions."
                if checking
                else "The current runtime, task, input, and agent states are clear."
            )
            for widget_id in ("#runtime-status", "#activity", "#recent-output"):
                self.query_one(widget_id, Static).update("")
            self.query_one("#output-meta", Static).update("")
            self._set_action_buttons_enabled(False)
            return
        filtered = bool(self.filter_query) or self.filters.active
        self.query_one("#identity", Static).update("No matches" if filtered else "No sessions")
        self.query_one("#overview", Static).update(
            "No managed sessions match the active filter."
            if filtered
            else "Create a managed session to get started."
        )
        for widget_id in ("#runtime-status", "#activity", "#recent-output"):
            self.query_one(widget_id, Static).update("")
        self.query_one("#output-meta", Static).update("")
        self._set_action_buttons_enabled(False)

    def _render_details(self, name: str) -> None:
        self._detail_generation += 1
        generation = self._detail_generation
        self._detail_refreshing = True
        self._load_details(name, generation)

    @work(thread=True, exclusive=True, group="detail-refresh")
    def _load_details(self, name: str, generation: int) -> None:
        try:
            details = self.service.inspect(name)
        except WsError as error:
            self.call_from_thread(self._finish_details, generation, None, str(error))
            return
        self.call_from_thread(self._finish_details, generation, details, "")

    def _finish_details(
        self,
        generation: int,
        details: SessionDetails | None,
        error: str,
    ) -> None:
        if generation != self._detail_generation or not self.is_running:
            return
        self._detail_refreshing = False
        scroller = self.query_one("#inspector-scroll", VerticalScroll)
        output_scroller = self.query_one("#recent-output-scroll", VerticalScroll)
        old_scroll = scroller.scroll_offset.y
        old_output_scroll = output_scroller.scroll_offset.y
        if error:
            self.query_one("#overview", Static).update(error)
            self._set_action_buttons_enabled(False)
            return
        assert details is not None
        session = details.session
        notice = detect_activity(session, details.preview)
        self._store_notice(session, notice, observed_at=datetime.now(UTC))
        self._record_activity_sample((session.name, session.session_id), details.preview)
        if not self._attention_scanning and self._attention_complete():
            self._attention_baseline_established = True
            self._stop_attention_dots()
        separator = " / " if self.ascii_only else " · "
        identity = Text()
        identity.append(
            f"{session.tool.value.upper():<7}",
            style=tool_style(session.tool, monochrome=self.monochrome),
        )
        identity_name = session.name
        alert_title = notice.title
        if self.has_class("narrow"):
            content_width = max(24, self.size.width - 4)
            alert_width = (
                min(len(notice.title) + 4, max(18, content_width // 3)) if notice.warning else 0
            )
            pin_width = 3 if session.pinned else 0
            identity_name = truncate(
                session.name,
                max(12, content_width - 7 - pin_width - alert_width),
                ascii_only=self.ascii_only,
            )
            if notice.warning:
                alert_title = truncate(
                    notice.title,
                    max(12, content_width - 7 - pin_width - len(identity_name) - 4),
                    ascii_only=self.ascii_only,
                )
        identity.append(identity_name, style="bold")
        if session.pinned:
            identity.append(
                "  *" if self.ascii_only else "  ★",
                self._theme_colors.get("accent", "yellow"),
            )
        if notice.warning:
            alert_style = (
                f"bold {self._theme_colors.get('warning', 'yellow')}"
                if notice.level == "warning"
                else f"bold {self._theme_colors.get('error', 'red')}"
            )
            identity.append(f"  ! {alert_title}", alert_style)
        identity.append(
            "\n"
            + separator.join(
                (
                    display_state(session.runtime.value),
                    display_state(session.task_state.value),
                    f"Last active {relative_activity(session.last_active_at)} ago",
                )
            ),
            runtime_style(session.runtime, monochrome=self.monochrome),
        )
        self.query_one("#identity", Static).update(identity)
        overview_values = [
            ("Task", humanize_task(session.note)),
            ("Project", session.project),
            ("Directory", display_path(session.cwd)),
            ("Ownership", "Managed by ws" if session.owned else "Read only"),
            ("Tags", ", ".join(session.tags)),
        ]
        if session.display_name and session.display_name != session.name:
            overview_values.insert(
                0,
                (
                    "Name" if self.has_class("narrow") else "Display name",
                    session.display_name,
                ),
            )
        status_values = [
            ("Runtime", display_state(session.runtime.value)),
            ("Task", display_state(session.task_state.value)),
            ("Agent", display_state(notice.agent_state.value)),
            ("Input", display_input(session.input_state)),
            ("Windows", str(session.windows)),
            ("Logging", "Enabled" if session.logging_enabled else "Disabled"),
            ("Last active", relative_activity(session.last_active_at)),
        ]
        if self.has_class("medium"):
            overview_values = overview_values[:1]
            status_values = status_values[:4]
        self.query_one("#overview", Static).update(labeled_values(overview_values))
        self.query_one("#runtime-status", Static).update(labeled_values(status_values))
        activity = Text(notice.title, style="bold")
        activity.append(f"\n{notice.detail}")
        activity_card = self.query_one("#activity-card", Vertical)
        activity_card.remove_class("warning", "error", "success")
        if notice.level == "warning":
            activity.stylize(self._theme_colors.get("warning", "yellow"), 0, len(notice.title))
            activity_card.add_class("warning")
        elif notice.level == "error":
            activity.stylize(self._theme_colors.get("error", "red"), 0, len(notice.title))
            activity_card.add_class("error")
        else:
            activity_card.add_class("success")
        self.query_one("#activity", Static).update(activity)
        preview = Text()
        if self.output_mode == "summary":
            preview = summarize_output(
                details.preview, notice, self._theme_colors.get("warning", "yellow")
            )
        else:
            if details.preview_truncated:
                preview.append("[older output truncated]\n", "dim")
            preview.append(details.preview or "No pane output")
        self.query_one("#recent-output", Static).update(preview)
        line_count = len(details.preview.splitlines())
        truncated = "truncated" if details.preview_truncated else "complete"
        self.query_one("#output-meta", Static).update(
            f"{self.output_mode.title()}  {line_count} lines  {truncated}  sanitized  l full logs"
        )
        self._set_action_buttons_enabled(
            True,
            pinned=session.pinned,
            resumable=session.runtime in {RuntimeState.STOPPED, RuntimeState.FAILED},
        )
        self.call_after_refresh(scroller.scroll_to, y=old_scroll, animate=False, force=True)
        self.call_after_refresh(
            output_scroller.scroll_to,
            y=old_output_scroll,
            animate=False,
            force=True,
        )
        self._render_header()
        self._render_attention_action()

    @on(Button.Pressed, ".output-mode")
    def output_mode_changed(self, event: Button.Pressed) -> None:
        self.output_mode = "raw" if event.button.id == "output-raw" else "summary"
        for mode in ("summary", "raw"):
            self.query_one(f"#output-{mode}", Button).set_class(self.output_mode == mode, "active")
        if self.selected_name:
            self._render_details(self.selected_name)

    @on(Button.Pressed, ".session-action-button")
    def session_action_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id
        if button_id == "action-open":
            self.action_open()
        elif button_id == "action-resume":
            self.action_resume_selected()
        elif button_id == "action-manage":
            self.action_manage()
        elif button_id == "action-logs":
            self.action_logs()
        elif button_id == "action-pin":
            self.action_toggle_pin()

    @on(Button.Pressed, "#toolbar-create")
    def toolbar_create_pressed(self) -> None:
        self.action_create()

    @on(Button.Pressed, "#toolbar-resume")
    def toolbar_resume_pressed(self) -> None:
        self.action_resume()

    @on(Button.Pressed, "#toolbar-filter")
    def toolbar_filter_pressed(self) -> None:
        self.action_filter()

    @on(Button.Pressed, "#toolbar-group")
    def toolbar_group_pressed(self) -> None:
        self.action_cycle_grouping()

    @on(Button.Pressed, "#toolbar-health")
    def toolbar_health_pressed(self) -> None:
        self.action_health_alerts()

    @on(Input.Changed, "#search")
    def search_changed(self, event: Input.Changed) -> None:
        if not self.has_class("searching"):
            return
        self.filter_query = event.value.strip()
        self._render_options()

    @on(Input.Submitted, "#search")
    def search_submitted(self) -> None:
        self._finish_search()

    def _finish_search(self) -> None:
        self.remove_class("searching")
        self._set_interaction_mode(InteractionMode.NORMAL)
        self.query_one("#sessions", OptionList).focus()
        self._render_header()
        self._render_action_bar()

    def action_search(self) -> None:
        if self.interaction_mode is not InteractionMode.NORMAL:
            return
        if self._attention_context is not None:
            self._restore_attention_view(restore_focus=False)
        if self.narrow_detail_open:
            self._close_narrow_detail(restore_focus=False)
        self._search_before = self.filter_query
        self.add_class("searching")
        self._set_interaction_mode(InteractionMode.SEARCH)
        self.add_class("searching")
        search = self.query_one("#search", Input)
        search.value = self.filter_query
        search.focus()
        self._render_action_bar()

    def action_filter(self) -> None:
        if self.interaction_mode not in {InteractionMode.NORMAL, InteractionMode.SEARCH}:
            return
        if self._attention_context is not None:
            self._restore_attention_view(restore_focus=False)
        if self.narrow_detail_open:
            self._close_narrow_detail()
        self._begin_overlay(InteractionMode.FILTER)
        available_tags = sorted({tag for session in self.sessions for tag in session.tags})
        available_projects = sorted(
            {session.project for session in self.sessions if session.project}
        )
        self.push_screen(
            FilterScreen(
                self.filters,
                available_tags=available_tags,
                available_projects=available_projects,
            ),
            self._apply_filter,
        )

    def _apply_filter(self, filters: FilterState | None) -> None:
        self._restore_dashboard_mode(filters=filters)
        if filters is not None:
            self._render_options()

    def action_search_output(self) -> None:
        if self.interaction_mode not in {InteractionMode.NORMAL, InteractionMode.SEARCH}:
            return
        if self._attention_context is not None:
            self._restore_attention_view(restore_focus=False)
        if self.narrow_detail_open:
            self._close_narrow_detail()
        self._begin_overlay(InteractionMode.FORM)
        self.push_screen(
            SearchOutputScreen(self.service, self.sessions), self._search_output_result
        )

    def _search_output_result(self, _result: None) -> None:
        self._restore_dashboard_mode()

    def action_command_palette(self) -> None:
        if self.interaction_mode not in {InteractionMode.NORMAL, InteractionMode.SEARCH}:
            return
        self._begin_overlay(InteractionMode.PALETTE)
        super().action_command_palette()

    @on(CommandPalette.Opened)
    def command_palette_opened(self) -> None:
        self._set_interaction_mode(InteractionMode.PALETTE)

    @on(CommandPalette.Closed)
    def command_palette_closed(self) -> None:
        self._restore_dashboard_mode()

    def action_recent(self) -> None:
        if self._attention_context is not None:
            self._restore_attention_view(restore_focus=False)
        self.filters = FilterState(recent_only=True)
        self._render_options()

    def action_toggle_unmanaged(self) -> None:
        if self.interaction_mode is not InteractionMode.NORMAL:
            return
        self.show_unmanaged = not self.show_unmanaged
        self.refresh_sessions()
        self.notify(
            "Showing every tmux session, including ones ws doesn't manage."
            if self.show_unmanaged
            else "Back to managed sessions only.",
            timeout=2,
        )

    def action_attention(self) -> None:
        if (
            self.interaction_mode is not InteractionMode.NORMAL
            or self._attention_context is not None
        ):
            return
        context = self._capture_dashboard_context()
        if context.selected_name is not None and context.selected_session_id is not None:
            context = replace(
                context,
                highlighted_option_id=session_option_id(
                    context.selected_name, context.selected_session_id
                ),
            )
        self._attention_context = context
        self._close_narrow_detail(restore_focus=False)
        self.filter_query = ""
        self.filters = FilterState(warnings_only=True)
        with self.prevent(Input.Changed):
            self.query_one("#search", Input).value = ""
        self.add_class("attention-view")
        self._render_options()
        self.query_one("#sessions", OptionList).focus()

    def _restore_attention_view(self, *, restore_focus: bool = True) -> None:
        context = self._attention_context
        self._attention_context = None
        if context is None:
            return
        self.remove_class("attention-view")
        self.filter_query = context.filter_query
        self.filters = context.filters
        self.selected_name = context.selected_name
        self.selected_session_id = context.selected_session_id
        search = self.query_one("#search", Input)
        with self.prevent(Input.Changed):
            search.value = context.search_value
        self._set_interaction_mode(
            InteractionMode.SEARCH if context.searching else InteractionMode.NORMAL
        )
        if context.searching:
            self.add_class("searching")
        self._set_narrow_detail_state(False)
        self._render_options()
        options = self.query_one("#sessions", OptionList)
        option_ids = {
            options.get_option_at_index(index).id for index in range(options.option_count)
        }
        if (
            context.highlighted_option_id is not None
            and context.highlighted_option_id in option_ids
        ):
            options.highlighted = options.get_option_index(context.highlighted_option_id)
        self.call_after_refresh(options.scroll_to, y=context.scroll_y, animate=False, force=True)
        restore_detail = (
            context.narrow_detail_open and self._selected() is not None and self.has_class("narrow")
        )
        self._set_narrow_detail_state(restore_detail)
        if restore_detail:
            inspector = self.query_one("#inspector-scroll", VerticalScroll)
            output = self.query_one("#recent-output-scroll", VerticalScroll)
            self.call_after_refresh(
                inspector.scroll_to,
                y=context.inspector_scroll_y,
                animate=False,
                force=True,
            )
            self.call_after_refresh(
                output.scroll_to,
                y=context.output_scroll_y,
                animate=False,
                force=True,
            )
        if restore_focus:
            focus_target: Widget = options
            if context.searching:
                focus_target = search
            elif restore_detail:
                focus_target = self.query_one("#inspector-scroll", VerticalScroll)
            elif context.focused_id:
                matches = self.query(f"#{context.focused_id}")
                if matches:
                    focus_target = matches.first()
            self.call_after_refresh(focus_target.focus)
        self._render_header()
        self._render_action_bar()

    def action_cycle_theme(self) -> None:
        if self._no_color_forced:
            self.notify("NO_COLOR keeps the interface in monochrome mode.")
            return
        self.ui_theme = THEME_MODES[(THEME_MODES.index(self.ui_theme) + 1) % len(THEME_MODES)]
        self.monochrome = self.ui_theme == "monochrome"
        self.theme = self.ui_theme
        self._refresh_theme_colors()
        self._set_layout_classes(self.size.width, self.size.height)
        self._render_health_row()
        self._render_options()
        self.notify(f"Theme: {self.ui_theme}")

    def action_escape(self) -> None:
        if self.has_class("searching"):
            self.filter_query = self._search_before
            self.query_one("#search", Input).value = self.filter_query
            self._finish_search()
            self._render_options()
        elif self.narrow_detail_open:
            self._close_narrow_detail()
        elif self._attention_context is not None:
            self._restore_attention_view()

    def action_open(self) -> None:
        session = self._selected()
        if session is None:
            return
        if self.has_class("narrow") and not self.narrow_detail_open:
            self._open_narrow_detail()
            return
        if session.runtime in {RuntimeState.STOPPED, RuntimeState.FAILED}:
            # Keep Enter's established protected-management behavior. Resume is
            # deliberately available as its own visible action in the inspector.
            self.action_manage()
            return
        self.exit(session.name)

    def action_attach(self) -> None:
        self.action_open()

    def action_create(self, tool: Tool = Tool.CLAUDE) -> None:
        if self.interaction_mode not in {InteractionMode.NORMAL, InteractionMode.SEARCH}:
            return
        self._begin_overlay(InteractionMode.FORM)
        self.push_screen(
            CreateSessionScreen(self.default_cwd, self.service, tool), self._create_session
        )

    def action_new_session(self) -> None:
        self.action_create()

    def action_shell(self) -> None:
        self.action_create(Tool.SHELL)

    def action_resume(self) -> None:
        try:
            target = self.service.resume_target()
        except WsError as error:
            self.notify(str(error), severity="warning")
            return
        self.exit(target.name)

    def action_resume_selected(self) -> None:
        """Expose the existing protected restart flow as a primary inspector action."""
        session = self._selected()
        if session is None:
            return
        if session.runtime not in {RuntimeState.STOPPED, RuntimeState.FAILED}:
            self.notify("This session is already available to attach.", severity="information")
            return
        self._begin_overlay(InteractionMode.MANAGE)
        self._open_manage_confirmation(session, "restart", ManageListState())

    def action_cycle_grouping(self) -> None:
        current = GROUPING_MODES.index(self.grouping)
        self.grouping = GROUPING_MODES[(current + 1) % len(GROUPING_MODES)]
        self._persist_interface_preferences()
        self._render_options()
        self.notify(f"Grouping: {GROUPING_LABELS[self.grouping]}")

    def action_toggle_density(self) -> None:
        self.density = "compact" if self.density == "comfortable" else "comfortable"
        self.set_class(self.density == "compact", "compact-density")
        self._persist_interface_preferences()
        self._render_options()
        self.notify(f"Density: {self.density}")

    def _restore_create_mode(self) -> None:
        self._restore_dashboard_mode()

    def _insert_created_session(self, session: SessionView) -> None:
        self.sessions.insert(0, session)
        self.selected_name = session.name
        self.selected_session_id = session.session_id
        self._render_options()
        option_id = session_option_id(session.name, session.session_id)
        options = self.query_one("#sessions", OptionList)
        if option_id in self._option_sessions:
            options.highlighted = options.get_option_index(option_id)
            self._select_option(option_id)
            self.call_after_refresh(options.scroll_to_highlight)
        if self.motion != "off":
            self.call_after_refresh(
                self._highlight_created_session, session.name, session.session_id
            )
        self.last_refreshed_at = datetime.now(UTC)
        self._render_header()
        self._render_action_bar()

    def _highlight_created_session(self, name: str, session_id: str) -> None:
        bright = self._theme_colors.get("primary", "#243d55")
        dim = self._theme_colors.get("primary-muted", "#1a2c3d")
        self._flash_session_row(name, session_id, f"on {bright}")
        fade_delay = 0.35 if self.motion == "full" else 0.25
        self.set_timer(fade_delay, lambda: self._flash_session_row(name, session_id, f"on {dim}"))
        self.set_timer(fade_delay + 0.25, lambda: self._restore_session_row(name, session_id))

    def _flash_session_row(self, name: str, session_id: str, style: str) -> None:
        if not self.is_running:
            return
        session = next(
            (item for item in self.sessions if item.name == name and item.session_id == session_id),
            None,
        )
        if session is None:
            return
        option_id = session_option_id(session.name, session_id)
        if option_id not in self._option_sessions:
            return
        prompt = session_row(
            session,
            self._row_width(),
            ascii_only=self.ascii_only,
            monochrome=self.monochrome,
            activity_spark=self._activity_spark_for(session),
            warning_color=self._theme_colors.get("warning", "yellow"),
            warning_dim_color=self._theme_colors.get("warning-muted", "#8a7a2a"),
            notice=self._notice_for(session),
            bulk_mode=bool(self._marked),
            marked=self._is_marked(session),
        )
        prompt.stylize(style)
        self.query_one("#sessions", OptionList).replace_option_prompt(option_id, prompt)

    def _create_session(self, result: CreateFormResult | None) -> None:
        if result is None:
            self._restore_create_mode()
            return
        self._attempt_create(result)

    def _attempt_create(self, result: CreateFormResult) -> None:
        try:
            session = self.service.create(result.request)
        except WsError as error:
            self._failed_create_result = result
            try:
                session_name = normalized_session_name(
                    result.request.tool,
                    result.request.name,
                    automatic_prefix=result.request.automatic_prefix,
                )
                metadata_exists = self.service.store.load(session_name) is not None
            except WsError:
                session_name = result.request.name
                metadata_exists = False
            self._set_interaction_mode(InteractionMode.CONFIRMATION)
            self.notify(
                f"{error}\nRetry, open details, or remove partial metadata if present.",
                title="Session startup failed",
                severity="error",
                timeout=0,
            )
            self.push_screen(
                CreateFailureScreen(
                    session_name,
                    str(error),
                    metadata_exists=metadata_exists,
                ),
                self._creation_failure_action,
            )
            return
        self._failed_create_result = None
        self._restore_create_mode()
        self._insert_created_session(session)
        self._notify_success(f"Session created: {session.name}", title="Session ready")
        if result.start_attached:
            self.exit(session.name)

    def _creation_failure_action(self, action: str | None) -> None:
        result = self._failed_create_result
        if action == "retry" and result is not None:
            self._set_interaction_mode(InteractionMode.FORM)
            self._attempt_create(result)
            return
        if action == "remove" and result is not None:
            try:
                session_name = normalized_session_name(
                    result.request.tool,
                    result.request.name,
                    automatic_prefix=result.request.automatic_prefix,
                )
                self.service.remove_metadata(session_name)
                self._notify_success(f"Metadata removed: {session_name}")
            except WsError as error:
                self.notify(str(error), title="Metadata removal failed", severity="error")
        self._failed_create_result = None
        if action == "details" and result is not None:
            self._set_interaction_mode(InteractionMode.FORM)
            self.call_after_refresh(self._open_startup_failure_details)
        else:
            self._restore_create_mode()

    def _open_startup_failure_details(self) -> None:
        self.push_screen(
            MessageScreen(
                "Startup Failure Details",
                "The tool process did not start. No active session was adopted or renamed.\n\n"
                "Run System Diagnostics, verify the configured executable, and retry creation.",
            ),
            lambda _result: self._restore_dashboard_mode(),
        )

    def action_edit(self) -> None:
        session = self._selected()
        if session:
            self._begin_overlay(InteractionMode.FORM)
            self.push_screen(
                IdentityOrganizationScreen(self.service, session),
                lambda result, target=session: self._save_identity(result, target),
            )

    def _save_identity(
        self,
        result: OrganizationEditResult | None,
        session: SessionView,
        manage_state: ManageListState | None = None,
    ) -> None:
        if result is None:
            if manage_state is None:
                self._restore_dashboard_mode()
            else:
                self._return_to_manage(session, manage_state)
            return
        try:
            updated = session
            if result.name != session.name:
                updated = self.service.rename(session.name, result.name)
            updated = self.service.organize(
                updated.name,
                display_name=result.display_name,
                tags=result.tags,
                project=result.project,
            )
        except WsError as error:
            self.notify(str(error), title="Save failed", severity="error")
            if manage_state is None:
                self._restore_dashboard_mode()
            else:
                self._return_to_manage(session, manage_state)
            return
        if manage_state is None:
            self._restore_dashboard_mode()
        self.selected_name = updated.name
        self.selected_session_id = updated.session_id
        self._notify_success("Identity and organization updated")
        self.refresh_sessions()
        if manage_state is not None:
            self._return_to_manage(updated, manage_state)

    def action_note(self) -> None:
        session = self._selected()
        if session:
            self._begin_overlay(InteractionMode.FORM)
            self.push_screen(
                NoteScreen(session),
                lambda note, target=session: self._save_note(note, target),
            )

    def _save_note(
        self,
        note: str | None,
        session: SessionView,
        manage_state: ManageListState | None = None,
    ) -> None:
        if note is None:
            if manage_state is None:
                self._restore_dashboard_mode()
            else:
                self._return_to_manage(session, manage_state)
            return
        try:
            updated = self.service.update_note(session.name, note)
        except WsError as error:
            self.notify(str(error), title="Save failed", severity="error")
            if manage_state is None:
                self._restore_dashboard_mode()
            else:
                self._return_to_manage(session, manage_state)
            return
        if manage_state is None:
            self._restore_dashboard_mode()
        self._notify_success("Task updated")
        self.refresh_sessions()
        if manage_state is not None:
            self._return_to_manage(updated, manage_state)

    def _save_status(
        self,
        result: StatusEditResult | None,
        session: SessionView,
        manage_state: ManageListState,
    ) -> None:
        if result is None:
            self._return_to_manage(session, manage_state)
            return
        try:
            updated = self.service.organize(
                session.name,
                state=result.task_state,
                input_state=result.input_state,
            )
        except WsError as error:
            self.notify(str(error), title="Status update failed", severity="error")
            self._return_to_manage(session, manage_state)
            return
        self.selected_name = updated.name
        self.selected_session_id = updated.session_id
        self._notify_success("Task and input status updated")
        self.refresh_sessions()
        self._return_to_manage(updated, manage_state)

    def action_logs(self) -> None:
        session = self._selected()
        if session:
            self._begin_overlay(InteractionMode.FORM)
            self.push_screen(LogScreen(self.service, session), self._logs_result)

    def _logs_result(self, target: str | None) -> None:
        self._restore_dashboard_mode()
        if target:
            self.exit(target)

    def action_toggle_pin(self) -> None:
        session = self._selected()
        if session is None:
            return
        self._toggle_pin(session)

    def _toggle_pin(self, session: SessionView) -> None:
        try:
            updated = self.service.organize(session.name, pinned=not session.pinned)
        except WsError as error:
            self.notify(str(error), severity="warning")
            return
        self.selected_name = updated.name
        self.selected_session_id = updated.session_id
        self._notify_success("Session unpinned" if session.pinned else "Session pinned")
        self.refresh_sessions()

    def action_toggle_mark(self) -> None:
        session = self._selected()
        if session is None:
            return
        identity = (session.name, session.session_id)
        if identity in self._marked:
            self._marked.discard(identity)
        else:
            self._marked.add(identity)
        self._render_options()

    def action_bulk_actions(self) -> None:
        if not self._marked:
            self.notify("Mark a session with space first.", title="No sessions marked")
            return
        self._begin_overlay(InteractionMode.MANAGE)
        self.push_screen(BulkActionsScreen(len(self._marked)), self._bulk_action_chosen)

    def _marked_names(self) -> list[str]:
        marked_ids = self._marked
        return [
            session.name
            for session in self.sessions
            if (session.name, session.session_id) in marked_ids
        ]

    def _bulk_action_chosen(self, action: str | None) -> None:
        if action is None:
            self._restore_dashboard_mode()
            return
        if action == "clear-marks":
            self._marked.clear()
            self._restore_dashboard_mode()
            self.refresh_sessions()
            self._notify_success("Marks cleared")
            return
        if action == "add-tag":
            self._set_interaction_mode(InteractionMode.FORM)
            self.call_after_refresh(self._open_bulk_tag_screen)
            return
        self._set_interaction_mode(InteractionMode.CONFIRMATION)
        self.call_after_refresh(self._open_bulk_confirmation, action)

    def _open_bulk_tag_screen(self) -> None:
        self.push_screen(BulkTagScreen(len(self._marked)), self._bulk_tag_chosen)

    def _bulk_tag_chosen(self, tag: str | None) -> None:
        if tag is None:
            self._restore_dashboard_mode()
            return
        succeeded = 0
        failed = 0
        for name in self._marked_names():
            try:
                current = self.service.get(name)
                tags = normalize_tags([*current.tags, tag])
                self.service.organize(name, tags=tags)
            except (WsError, ValueError):
                failed += 1
            else:
                succeeded += 1
        self._marked.clear()
        self._restore_dashboard_mode()
        self.refresh_sessions()
        self._notify_bulk_result("Tagged", succeeded, failed)

    def _open_bulk_confirmation(self, action: str) -> None:
        names = self._marked_names()
        joined = _format_marked_names(names)
        if action == "stop":
            title, consequence, confirm_label = (
                "Stop Sessions",
                "The tmux sessions will stop. ws metadata and sanitized logs are retained.",
                "Stop Sessions",
            )
        else:
            title, consequence, confirm_label = (
                "Delete Sessions",
                "tmux will stop and ws metadata and logs will be permanently removed.",
                "Delete Sessions",
            )
        screen = ConfirmActionScreen(title, joined, consequence, confirm_label=confirm_label)
        self.push_screen(
            screen,
            lambda confirmed, selected=action: self._bulk_confirmation_result(selected, confirmed),
        )

    def _bulk_confirmation_result(self, action: str, confirmed: bool) -> None:
        if not confirmed:
            self._restore_dashboard_mode()
            return
        names = self._marked_names()
        succeeded = 0
        failed = 0
        for name in names:
            try:
                if action == "stop":
                    self.service.stop_session(name)
                else:
                    self.service.delete(name)
            except (WsError, OSError):
                failed += 1
            else:
                succeeded += 1
        self._marked.clear()
        self._restore_dashboard_mode()
        self.refresh_sessions()
        self._notify_bulk_result("Stopped" if action == "stop" else "Deleted", succeeded, failed)

    def _notify_bulk_result(self, verb: str, succeeded: int, failed: int) -> None:
        if failed:
            self.notify(
                f"{verb} {succeeded} of {succeeded + failed} session(s); {failed} failed",
                title="Bulk action partially completed",
                severity="warning",
            )
        elif succeeded:
            self._notify_success(f"{verb} {succeeded} session(s)")
        else:
            self.notify("Nothing to do", title="Bulk action")

    def action_manage(self) -> None:
        session = self._selected()
        if session:
            self._begin_overlay(InteractionMode.MANAGE)
            self._open_manage_screen(session)

    def _open_manage_screen(
        self,
        session: SessionView,
        *,
        state: ManageListState | None = None,
    ) -> None:
        current = self._current_session(session)
        if current is None:
            self._restore_dashboard_mode()
            self.notify(
                "The selected session changed or disappeared during the operation.",
                title="Session unavailable",
                severity="warning",
            )
            return
        self._set_interaction_mode(InteractionMode.MANAGE)
        self.push_screen(
            ManageSessionScreen(current, state=state),
            lambda selection, target=current: self._manage_action(target, selection),
        )

    def action_more_actions(self) -> None:
        self.action_manage()

    def action_delete_session(self) -> None:
        self.action_manage()

    def _current_session(self, target: SessionView) -> SessionView | None:
        return next(
            (
                session
                for session in self.sessions
                if session.name == target.name and session.session_id == target.session_id
            ),
            None,
        )

    def _manage_action(self, session: SessionView, selection: ManageSelection | None) -> None:
        if selection is None:
            self._restore_dashboard_mode()
            return
        action = selection.action
        state = selection.state
        if action == "identity":
            self._set_interaction_mode(InteractionMode.FORM)
            self.call_after_refresh(self._open_identity_screen, session, state)
        elif action == "task":
            self._set_interaction_mode(InteractionMode.FORM)
            self.call_after_refresh(self._open_note_screen, session, state)
        elif action == "status":
            self._set_interaction_mode(InteractionMode.FORM)
            self.call_after_refresh(self._open_status_screen, session, state)
        elif action == "pin":
            try:
                updated = self.service.organize(session.name, pinned=not session.pinned)
            except WsError as error:
                self.notify(str(error), severity="warning")
                self._return_to_manage(session, state)
                return
            self.selected_name = updated.name
            self.selected_session_id = updated.session_id
            self._notify_success("Session unpinned" if session.pinned else "Session pinned")
            self.refresh_sessions()
            self._return_to_manage(updated, state)
        elif action == "advanced":
            self._set_interaction_mode(InteractionMode.FORM)
            self.call_after_refresh(self._open_advanced_screen, session, state)
        elif action == "clone":
            self._set_interaction_mode(InteractionMode.FORM)
            self.call_after_refresh(self._open_clone_screen, session, state)
        elif action == "logging":
            try:
                updated = self.service.set_logging(session.name, not session.logging_enabled)
            except (WsError, OSError) as error:
                self.notify(str(error), title="Logging update failed", severity="error")
                self._return_to_manage(session, state)
                return
            self.selected_name = updated.name
            self.selected_session_id = updated.session_id
            self._notify_success(
                "Logging enabled" if updated.logging_enabled else "Logging disabled"
            )
            self.refresh_sessions()
            self._return_to_manage(updated, state)
        else:
            self._set_interaction_mode(InteractionMode.CONFIRMATION)
            self.call_after_refresh(self._open_manage_confirmation, session, action, state)

    def _return_to_manage(self, session: SessionView, state: ManageListState) -> None:
        if self._mode_context is not None:
            self._mode_context = replace(
                self._mode_context,
                selected_name=session.name,
                selected_session_id=session.session_id,
            )
        self._set_interaction_mode(InteractionMode.MANAGE)
        self.call_after_refresh(self._open_manage_screen, session, state=state)

    def _open_identity_screen(self, session: SessionView, state: ManageListState) -> None:
        current = self._current_session(session)
        if current is None:
            self._restore_dashboard_mode()
            return
        self.push_screen(
            IdentityOrganizationScreen(self.service, current),
            lambda result, target=current, context=state: self._save_identity(
                result, target, context
            ),
        )

    def _open_note_screen(self, session: SessionView, state: ManageListState) -> None:
        current = self._current_session(session)
        if current is None:
            self._restore_dashboard_mode()
            return
        self.push_screen(
            NoteScreen(current),
            lambda note, target=current, context=state: self._save_note(note, target, context),
        )

    def _open_status_screen(self, session: SessionView, state: ManageListState) -> None:
        current = self._current_session(session)
        if current is None:
            self._restore_dashboard_mode()
            return
        self.push_screen(
            StatusScreen(current),
            lambda result, target=current, context=state: self._save_status(
                result, target, context
            ),
        )

    def _open_clone_screen(self, session: SessionView, state: ManageListState) -> None:
        del state
        current = self._current_session(session)
        if current is None:
            self._restore_dashboard_mode()
            return
        self.push_screen(
            CreateSessionScreen(self.default_cwd, self.service, current.tool, template=current),
            self._create_session,
        )

    def _open_advanced_screen(
        self, session: SessionView, state: ManageListState | None = None
    ) -> None:
        current = self._current_session(session)
        if current is None:
            self._restore_dashboard_mode()
            return
        self.push_screen(
            MessageScreen("Advanced Details", advanced_document(current)),
            lambda _result, target=current, context=state: (
                self._restore_dashboard_mode()
                if context is None
                else self._return_to_manage(target, context)
            ),
        )

    def _open_manage_confirmation(
        self,
        session: SessionView,
        action: str,
        state: ManageListState,
    ) -> None:
        current = self._current_session(session)
        if current is None:
            self._restore_dashboard_mode()
            return
        if action == "delete":
            screen: ModalScreen[bool] = DeleteSessionScreen(current.name)
        else:
            confirmations = {
                "restart": (
                    "Restart Tool",
                    "The current pane command will be replaced and restarted.",
                    "Restart Tool",
                ),
                "stop-command": (
                    "Stop Command",
                    "ws will send Ctrl+C to the active pane. The tmux session remains available.",
                    "Stop Command",
                ),
                "stop-session": (
                    "Stop tmux Session",
                    "The tmux session will stop. ws metadata and sanitized logs are retained.",
                    "Stop Session",
                ),
                "remove-metadata": (
                    "Remove ws Metadata",
                    "The tmux session remains running but disappears from managed ws views.",
                    "Remove Metadata",
                ),
                "delete-logs": (
                    "Delete Logs",
                    "Persisted sanitized logs will be permanently removed.",
                    "Delete Logs",
                ),
            }
            title, consequence, confirm_label = confirmations[action]
            screen = ConfirmActionScreen(
                title, current.name, consequence, confirm_label=confirm_label
            )
        self.push_screen(
            screen,
            lambda confirmed, selected=action, target=current, context=state: (
                self._manage_confirmation_result(target, selected, confirmed, context)
            ),
        )

    def _manage_confirmation_result(
        self,
        session: SessionView,
        action: str,
        confirmed: bool,
        state: ManageListState,
    ) -> None:
        if not confirmed:
            self._return_to_manage(session, state)
            return
        self._restore_dashboard_mode()
        if action == "delete":
            self._delete_session(session)
        else:
            self._confirmed_manage(action, session.name)

    def _confirmed_manage(self, action: str, name: str) -> None:
        try:
            if action == "restart":
                updated = self.service.restart(name)
                self.selected_name = updated.name
                self.selected_session_id = updated.session_id
                message = "Session restarted"
            elif action == "stop-command":
                self.service.stop_command(name)
                message = "Interrupt sent"
            elif action == "stop-session":
                updated = self.service.stop_session(name)
                self.selected_name = updated.name
                self.selected_session_id = updated.session_id
                message = "tmux session stopped; metadata retained"
            elif action == "remove-metadata":
                self.service.remove_metadata(name)
                self.selected_name = None
                self.selected_session_id = None
                message = "ws metadata removed"
            elif action == "delete-logs":
                self.service.delete_logs(name)
                message = "Sanitized logs deleted"
            else:
                return
        except (WsError, OSError) as error:
            self.notify(str(error), title=f"{display_state(action)} failed", severity="error")
            return
        self._notify_success(message)
        self.refresh_sessions()

    def action_advanced_details(self) -> None:
        session = self._selected()
        if session:
            self._begin_overlay(InteractionMode.FORM)
            self._open_advanced_screen(session)

    def _delete_session(self, session: SessionView) -> None:
        try:
            self.service.delete(session.name)
        except (WsError, OSError) as error:
            self.notify(str(error), title="Delete failed", severity="error")
            return
        self.selected_name = None
        self.selected_session_id = None
        self._notify_success(f"Deleted {session.name}")
        self.refresh_sessions()

    def action_diagnostics(self) -> None:
        self._begin_overlay(InteractionMode.FORM)
        self.push_screen(
            DiagnosticsScreen(self.service),
            lambda _result: self._restore_dashboard_mode(),
        )

    def action_health_alerts(self) -> None:
        self._begin_overlay(InteractionMode.FORM)
        self.push_screen(
            HealthAlertsScreen(self.service),
            self._finish_health_alerts,
        )

    def _finish_health_alerts(self, action: str | None) -> None:
        self._restore_dashboard_mode()
        if action == "view-sessions":
            self.call_after_refresh(self.action_attention)
        elif action == "dismiss":
            self._health_dismissed_until_refresh = True
            self._render_health_row()

    def action_help(self) -> None:
        content = (
            "Up/Down or j/k  Navigate warnings\n"
            "Enter    Attach or open details\n"
            "Esc      Return to the previous dashboard view\n"
            "f        Open full filters\n"
            "r        Refresh inventory and alerts\n"
            "p        Command palette\n"
            "q        Quit"
            if self.has_class("attention-view")
            else "Up/Down or j/k  Scroll details\n"
            "Esc      Return to sessions\n"
            "Enter    Attach or manage a stopped session\n"
            "e        Edit identity and organization\n"
            "n        Edit task\n"
            "l        Open full Logs workspace\n"
            "*        Toggle pin\n"
            "d        Manage session\n"
            "r        Refresh\n"
            "g        Cycle grouping\n"
            "z        Toggle compact/comfortable density\n"
            "h        Open System Health\n"
            "t        Cycle color theme\n"
            "q        Quit"
            if self.narrow_detail_open
            else "Up/Down or j/k  Navigate\n"
            "Enter    Attach or open details\n"
            "c        Create session\n"
            "/        Search\n"
            "f        Filter sessions\n"
            "e        Edit selected session\n"
            "n        Edit task\n"
            "l        View logs\n"
            "*        Toggle pin\n"
            "d        Manage session\n"
            "u        Show all tmux sessions (unmanaged too)\n"
            "r        Refresh\n"
            "g        Cycle grouping\n"
            "z        Toggle compact/comfortable density\n"
            "h        Open System Health\n"
            "p        Command palette\n"
            "t        Cycle color theme\n"
            "q        Quit"
        )
        self._begin_overlay(InteractionMode.FORM)
        self.push_screen(
            MessageScreen(
                "Attention help"
                if self.has_class("attention-view")
                else "Session detail help"
                if self.narrow_detail_open
                else "Keyboard help",
                content,
            ),
            lambda _result: self._restore_dashboard_mode(),
        )

    def action_refresh(self) -> None:
        self.refresh_sessions()
