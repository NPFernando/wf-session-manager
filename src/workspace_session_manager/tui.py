"""Responsive Textual dashboard for persistent workflow sessions."""

from __future__ import annotations

import base64
import contextlib
import json
import locale
import os
import re
import shlex
import socket
import sys
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from difflib import SequenceMatcher
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
    Preset,
    ProjectVisualProfile,
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
    default_enabled_tool,
    normalized_session_name,
)
from workspace_session_manager.store import InterfacePreferencesStore

BindingSpec = Binding | tuple[str, str] | tuple[str, str, str]
RECENT_WINDOW = timedelta(hours=24)
GroupingMode = Literal["attention", "runtime", "agent", "project", "warning", "recent"]
DensityMode = Literal["compact", "comfortable"]
TextScaleMode = Literal["compact", "comfortable", "readable"]
MotionPresetMode = Literal["auto", "off", "subtle", "full"]
AccentMode = Literal["default", "vivid", "calm", "safe"]
HintLevel = Literal["minimal", "verbose"]
HintProfile = Literal["beginner", "advanced"]
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
    "contrast-dark",
    "light",
    "pastel-light",
    "monochrome",
    "midnight",
    "night-owl",
    "cyberpunk",
    "terminal",
    "terminal-green",
    "paper",
)
TEXT_SCALE_MODES: tuple[TextScaleMode, ...] = ("compact", "comfortable", "readable")
MOTION_PRESET_MODES: tuple[MotionPresetMode, ...] = ("auto", "off", "subtle", "full")
ACCENT_MODES: tuple[AccentMode, ...] = ("default", "vivid", "calm", "safe")
HINT_PROFILES: tuple[HintProfile, ...] = ("beginner", "advanced")


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
        "contrast-dark": _ThemePalette(
            primary="#5ea6ff",
            accent="#7ecbff",
            background="#050607",
            surface="#0a0e12",
            panel="#10161c",
            foreground="#f4f8fb",
            dark=True,
            warning="#ffd166",
            error="#ff6b6b",
            success="#7be495",
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
        "night-owl": _ThemePalette(
            primary="#6b8ed6",
            accent="#91a7d6",
            background="#101522",
            surface="#151c2b",
            panel="#1d2434",
            foreground="#dce4f2",
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
        "terminal-green": _ThemePalette(
            primary="#00c853",
            accent="#76ff03",
            background="#050905",
            surface="#091109",
            panel="#0e170e",
            foreground="#d8ffd8",
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
        "pastel-light": _ThemePalette(
            primary="#7b8fb8",
            accent="#b089a6",
            background="#f9f7fb",
            surface="#f1edf6",
            panel="#ebe5f2",
            foreground="#2d2a32",
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
LAYOUT_PRESETS: tuple[tuple[DensityMode, TextScaleMode], ...] = (
    ("compact", "compact"),
    ("comfortable", "comfortable"),
    ("comfortable", "readable"),
)
PALETTE_ALIASES: dict[str, tuple[str, ...]] = {
    "create · session": ("new", "new session", "start session", "spawn"),
    "dashboard · refresh sessions": ("reload", "rescan", "sync"),
    "dashboard · filter sessions": ("narrow", "scope", "filter view"),
    "dashboard · search output": ("grep", "find logs", "output search"),
    "interface · open controls panel": ("settings", "preferences", "ui settings"),
    "selected · open logs": ("tail", "stream", "show logs"),
}


def normalize_palette_key(value: str) -> str:
    compact = re.sub(r"^\s*(\[[^\]]+\]|[^\w\s])\s*", "", value.strip())
    return re.sub(r"\s+", " ", compact).strip().casefold()


def modal_breadcrumb(*segments: str) -> str:
    cleaned = [segment.strip() for segment in segments if segment.strip()]
    if not cleaned:
        return "Dashboard"
    return " -> ".join(cleaned)


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
    r"(?i)^(?:claude|copilot|codex|hermes)\s+task:\s*(?P<task>.+?)(?:\s+\([^)]*\))?$"
)
TOOL_LABELS = {
    Tool.CLAUDE: "Claude Code",
    Tool.COPILOT: "Copilot",
    Tool.CODEX: "Codex",
    Tool.HERMES: "Hermes",
    Tool.SHELL: "Shell",
}
TOOL_CREATE_HINTS = {
    Tool.CLAUDE: "Ensure `claude` is authenticated before attaching.",
    Tool.COPILOT: "Ensure `copilot` is authenticated (`copilot auth status`).",
    Tool.CODEX: "Ensure `codex` is authenticated before attaching.",
    Tool.HERMES: "Ensure Hermes gateway/dashboard are reachable before attaching.",
    Tool.SHELL: "Use this for diagnostics and one-off commands without an AI agent.",
}
TOOL_STYLES = {
    Tool.CLAUDE: "bold #c792ea",
    Tool.COPILOT: "bold #8a7fff",
    Tool.CODEX: "bold #66aaff",
    Tool.HERMES: "bold #e9b44c",
    Tool.SHELL: "bold #72c78e",
}
CREATE_TOOL_SHORT_LABELS = {
    Tool.CLAUDE: "Claude",
    Tool.COPILOT: "Copilot",
    Tool.CODEX: "Codex",
    Tool.HERMES: "Hermes",
    Tool.SHELL: "Shell",
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
class JobNotice:
    label: str
    severity: Literal["success", "error", "info"] = "info"
    at: datetime = field(default_factory=lambda: datetime.now(UTC))


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
                for item in (Tool.CLAUDE, Tool.COPILOT, Tool.CODEX, Tool.HERMES)
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


def _snapshot_now_from_env() -> datetime | None:
    if os.environ.get("WS_SNAPSHOT_MODE", "").strip().lower() not in {
        "1",
        "true",
        "yes",
        "on",
    }:
        return None
    value = os.environ.get("WS_SNAPSHOT_NOW", "").strip()
    if value:
        with contextlib.suppress(ValueError):
            parsed = datetime.fromisoformat(value)
            return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
    return datetime(2099, 1, 1, tzinfo=UTC)


def ui_now_utc() -> datetime:
    return _snapshot_now_from_env() or datetime.now(UTC)


def relative_activity(value: datetime | None, *, now: datetime | None = None) -> str:
    if value is None:
        return "unknown"
    current = now or ui_now_utc()
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


def condensed_session_label(session: SessionView) -> str:
    """Create a compact, human-friendly session label while preserving full ID elsewhere."""
    display = session.display_name.strip()
    if display and (" " in display or "/" in display or len(display) < 32):
        return display
    source = display or session.name
    prefix = f"{session.tool.value}-"
    raw = source.removeprefix(prefix)
    tokens = [token for token in raw.split("-") if token]
    if not tokens:
        return session.name
    filler = {
        "http",
        "https",
        "www",
        "com",
        "org",
        "net",
        "io",
        "co",
        "app",
        "dev",
        "ai",
        "main",
        "session",
    }
    meaningful = [token for token in tokens if token not in filler]
    if not meaningful:
        meaningful = tokens
    if len(meaningful) == 1:
        return f"{session.tool.value} · {meaningful[0]}"
    first = meaningful[0]
    tail = "-".join(meaningful[-2:]) if len(meaningful) >= 3 else meaningful[-1]
    return f"{session.tool.value} · {first} / {tail}"


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
    current = now or ui_now_utc()
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
            elif session.input_state is InputState.REQUIRED or session.task_state in {
                TaskState.NEEDS_INPUT,
                TaskState.BLOCKED,
            }:
                label = "BLOCKED"
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
RECENT_JOB_LIMIT = 6


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
    task_kind = (
        "active"
        if session.task_state is TaskState.COMPLETED
        else "waiting"
        if session.task_state in {TaskState.BLOCKED, TaskState.NEEDS_INPUT, TaskState.WAITING}
        else "info"
    )
    result.append_text(
        status_badge(
            display_state(session.runtime.value),
            kind=runtime_kind,
            ascii_only=ascii_only,
            monochrome=monochrome,
        )
    )
    result.append("  ")
    result.append_text(
        status_badge(
            display_state(session.task_state.value),
            kind=task_kind,
            ascii_only=ascii_only,
            monochrome=monochrome,
        )
    )
    result.append("  ")
    result.append_text(
        status_badge(
            display_state(notice.agent_state.value),
            kind="agent",
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
    if notice.warning:
        result.append("  ")
        result.append_text(
            status_badge(
                notice.title, kind=notice.level, ascii_only=ascii_only, monochrome=monochrome
            )
        )
    return result


def status_chip_line(
    session: SessionView,
    notice: ActivityNotice,
    *,
    ascii_only: bool,
    monochrome: bool,
) -> Text:
    chips = session_status_badges(
        session,
        notice,
        ascii_only=ascii_only,
        monochrome=monochrome,
    )
    if chips:
        chips.append("\n", style="dim")
    return chips


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
    compact: bool = False,
    pulse_active: bool = False,
    now: datetime | None = None,
) -> Text:
    """Build a stable, compact row sized to its current pane."""
    alert = notice or detect_activity(session, "")
    if alert.level == "error":
        marker = "x" if ascii_only else "\u00d7"
    elif alert.warning:
        marker = "!"
    elif session.runtime is RuntimeState.ATTACHED:
        marker = "o" if ascii_only else ("◉" if pulse_active else "●")
    else:
        marker = "o" if ascii_only else "○"
    tool = session.tool.value.upper()[:7]
    state = (
        "stopped"
        if session.runtime in {RuntimeState.STOPPED, RuntimeState.FAILED}
        else session.runtime.value
    )
    when = relative_activity(session.last_active_at, now=now)
    name_label = condensed_session_label(session)
    state_width = max(8, len(state))
    name_width = max(10, width - len(tool) - len(when) - state_width - 10)
    name = truncate(name_label, name_width, ascii_only=ascii_only)
    row = Text()
    marker_style = (
        ""
        if marker in {" ", "○", "o"}
        else warning_dim_color
        if ((marker == "!" or marker == "\u00d7") and pulse_dim)
        else warning_color
    )
    row.append(f"{marker} ", style=marker_style)
    row.append(f"{tool:<7}", style=tool_style(session.tool, monochrome=monochrome))
    row.append(f" {name:<{name_width}}", style="bold")
    row.append(f" {state:<{state_width}}", runtime_style(session.runtime, monochrome=monochrome))
    row.append(f" {when}", style="dim")
    if not compact and activity_spark:
        row.append(" ")
        row.append(activity_spark, style="dim")
    return row


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


def animate_focus_pulse(widget: Widget, *, motion: str) -> None:
    """Subtle focus pulse used for high-frequency cursor movement cues."""
    if motion == "off":
        return
    duration = 0.16 if motion == "full" else 0.1
    widget.styles.opacity = 0.82
    widget.styles.animate("opacity", 1.0, duration=duration)


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
        Binding("q", "cancel", "Cancel"),
        Binding("ctrl+enter", "submit", "Create"),
    ]

    def __init__(
        self,
        default_cwd: Path,
        service: SessionService | None = None,
        default_tool: Tool = Tool.CLAUDE,
        template: SessionView | Preset | None = None,
        draft: dict[str, object] | None = None,
    ) -> None:
        super().__init__()
        self.default_cwd = default_cwd
        self.service = service
        self.default_tool = default_tool
        self.template = template
        self.draft = draft or {}
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
        self._tool_user_edited = False

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
            yield Static(modal_breadcrumb("Dashboard", "Create"), classes="modal-breadcrumb")
            with Vertical(id="create-basic"):
                yield Static("Preset", classes="form-section")
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
                yield Static("Tool", classes="form-section")
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
                yield Static("Session", classes="form-section")
                with Horizontal(classes="form-row"):
                    yield Label("Session ID", classes="field-label")
                    yield Input(
                        placeholder="api_refactor",
                        compact=True,
                        max_length=200,
                        id="create-name",
                    )
                yield Static(
                    "Use lowercase letters, digits, '-' or '_' (example: api_refactor).",
                    classes="field-help",
                )
                yield Static("", id="create-name-status", classes="field-status")
                with Horizontal(classes="form-row task-row"):
                    yield Label("Describe task", classes="field-label")
                    yield TextArea(
                        placeholder="Improve API authentication",
                        compact=True,
                        soft_wrap=True,
                        tab_behavior="focus",
                        id="create-note",
                    )
                yield Static("Workspace", classes="form-section")
                with Horizontal(classes="form-row"):
                    yield Label("Workspace path", classes="field-label")
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
                yield Static("Runtime", classes="form-section")
                with Horizontal(id="logging-row"):
                    yield Label("Logging", classes="field-label")
                    yield Switch(value=True, id="create-logging")
                    yield Static("Enabled", id="logging-state")
                yield Static("", id="logging-hint")
                yield Button(
                    f"{disclosure} Show advanced options",
                    id="create-advanced-toggle",
                    compact=True,
                )
                yield Static(
                    "Tip: advanced controls unlock startup/profile details when required.",
                    classes="inline-chip",
                )
            with (
                VerticalScroll(id="create-advanced", classes="collapsed"),
                Vertical(id="create-advanced-content"),
            ):
                with Horizontal(classes="form-row"):
                    yield Label("Tags", classes="field-label")
                    yield Input(placeholder="backend, urgent", compact=True, id="create-tags")
                yield Static(
                    "Add short tags to improve search and filtering (example: backend,api-prod).",
                    classes="field-help",
                )
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
            yield Static("", id="create-command-status", classes="field-status")
            yield Static("Validation", classes="form-section")
            yield Static("", id="create-validation", classes="validation-list")
            yield Static("", id="create-progress", classes="field-help")
            yield Static("", id="create-recovery", classes="field-help")
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
        self.set_class(self.size.width < 120 or self.size.height <= 35, "compact-form")
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
        if bool(getattr(self.app, "create_advanced_by_default", False)):
            self._set_advanced(True)
        if self.template is not None:
            self._apply_template(
                tool=self.template.tool,
                cwd=self.template.cwd,
                project=self.template.project,
                tags=self.template.tags,
                logging_enabled=self.template.logging_enabled,
            )
        self._apply_draft()
        self._schedule_validation()
        self.query_one("#create-name", Input).focus()

    def on_resize(self, event: events.Resize) -> None:
        self.set_class(event.size.width < 100, "narrow-form")
        self.set_class(event.size.width < 120 or event.size.height <= 35, "compact-form")

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
        for field_name in ("name", "cwd"):
            if field_name in self._touched:
                self._render_field_status(
                    f"#create-{field_name}-status",
                    "checking",
                    "Checking...",
                )
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
        if self.service and detected_project:
            default_tool, default_tags, default_task, default_logging = (
                self.service.project_default(detected_project)
            )
            if not self._tool_user_edited and default_tool is not None and default_tool is not tool:
                with self.prevent(Select.Changed):
                    self.query_one("#create-tool", Select).value = default_tool.value
                self._schedule_validation()
                return
            if (
                "tags" not in self._touched
                and default_tags
                and not self.query_one("#create-tags", Input).value
            ):
                with self.prevent(Input.Changed):
                    self.query_one("#create-tags", Input).value = ", ".join(default_tags)
            if default_task and not self.query_one("#create-note", TextArea).text.strip():
                with self.prevent(TextArea.Changed):
                    self.query_one("#create-note", TextArea).text = default_task
            if default_logging is not None and not self._touched.intersection({"logging"}):
                with self.prevent(Switch.Changed):
                    self.query_one("#create-logging", Switch).value = default_logging
                    self.query_one("#logging-state", Static).update(
                        "Enabled" if default_logging else "Disabled"
                    )
        suggested_tool = self._suggest_tool_for_project(detected_project)
        if suggested_tool is not None and suggested_tool is not tool:
            with self.prevent(Select.Changed):
                self.query_one("#create-tool", Select).value = suggested_tool.value
            self._schedule_validation()
            return

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
            "#create-command-status",
            "invalid" if tool_error else "valid",
            tool_error or f"Executable ready: {command[0]}",
        )
        self._render_field_status(
            "#create-tags-status",
            "invalid" if tag_error else "valid",
            tag_error or "Tags are valid",
            visible="tags" in self._touched,
        )
        errors = [issue for issue in (name_error, cwd_error, tool_error, tag_error) if issue]
        validation_lines = [
            ("valid", f"Directory exists: {display_path(resolved_cwd)}")
            if resolved_cwd is not None
            else ("invalid", cwd_error or f"Directory does not exist: {display_path(cwd)}"),
            ("valid", f"Executable ready: {command[0]}")
            if not tool_error
            else ("invalid", tool_error),
            ("valid", f"Session ID available as: {normalized}")
            if not name_error
            else ("invalid", name_error),
            ("valid", f"Project detected: {detected_project}")
            if detected_project
            else ("warning", "Project not detected"),
        ]
        validation = Text()
        markers = {"valid": "✓", "warning": "!", "invalid": "\u00d7"}
        for state, message in validation_lines:
            style = (
                "#72c78e" if state == "valid" else "#e9b44c" if state == "warning" else "#ef6b73"
            )
            validation.append(f"{markers[state]} {message}\n", style)
        self.query_one("#create-validation", Static).update(validation)
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
        fix_hints = {
            "#create-name-status": "Fix: use lowercase letters, digits, '-' or '_' only.",
            "#create-cwd-status": "Fix: enter an existing absolute path.",
            "#create-tool-status": "Fix: enable/configure the selected tool in config.toml.",
            "#create-tags-status": "Fix: use tags like backend,api-prod (letters/digits/-/_).",
        }
        rendered = f"{markers.get(state, '')} {message}".strip()
        if state == "invalid":
            hint = fix_hints.get(selector, "")
            if hint:
                rendered = f"{rendered}\n{hint}"
        target.update(rendered)
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
        recovery = self.query_one("#create-recovery", Static)
        self._render_form_progress(errors or [])
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
                        ("Hint", TOOL_CREATE_HINTS[request.tool]),
                        ("Logging", "Enabled" if request.logging_enabled else "Disabled"),
                    ]
                )
            )
            summary.display = True
            reason.display = False
            recovery.display = False
            return
        summary.display = False
        reason.display = True
        recovery.display = False
        reason.remove_class("invalid")
        if "name" not in self._touched or not self.query_one("#create-name", Input).value:
            reason.update(
                "Enter Session ID to continue. "
                "Tip: open advanced options to tune startup/runtime controls."
            )
        elif errors:
            reason.update(f"Create disabled: {errors[0]}")
            reason.add_class("invalid")
            recovery.update(
                "Recovery commands: ws doctor --actionable | ws health --actionable | ws setup"
            )
            recovery.display = True
        else:
            reason.update("Checking validation...")

    def _render_form_progress(self, errors: list[str]) -> None:
        name_ready = (
            bool(self.query_one("#create-name", Input).value.strip()) and "name" in self._touched
        )
        cwd_ready = "cwd" in self._touched and not self.query_one(
            "#create-cwd-status", Static
        ).has_class("invalid")
        tool_ready = not self.query_one("#create-tool-status", Static).has_class("invalid")
        completed = sum((name_ready, cwd_ready, tool_ready))
        status = "ready" if self._validated_request is not None and not errors else "in progress"
        self.query_one("#create-progress", Static).update(
            f"Progress: {completed}/3 required checks complete ({status})."
        )

    @on(Select.Changed, "#create-tool")
    def tool_changed(self) -> None:
        self._touched.add("tool")
        self._tool_user_edited = True
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
        self._tool_user_edited = True
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

    def _suggest_tool_for_project(self, project: str) -> Tool | None:
        if not self.service or not project:
            return None
        current_tool = Tool(str(self.query_one("#create-tool", Select).value))
        if self._tool_user_edited and current_tool is not self.default_tool:
            return None
        for session in self.service.list_sessions():
            if session.project != project:
                continue
            profile = self.service.config.tools.get(session.tool)
            if profile is not None and profile.enabled:
                return session.tool
        return None

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
            self._touched.add("logging")
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
            self.action_cancel()
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
            button.add_class("button-feedback-error")
            self.set_timer(0.28, lambda: button.remove_class("button-feedback-error"))
            self._focus_first_invalid_input()
            return
        request = self._validated_request
        if request is None:
            return
        button.add_class("button-feedback-success")
        if isinstance(self.app, WsApp):
            self.app._clear_form_draft("create")
        self.dismiss(
            CreateFormResult(
                request=request,
                start_attached=str(self.query_one("#create-startup", Select).value) == "attached",
            )
        )

    def _focus_first_invalid_input(self) -> None:
        for status_selector, field_selector in (
            ("#create-name-status", "#create-name"),
            ("#create-cwd-status", "#create-cwd"),
            ("#create-tool-status", "#create-tool"),
            ("#create-tags-status", "#create-tags"),
        ):
            if self.query_one(status_selector, Static).has_class("invalid"):
                self.query_one(field_selector).focus()
                return
        if not self.query_one("#create-name", Input).value.strip():
            self.query_one("#create-name", Input).focus()

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
            toggle.label = "v Hide advanced options" if expanded else "> Show advanced options"
        else:
            toggle.label = "▾ Hide advanced options" if expanded else "▸ Show advanced options"
        app = self.app
        if hasattr(app, "create_advanced_by_default"):
            app.create_advanced_by_default = expanded
            if hasattr(app, "_persist_interface_preferences"):
                try:
                    app._persist_interface_preferences()  # type: ignore[attr-defined]
                except (OSError, WsError):
                    app.notify(
                        "Unable to save advanced option preference.",
                        severity="warning",
                    )
        if not expanded and focused is not None and advanced in focused.ancestors:
            toggle.focus()
        elif expanded and self._advanced_focus_id:
            self.call_after_refresh(self.query_one(f"#{self._advanced_focus_id}").focus)

    def action_cancel(self) -> None:
        if self._advanced:
            self._set_advanced(False)
            self.query_one("#create-advanced-toggle", Button).focus()
            return
        self._persist_draft()
        self.dismiss(None)

    def _apply_draft(self) -> None:
        if not self.draft:
            return
        with self.prevent(Input.Changed, Select.Changed, Switch.Changed, TextArea.Changed):
            name = str(self.draft.get("name", "")).strip()
            if name:
                self.query_one("#create-name", Input).value = name
                self._touched.add("name")
            cwd = str(self.draft.get("cwd", "")).strip()
            if cwd:
                self.query_one("#create-cwd", Input).value = cwd
                self._touched.add("cwd")
            project = str(self.draft.get("project", "")).strip()
            if project:
                self.query_one("#create-project", Input).value = project
                self._project_user_edited = True
            note = str(self.draft.get("note", ""))
            if note:
                self.query_one("#create-note", TextArea).text = note
            tags = str(self.draft.get("tags", "")).strip()
            if tags:
                self.query_one("#create-tags", Input).value = tags
                self._touched.add("tags")
            tool_value = str(self.draft.get("tool", "")).strip()
            if tool_value:
                self.query_one("#create-tool", Select).value = tool_value
                self._tool_user_edited = True
            for draft_key, selector in (
                ("logging_enabled", "#create-logging"),
                ("automatic_prefix", "#create-prefix"),
            ):
                value = self.draft.get(draft_key)
                if isinstance(value, bool):
                    self.query_one(selector, Switch).value = value
            task_state = str(self.draft.get("task_state", "")).strip()
            if task_state:
                self.query_one("#create-task-state", Select).value = task_state
            startup = str(self.draft.get("startup", "")).strip()
            if startup:
                self.query_one("#create-startup", Select).value = startup
            advanced = self.draft.get("advanced")
            if isinstance(advanced, bool):
                self._set_advanced(advanced)

    def _persist_draft(self) -> None:
        if not isinstance(self.app, WsApp):
            return
        self.app._save_form_draft(
            "create",
            {
                "tool": str(self.query_one("#create-tool", Select).value),
                "name": self.query_one("#create-name", Input).value,
                "cwd": self.query_one("#create-cwd", Input).value,
                "project": self.query_one("#create-project", Input).value,
                "note": self.query_one("#create-note", TextArea).text,
                "tags": self.query_one("#create-tags", Input).value,
                "logging_enabled": self.query_one("#create-logging", Switch).value,
                "automatic_prefix": self.query_one("#create-prefix", Switch).value,
                "task_state": str(self.query_one("#create-task-state", Select).value),
                "startup": str(self.query_one("#create-startup", Select).value),
                "advanced": self._advanced,
            },
        )


class CreateFailureScreen(ModalScreen[str | None]):
    BINDINGS: ClassVar[list[BindingSpec]] = [Binding("escape", "close", "Close")]

    def __init__(self, session_name: str, error: str, *, metadata_exists: bool) -> None:
        super().__init__()
        self.session_name = session_name
        self.error = error
        self.metadata_exists = metadata_exists

    def compose(self) -> ComposeResult:
        fix = self._likely_fix()
        with Vertical(id="create-failure-dialog", classes="dialog small-dialog danger-dialog"):
            yield Label("Session Startup Failed", classes="dialog-title danger-title")
            yield Static(
                f"{self.session_name} was not started.\n\n{self.error}",
                classes="confirm-copy",
            )
            yield Static(f"Likely fix: {fix}", classes="dialog-context")
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

    def _likely_fix(self) -> str:
        message = self.error.casefold()
        if "command not found" in message:
            return "Install the tool binary or update command path in config.toml."
        if "working directory" in message:
            return "Create the directory or choose an existing path."
        if "already exists" in message:
            return "Choose a different session ID."
        if "tmux" in message:
            return "Run ws doctor and repair tmux connectivity before retrying."
        return "Open details, verify config/tool auth, then retry."


class IdentityOrganizationScreen(ModalScreen[OrganizationEditResult | None]):
    BINDINGS: ClassVar[list[BindingSpec]] = [
        Binding("escape", "cancel", "Cancel"),
        Binding("ctrl+enter", "submit", "Save"),
    ]

    def __init__(
        self,
        service: SessionService,
        session: SessionView,
        *,
        draft: dict[str, object] | None = None,
    ) -> None:
        super().__init__()
        self.service = service
        self.session = session
        self.draft = draft or {}
        self._validation_timer: Timer | None = None
        self._validation_serial = 0
        self._validated_result: OrganizationEditResult | None = None

    def compose(self) -> ComposeResult:
        with Vertical(id="identity-dialog", classes="dialog identity-dialog"):
            yield Label("Identity & Organization", classes="dialog-title")
            yield Static(
                modal_breadcrumb("Dashboard", "Manage", "Identity"), classes="modal-breadcrumb"
            )
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
        self._apply_draft()
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
            self.action_cancel()
        elif event.button.id == "identity-submit":
            self.action_submit()

    def action_submit(self) -> None:
        if self.query_one("#identity-submit", Button).disabled:
            animate_modal_shake(self)
            return
        if self._validated_result is not None:
            if isinstance(self.app, WsApp):
                self.app._clear_form_draft(
                    f"identity:{self.session.name}:{self.session.session_id}"
                )
            self.dismiss(self._validated_result)

    def action_cancel(self) -> None:
        self._persist_draft()
        self.dismiss(None)

    def _apply_draft(self) -> None:
        if not self.draft:
            return
        with self.prevent(Input.Changed):
            for key, selector in (
                ("display_name", "#identity-display-name"),
                ("name", "#identity-name"),
                ("project", "#identity-project"),
                ("tags", "#identity-tags"),
            ):
                if value := str(self.draft.get(key, "")).strip():
                    self.query_one(selector, Input).value = value

    def _persist_draft(self) -> None:
        if not isinstance(self.app, WsApp):
            return
        self.app._save_form_draft(
            f"identity:{self.session.name}:{self.session.session_id}",
            {
                "display_name": self.query_one("#identity-display-name", Input).value,
                "name": self.query_one("#identity-name", Input).value,
                "project": self.query_one("#identity-project", Input).value,
                "tags": self.query_one("#identity-tags", Input).value,
            },
        )


EditSessionScreen = IdentityOrganizationScreen


class NoteScreen(ModalScreen[str | None]):
    BINDINGS: ClassVar[list[BindingSpec]] = [
        Binding("escape", "cancel", "Cancel"),
        Binding("ctrl+enter", "submit", "Save"),
    ]

    def __init__(self, session: SessionView, *, draft: dict[str, object] | None = None) -> None:
        super().__init__()
        self.session = session
        self.draft = draft or {}

    def compose(self) -> ComposeResult:
        with Vertical(id="note-dialog", classes="dialog task-dialog"):
            yield Label("Edit Task", classes="dialog-title")
            yield Static(
                modal_breadcrumb("Dashboard", "Manage", "Edit task"), classes="modal-breadcrumb"
            )
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
        self._apply_draft()
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
            self.action_cancel()
        elif event.button.id == "note-submit":
            self.action_submit()

    def action_submit(self) -> None:
        if self.query_one("#note-submit", Button).disabled:
            animate_modal_shake(self)
            return
        if isinstance(self.app, WsApp):
            self.app._clear_form_draft(f"note:{self.session.name}:{self.session.session_id}")
        self.dismiss(self.query_one("#note-value", TextArea).text)

    def action_cancel(self) -> None:
        self._persist_draft()
        self.dismiss(None)

    def _apply_draft(self) -> None:
        note = str(self.draft.get("note", ""))
        if not note:
            return
        with self.prevent(TextArea.Changed):
            self.query_one("#note-value", TextArea).text = note

    def _persist_draft(self) -> None:
        if not isinstance(self.app, WsApp):
            return
        self.app._save_form_draft(
            f"note:{self.session.name}:{self.session.session_id}",
            {"note": self.query_one("#note-value", TextArea).text},
        )


class StatusScreen(ModalScreen[StatusEditResult | None]):
    BINDINGS: ClassVar[list[BindingSpec]] = [
        Binding("escape", "cancel", "Cancel"),
        Binding("ctrl+enter", "submit", "Save"),
    ]

    def __init__(self, session: SessionView, *, draft: dict[str, object] | None = None) -> None:
        super().__init__()
        self.session = session
        self.draft = draft or {}

    def compose(self) -> ComposeResult:
        with Vertical(id="status-dialog", classes="dialog small-dialog status-dialog"):
            yield Label("Task & Input Status", classes="dialog-title")
            yield Static(
                modal_breadcrumb("Dashboard", "Manage", "Status"), classes="modal-breadcrumb"
            )
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
        self._apply_draft()
        self.query_one("#status-task-state", Select).focus()

    @on(Button.Pressed)
    def button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "status-cancel":
            self.action_cancel()
        elif event.button.id == "status-submit":
            self.action_submit()

    def action_submit(self) -> None:
        if isinstance(self.app, WsApp):
            self.app._clear_form_draft(f"status:{self.session.name}:{self.session.session_id}")
        self.dismiss(
            StatusEditResult(
                task_state=TaskState(str(self.query_one("#status-task-state", Select).value)),
                input_state=InputState(str(self.query_one("#status-input-state", Select).value)),
            )
        )

    def action_cancel(self) -> None:
        self._persist_draft()
        self.dismiss(None)

    def _apply_draft(self) -> None:
        if not self.draft:
            return
        with self.prevent(Select.Changed):
            if task_state := str(self.draft.get("task_state", "")).strip():
                self.query_one("#status-task-state", Select).value = task_state
            if input_state := str(self.draft.get("input_state", "")).strip():
                self.query_one("#status-input-state", Select).value = input_state

    def _persist_draft(self) -> None:
        if not isinstance(self.app, WsApp):
            return
        self.app._save_form_draft(
            f"status:{self.session.name}:{self.session.session_id}",
            {
                "task_state": str(self.query_one("#status-task-state", Select).value),
                "input_state": str(self.query_one("#status-input-state", Select).value),
            },
        )


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
            "p",
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
            "restart-attach",
            "Runtime",
            "Restart and attach",
            "Restart the tool and immediately attach to the session.",
            "y",
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
        Binding("escape", "cancel", "Back"),
        Binding("q", "cancel", "Back"),
        Binding("/", "find", "Find"),
        Binding("ctrl+u", "clear_find", "Clear", show=False, priority=True),
        Binding("j", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
        Binding("e", "choose('identity')", "Identity", show=False),
        Binding("n", "choose('task')", "Task", show=False),
        Binding("s", "choose('status')", "Status", show=False),
        Binding("p", "choose('pin')", "Pin", show=False),
        Binding("asterisk", "choose('pin')", "Pin", show=False),
        Binding("g", "choose('logging')", "Logging", show=False),
        Binding("a", "choose('advanced')", "Advanced", show=False),
        Binding("c", "choose('clone')", "Clone", show=False),
        Binding("r", "choose('restart')", "Restart", show=False),
        Binding("y", "choose('restart-attach')", "Restart attach", show=False),
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
            yield Static(
                labeled_values(
                    [
                        ("Display name", condensed_session_label(self.session)),
                        ("Full ID", self.session.name),
                    ]
                ),
                classes="dialog-context",
            )
            yield Input(placeholder="Find actions", compact=True, id="manage-search")
            yield OptionList(id="manage-actions")
            yield Static("", id="manage-detail")
            yield Static(
                "MANAGE  Up/Down or j/k Navigate   Enter Select   / Find   Esc Back",
                id="manage-help",
                classes="mode-help",
            )
            with Horizontal(id="manage-close-row"):
                yield Button("Back", id="more-cancel")

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
            status = "Ready"
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
        prompt = Text(f"{marker}[{action.shortcut}] {label}")
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
        preview = self._command_preview(action.action_id)
        if preview:
            detail.append(f"\nPreview: {preview}", "#8fa0a9")
        if not action.enabled and action.disabled_reason:
            detail.append(f"\nUnavailable: {action.disabled_reason}", "#d9a441")
        elif action.destructive:
            detail.append("\nProtected confirmation required.", "#d9757d")
        self.query_one("#manage-detail", Static).update(detail)

    def _command_preview(self, action_id: str) -> str:
        name = self.session.name
        if action_id == "restart":
            return f"tmux respawn-pane -k -t {name}"
        if action_id == "restart-attach":
            return f"ws attach {name}  (after restart)"
        if action_id == "stop-command":
            return f"tmux send-keys -t {name} C-c"
        if action_id in {"stop-session", "delete"}:
            return f"tmux kill-session -t {name}"
        if action_id == "remove-metadata":
            return f"state: remove ws metadata for {name}"
        if action_id == "delete-logs":
            return f"state: delete sanitized logs for {name}"
        if action_id == "logging":
            logging_action = "disable" if self.session.logging_enabled else "enable"
            return f"state: logging {logging_action} for {name}"
        if action_id == "pin":
            return f"state: {'unpin' if self.session.pinned else 'pin'} {name}"
        return ""

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
        motion = getattr(self.app, "motion", "off")
        animate_focus_pulse(event.option_list, motion=motion)
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
                modal_breadcrumb("Dashboard", "Manage", "Delete"), classes="modal-breadcrumb"
            )
            yield Static(
                "HIGH RISK  Runtime stops immediately and metadata history is removed.",
                classes="danger-chip",
            )
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
        require_exact_name: bool = False,
        risk_level: Literal["medium", "high", "critical"] = "high",
        impact_summary: str = "",
    ) -> None:
        super().__init__()
        self.confirm_title = title
        self.session_name = session_name
        self.consequence = consequence
        self.confirm_label = confirm_label
        self.require_exact_name = require_exact_name
        self.risk_level = risk_level
        self.impact_summary = impact_summary

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-dialog", classes="dialog danger-dialog small-dialog"):
            yield Label(self.confirm_title, classes="dialog-title danger-title")
            yield Static(
                modal_breadcrumb("Dashboard", "Manage", "Confirm"), classes="modal-breadcrumb"
            )
            risk_copy = {
                "medium": "MEDIUM RISK  Verify state and impact before continuing.",
                "high": "HIGH RISK  Verify impact before continuing.",
                "critical": "CRITICAL RISK  Exact confirmation is required.",
            }[self.risk_level]
            yield Static(
                risk_copy,
                classes="danger-chip",
            )
            yield Static(self.session_name, classes="confirm-name")
            yield Static(self.consequence, classes="confirm-copy")
            if self.impact_summary:
                yield Static(f"Impact: {self.impact_summary}", classes="confirm-copy")
            if self.require_exact_name:
                yield Static("Type the full session ID to confirm.", classes="confirm-copy")
                yield Input(id="confirm-typed")
            yield Static(
                "CONFIRMATION  Tab Switch   Enter Activate   Esc Back",
                classes="mode-help",
            )
            with Horizontal(classes="dialog-actions"):
                yield Button("Cancel", id="confirm-cancel")
                yield Button(
                    self.confirm_label,
                    variant="error",
                    id="confirm-submit",
                    disabled=self.require_exact_name,
                )

    def on_mount(self) -> None:
        animate_modal_open(self)
        self.query_one("#confirm-cancel", Button).focus()

    @on(Button.Pressed)
    def button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm-submit")

    @on(Input.Changed, "#confirm-typed")
    def typed_confirmation_changed(self, event: Input.Changed) -> None:
        if not self.require_exact_name:
            return
        self.query_one("#confirm-submit", Button).disabled = event.value != self.session_name

    def action_cancel(self) -> None:
        self.dismiss(False)


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


class BulkActionScreen(ModalScreen[str | None]):
    BINDINGS: ClassVar[list[BindingSpec]] = [
        Binding("escape", "cancel", "Back"),
        Binding("j", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
        Binding("enter", "choose", "Apply", show=False),
    ]

    def __init__(self, count: int) -> None:
        super().__init__()
        self.count = count

    def compose(self) -> ComposeResult:
        with Vertical(id="bulk-dialog", classes="dialog small-dialog"):
            yield Label("Bulk Actions", classes="dialog-title")
            yield Static(f"{self.count} selected session(s)", classes="dialog-context")
            yield OptionList(
                Option("[p] Pin selected", id="bulk-pin"),
                Option("[u] Unpin selected", id="bulk-unpin"),
                Option("[g] Enable logging", id="bulk-logging-on"),
                Option("[h] Disable logging", id="bulk-logging-off"),
                Option("[x] Stop command", id="bulk-stop-command"),
                Option("[e] Export handoff summaries", id="bulk-handoff"),
                id="bulk-actions",
            )
            yield Static(
                "BULK  Up/Down or j/k Navigate   Enter Apply   Esc Back", classes="mode-help"
            )
            with Horizontal(classes="dialog-actions"):
                yield Button("Back", id="bulk-cancel")

    def on_mount(self) -> None:
        animate_modal_open(self)
        options = self.query_one("#bulk-actions", OptionList)
        options.highlighted = 0
        options.focus()

    @on(OptionList.OptionSelected, "#bulk-actions")
    def option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option.id)

    @on(Button.Pressed, "#bulk-cancel")
    def cancel_button(self) -> None:
        self.dismiss(None)

    def action_cursor_down(self) -> None:
        self.query_one("#bulk-actions", OptionList).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one("#bulk-actions", OptionList).action_cursor_up()

    def action_choose(self) -> None:
        options = self.query_one("#bulk-actions", OptionList)
        if options.highlighted is None:
            return
        self.dismiss(options.get_option_at_index(options.highlighted).id)

    def action_cancel(self) -> None:
        self.dismiss(None)


class PresetLauncherScreen(ModalScreen[str | None]):
    BINDINGS: ClassVar[list[BindingSpec]] = [
        Binding("escape", "cancel", "Close"),
        Binding("j", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
        Binding("enter", "choose", "Choose", show=False),
    ]

    def __init__(self, presets: Sequence[Preset]) -> None:
        super().__init__()
        self.presets = tuple(presets)

    def compose(self) -> ComposeResult:
        with Vertical(id="preset-launcher-dialog", classes="dialog small-dialog"):
            yield Label("Create from preset", classes="dialog-title")
            yield OptionList(id="preset-launcher-options")
            yield Static(
                "PRESETS  Up/Down or j/k Navigate   Enter Select   Esc Back",
                classes="mode-help",
            )
            with Horizontal(classes="dialog-actions"):
                yield Button("Cancel", id="preset-launcher-cancel")

    def on_mount(self) -> None:
        animate_modal_open(self)
        options = self.query_one("#preset-launcher-options", OptionList)
        entries: list[Option] = [Option("Blank session", id="preset-launcher-blank")]
        entries.extend(
            Option(
                f"{preset.name}  ·  {TOOL_LABELS[preset.tool]}  ·  {display_path(preset.cwd)}",
                id=f"preset-launcher-{preset.name}",
            )
            for preset in self.presets
        )
        options.add_options(entries)
        options.highlighted = 0
        options.focus()

    @on(Button.Pressed, "#preset-launcher-cancel")
    def close_button(self) -> None:
        self.dismiss(None)

    @on(OptionList.OptionSelected, "#preset-launcher-options")
    def option_selected(self, event: OptionList.OptionSelected) -> None:
        self._dismiss_choice(event.option.id)

    def action_cursor_down(self) -> None:
        self.query_one("#preset-launcher-options", OptionList).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one("#preset-launcher-options", OptionList).action_cursor_up()

    def action_choose(self) -> None:
        options = self.query_one("#preset-launcher-options", OptionList)
        highlighted = options.highlighted
        if highlighted is None:
            return
        self._dismiss_choice(options.get_option_at_index(highlighted).id)

    def action_cancel(self) -> None:
        self.dismiss(None)

    def _dismiss_choice(self, option_id: str | None) -> None:
        if option_id is None:
            self.dismiss(None)
            return
        if option_id == "preset-launcher-blank":
            self.dismiss("")
            return
        self.dismiss(option_id.removeprefix("preset-launcher-"))


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


class PolicySandboxScreen(ModalScreen[None]):
    BINDINGS: ClassVar[list[BindingSpec]] = [Binding("escape", "cancel", "Back")]

    def __init__(self, service: SessionService) -> None:
        super().__init__()
        self.service = service

    def compose(self) -> ComposeResult:
        approvals_enabled = self.service.config.approvals.enabled
        archive_enabled = self.service.config.archive_policy.enabled
        sla_enabled = bool(self.service.config.sla_rules)
        idle_days = self.service.config.health.idle_auto_wait_days
        with Vertical(id="policy-sandbox-dialog", classes="dialog medium-dialog"):
            yield Label("Policy Sandbox", classes="dialog-title")
            yield Label("Interactive what-if preview (read-only)", classes="dialog-subtitle")
            yield Static(
                "Hint: policy toggles are simulation-only until changed in config.toml.",
                classes="inline-chip",
            )
            yield Checkbox("Approvals enabled", approvals_enabled, id="policy-approvals")
            yield Checkbox("Archive policy enabled", archive_enabled, id="policy-archive")
            yield Checkbox("SLA checks enabled", sla_enabled, id="policy-sla")
            yield Label("Idle auto-wait days", classes="field-label")
            yield Input(value=str(idle_days), id="policy-idle-days")
            with VerticalScroll(id="policy-output-scroll"):
                yield Static("", id="policy-output")
            yield Static(
                "POLICY  Tab Next   Shift+Tab Previous   Enter Simulate   Esc Back",
                classes="mode-help",
            )
            with Horizontal(classes="dialog-actions"):
                yield Button("Back", id="policy-cancel")
                yield Button("Simulate", variant="primary", id="policy-run")

    def on_mount(self) -> None:
        self.query_one("#policy-run", Button).focus()
        self._render_output()

    @on(Button.Pressed)
    def button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "policy-cancel":
            self.dismiss(None)
        elif event.button.id == "policy-run":
            self._render_output()

    @on(Input.Submitted)
    def input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "policy-idle-days":
            self._render_output()

    def _render_output(self) -> None:
        approvals_enabled = self.query_one("#policy-approvals", Checkbox).value
        archive_enabled = self.query_one("#policy-archive", Checkbox).value
        sla_enabled = self.query_one("#policy-sla", Checkbox).value
        idle_days_raw = self.query_one("#policy-idle-days", Input).value.strip()
        try:
            idle_days = max(0, int(idle_days_raw or "0"))
            idle_note = ""
        except ValueError:
            idle_days = self.service.config.health.idle_auto_wait_days
            idle_note = (
                f"\nIdle auto-wait days: invalid value {idle_days_raw!r}; using current config."
            )
        baseline = self.service.policy_simulation()
        archive_candidates = baseline.get("archive_candidates", [])
        archive_count = len(archive_candidates) if archive_enabled else 0
        lines = [
            "Policy simulation",
            "",
            f"Sessions total: {baseline.get('sessions_total', 0)}",
            f"Approvals: {'enabled' if approvals_enabled else 'disabled'}",
            f"Archive candidates: {archive_count}",
            f"SLA checks: {'enabled' if sla_enabled else 'disabled'}",
            f"Idle auto-wait days: {idle_days}",
            "",
            "Note: sandbox preview only; no policy changes are applied.",
        ]
        if idle_note:
            lines.append(idle_note.strip())
        self.query_one("#policy-output", Static).update("\n".join(lines))

    def action_cancel(self) -> None:
        self.dismiss(None)


def diagnostic_name(check: HealthCheck) -> str:
    labels = {
        "tmux": "tmux",
        "tool:claude": "Claude Code",
        "tool:copilot": "Copilot",
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
        "missing-cwd": "Missing working directories",
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
    ]

    def __init__(self, service: SessionService) -> None:
        super().__init__()
        self.service = service
        self.checks: list[HealthCheck] = service.cached_health_alerts()
        self._focused_related_sessions: tuple[str, ...] = ()
        self.running = False

    def compose(self) -> ComposeResult:
        with Vertical(id="diagnostics-dialog", classes="dialog diagnostics-dialog"):
            yield Label("System Health", classes="dialog-title")
            yield Static("", id="health-alerts-summary")
            yield Static("STATUS  CHECK                         RESULT", id="health-alerts-header")
            yield LoadingIndicator(id="health-alerts-loading")
            with VerticalScroll(id="health-alerts-list"):
                yield Static("", id="health-alerts-content")
            yield Static("", id="health-alerts-selected")
            yield Static(
                "HEALTH  r Refresh   v View sessions   c Copy details   Esc Back",
                classes="mode-help",
            )
            with Horizontal(classes="dialog-actions diagnostics-actions"):
                yield Button("Refresh Now", id="health-alerts-run")
                yield Button("View Sessions", id="health-alerts-sessions")
                yield Button("Copy Details", id="health-alerts-copy")
                yield Button("Dismiss", id="health-alerts-dismiss")
                yield Button("Back", variant="primary", id="health-alerts-close")

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
            label = (
                "PASS"
                if check.status is HealthStatus.PASS
                else "WARN"
                if check.status is HealthStatus.WARN
                else "FAIL"
                if check.status is HealthStatus.FAIL
                else "INFO"
            )
            content.append(f"{label:<7}", styles[check.status])
            content.append(f"{diagnostic_name(check):<29}", "bold")
            content.append(f"{diagnostic_detail(check, expanded=True)}\n")
            if check.status in {HealthStatus.WARN, HealthStatus.FAIL} and check.corrective_action:
                content.append(f"     Action: {check.corrective_action}\n", "dim")
        self.query_one("#health-alerts-content", Static).update(content)
        focused = next(
            (
                check
                for check in self.checks
                if check.status in {HealthStatus.WARN, HealthStatus.FAIL}
            ),
            None,
        )
        selected = self.query_one("#health-alerts-selected", Static)
        if focused is None:
            self._focused_related_sessions = ()
            selected.update("Selected warning detail:\nNo active warnings.")
        else:
            related = self.service.related_sessions_for_health_check(focused)
            self._focused_related_sessions = tuple(related)
            related_text = ", ".join(related[:5]) if related else "none linked"
            selected.update(
                "Selected warning detail:\n"
                f"{diagnostic_name(focused)}:\n{diagnostic_detail(focused, expanded=True)}\n\n"
                f"Related sessions:\n{related_text}\n\n"
                f"Recommended action:\n{focused.corrective_action or 'No action required.'}"
            )

    def action_run(self) -> None:
        if self.running:
            return
        self.running = True
        self.query_one("#health-alerts-loading", LoadingIndicator).display = True
        for selector in ("#health-alerts-run",):
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
        for selector in ("#health-alerts-run",):
            self.query_one(selector, Button).disabled = False
        self._render_report()

    @on(Button.Pressed)
    def button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "health-alerts-close":
            self.dismiss(None)
        elif event.button.id == "health-alerts-run":
            self.action_run()
        elif event.button.id == "health-alerts-sessions":
            self.action_view_sessions()
        elif event.button.id == "health-alerts-copy":
            self.action_copy()
        elif event.button.id == "health-alerts-dismiss":
            self.action_dismiss_until_refresh()

    def action_close(self) -> None:
        self.dismiss(None)

    def action_copy(self) -> None:
        lines = [
            "SYSTEM HEALTH",
            str(self.query_one("#health-alerts-summary", Static).content),
            "",
        ]
        lines.append("STATUS  CHECK                         RESULT")
        for check in self.checks:
            label = (
                "PASS"
                if check.status is HealthStatus.PASS
                else "WARN"
                if check.status is HealthStatus.WARN
                else "FAIL"
                if check.status is HealthStatus.FAIL
                else "INFO"
            )
            lines.append(f"{label:<7} {diagnostic_name(check):<29} {check.detail}")
            if check.corrective_action:
                lines.append(f"        Action: {check.corrective_action}")
        details = "\n".join(lines)
        copied = self.app.copy_to_clipboard(details)
        if copied is False:
            self.notify(
                "Copy failed. Enable terminal clipboard integration or OSC 52 support.",
                severity="warning",
            )
            return
        channel = getattr(self.app, "_last_copy_channel", "native")
        message = (
            "Health details copied"
            if channel in {"native", "both"}
            else "Health details copied (OSC 52)"
        )
        self.notify(message)

    def action_view_sessions(self) -> None:
        encoded = ",".join(self._focused_related_sessions[:8])
        self.dismiss(f"view-sessions:{encoded}" if encoded else "view-sessions")

    def action_dismiss_until_refresh(self) -> None:
        self.dismiss("dismiss")


class DependencyGraphScreen(ModalScreen[None]):
    BINDINGS: ClassVar[list[BindingSpec]] = [
        Binding("escape", "close", "Back"),
        Binding("q", "close", "Back"),
        Binding("r", "refresh", "Refresh"),
    ]

    def __init__(self, service: SessionService, *, focus_session: str = "") -> None:
        super().__init__()
        self.service = service
        self.focus_session = focus_session
        self._dependency_nodes: list[str] = []
        self._index_to_node: dict[str, str] = {}
        self._critical_path: list[str] = []

    def compose(self) -> ComposeResult:
        with Vertical(id="dependency-dialog", classes="dialog diagnostics-dialog"):
            yield Label("Session Dependency Graph", classes="dialog-title")
            yield Static("", id="dependency-summary")
            with Horizontal(id="dependency-body"):
                with Vertical(id="dependency-list-pane"):
                    yield Static("NODE                          BLOCKED BY", id="dependency-header")
                    yield OptionList(id="dependency-nodes")
                with Vertical(id="dependency-detail-pane"):
                    yield Static("", id="dependency-details")
            yield Static(
                "DEPENDENCIES  r Refresh   Enter Inspect   Esc Back",
                classes="mode-help",
            )
            with Horizontal(classes="dialog-actions diagnostics-actions"):
                yield Button("Refresh", id="dependency-refresh")
                yield Button("Back", variant="primary", id="dependency-close")

    def on_mount(self) -> None:
        animate_modal_open(self)
        self._reload()
        options = self.query_one("#dependency-nodes", OptionList)
        options.focus()

    def _reload(self) -> None:
        graph = self.service.dependency_graph()
        reverse: dict[str, list[str]] = {}
        all_nodes: set[str] = set(graph)
        for node, blocked_by in graph.items():
            for upstream in blocked_by:
                all_nodes.add(upstream)
                reverse.setdefault(upstream, []).append(node)
        self._critical_path = self.service.dependency_critical_path()
        critical = set(self._critical_path)
        self._dependency_nodes = sorted(all_nodes)
        options = self.query_one("#dependency-nodes", OptionList)
        options.clear_options()
        self._index_to_node.clear()
        for index, node in enumerate(self._dependency_nodes):
            blocked_by = graph.get(node, [])
            marker = "*" if node in critical else "-"
            truncated_node = truncate(
                node,
                30,
                ascii_only=getattr(self.app, "ascii_only", False),
            )
            label = Text()
            label.append(f"{marker} {truncated_node:<30}")
            label.append(f"{len(blocked_by):>4}", "dim")
            option_id = f"dep-node-{index}"
            self._index_to_node[option_id] = node
            options.add_option(Option(label, id=option_id))
        summary = (
            f"Nodes: {len(self._dependency_nodes)}   "
            f"Edges: {sum(len(values) for values in graph.values())}   "
            f"Critical path: {' -> '.join(self._critical_path) if self._critical_path else 'none'}"
        )
        self.query_one("#dependency-summary", Static).update(summary)
        target_id = ""
        if self.focus_session and self.focus_session in self._dependency_nodes:
            target_id = f"dep-node-{self._dependency_nodes.index(self.focus_session)}"
        elif self._dependency_nodes:
            target_id = "dep-node-0"
        if target_id:
            options.highlighted = options.get_option_index(target_id)
            self._render_details_for(target_id)
        else:
            self.query_one("#dependency-details", Static).update("No dependencies recorded.")

    def _render_details_for(self, option_id: str) -> None:
        node = self._index_to_node.get(option_id)
        if not node:
            return
        graph = self.service.dependency_graph()
        blocked_by = graph.get(node, [])
        unblocks = sorted(item for item, deps in graph.items() if node in deps)
        lines = [f"Session: {node}", "", "Blocked by:"]
        if blocked_by:
            lines.extend(f"- {item}" for item in blocked_by)
        else:
            lines.append("- none")
        lines.extend(("", "Unblocks:"))
        if unblocks:
            lines.extend(f"- {item}" for item in unblocks)
        else:
            lines.append("- none")
        lines.extend(
            (
                "",
                "Critical path: "
                + (" -> ".join(self._critical_path) if self._critical_path else "none"),
            )
        )
        self.query_one("#dependency-details", Static).update("\n".join(lines))

    @on(OptionList.OptionHighlighted, "#dependency-nodes")
    def dependency_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        if event.option_id:
            self._render_details_for(event.option_id)

    @on(Button.Pressed)
    def button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "dependency-close":
            self.dismiss(None)
        elif event.button.id == "dependency-refresh":
            self._reload()

    def action_refresh(self) -> None:
        self._reload()

    def action_close(self) -> None:
        self.dismiss(None)


class FederationControlScreen(ModalScreen[str | None]):
    BINDINGS: ClassVar[list[BindingSpec]] = [
        Binding("escape", "close", "Back"),
        Binding("q", "close", "Back"),
        Binding("r", "refresh", "Refresh"),
        Binding("a", "toggle_scope", "Scope"),
        Binding("5", "save_dashboard", "Save view"),
        Binding("6", "apply_dashboard", "Apply view"),
        Binding("7", "delete_dashboard", "Delete view"),
        Binding("8", "fleet_diff", "Fleet diff"),
        Binding("9", "toggle_safe_mode", "Safe mode"),
        Binding("enter", "focus_local", "Focus local"),
        Binding("w", "focus_warning", "Focus warning"),
        Binding("m", "manage_local", "Manage local"),
        Binding("l", "logs_local", "Logs local"),
        Binding("1", "run_list", "List"),
        Binding("2", "run_health", "Health"),
        Binding("3", "run_report", "Report"),
        Binding("4", "run_resume", "Resume"),
    ]

    ACTIONS: ClassVar[dict[str, str]] = {
        "1": "list",
        "2": "health",
        "3": "report",
        "4": "resume",
    }

    def __init__(self, service: SessionService) -> None:
        super().__init__()
        self.service = service
        self._rows: list[dict[str, object]] = []
        self._filtered_hosts: list[str] = []
        self._rows_by_host: dict[str, dict[str, object]] = {}
        self._health_snapshots: dict[str, dict[str, int]] = {}
        self._option_to_host: dict[str, str] = {}
        self._scope_all_hosts = False
        self._safe_mode = False
        self._dashboard_hosts_override: list[str] | None = None
        self._loaded_hosts_total = 0
        self._loaded_session_total = 0
        self._refresh_cooldown_until = 0.0
        self._loading = False

    def compose(self) -> ComposeResult:
        with Vertical(id="federation-dialog", classes="dialog diagnostics-dialog"):
            yield Label("Federation Control Center", classes="dialog-title")
            yield Static("", id="federation-summary")
            yield Static(
                "Hint: host actions are scoped to selected host unless scope=all is enabled.",
                id="federation-hint",
                classes="inline-chip",
            )
            with Horizontal(id="federation-filter-row", classes="form-row"):
                yield Label("Host filter", classes="field-label")
                yield Input(placeholder="Filter hosts...", id="federation-filter")
            with Horizontal(id="federation-dashboard-row", classes="form-row"):
                yield Label("Pinned view", classes="field-label")
                yield Input(placeholder="dashboard name", id="federation-dashboard-name")
            with Horizontal(id="federation-body"):
                with Vertical(id="federation-hosts-pane"):
                    yield Static(
                        "HOST                         STATUS  SESSIONS", id="federation-header"
                    )
                    yield OptionList(id="federation-hosts")
                with Vertical(id="federation-detail-pane"):
                    with VerticalScroll(id="federation-host-cards-scroll"):
                        yield Static("", id="federation-host-cards")
                    yield Static("", id="federation-details")
                    yield Static("", id="federation-sla-panel")
                    yield Static("", id="federation-diff-panel")
                    yield Static("", id="federation-dashboard-list")
                    yield Static("", id="federation-action-status")
            yield LoadingIndicator(id="federation-loading")
            yield Static(
                "FEDERATION  Enter Focus local   w Focus warning   m Manage local   l Logs local"
                "   5 Save view   6 Apply view   7 Delete view   8 Fleet diff   a Scope (host/all)"
                "   1 List   2 Health   3 Report   4 Resume   r Refresh   Esc Back",
                classes="mode-help",
            )
            with Horizontal(classes="dialog-actions diagnostics-actions"):
                yield Button("Refresh", id="federation-refresh")
                yield Button("List", id="federation-list")
                yield Button("Health", id="federation-health")
                yield Button("Report", id="federation-report")
                yield Button("Resume", id="federation-resume")
                yield Button("Save view", id="federation-save-dashboard")
                yield Button("Apply view", id="federation-apply-dashboard")
                yield Button("Delete view", id="federation-delete-dashboard")
                yield Button("Fleet diff", id="federation-fleet-diff")
                yield Button("Safe mode", id="federation-safe-mode")
                yield Button("Focus warning", id="federation-focus-warning")
                yield Button("Manage local", id="federation-manage-local")
                yield Button("Logs local", id="federation-logs-local")
                yield Button("Back", variant="primary", id="federation-close")

    def on_mount(self) -> None:
        animate_modal_open(self)
        self._safe_mode = bool(
            getattr(self.app, "_auto_safe_mode", False)
            or self.service.config.interface.performance_profile == "ssh-safe"
        )
        self.query_one("#federation-loading", LoadingIndicator).display = False
        self.query_one("#federation-hosts", OptionList).focus()
        self._render_dashboard_list()
        self._render_fleet_diff_panel()
        self.action_refresh()

    def _set_loading(self, loading: bool) -> None:
        self._loading = loading
        self.query_one("#federation-loading", LoadingIndicator).display = loading
        for selector in (
            "#federation-refresh",
            "#federation-list",
            "#federation-health",
            "#federation-report",
            "#federation-resume",
        ):
            self.query_one(selector, Button).disabled = loading

    @work(thread=True, exclusive=True, group="federation-control-load")
    def _load_hosts(self) -> None:
        try:
            rows = self.service.federated_sessions(self._dashboard_hosts_override)
        except (WsError, OSError) as error:
            self.app.call_from_thread(self._finish_load, [], str(error))
            return
        self.app.call_from_thread(self._finish_load, rows, "")

    def _host_scan_budget(self) -> int:
        return 40 if self._safe_mode else 200

    def _session_scan_budget(self) -> int:
        return 30 if self._safe_mode else 120

    def _refresh_cooldown_seconds(self) -> float:
        return 8.0 if self._safe_mode else 3.0

    def _finish_load(self, rows: list[dict[str, object]], error: str) -> None:
        if not self.is_mounted:
            return
        self._set_loading(False)
        if error:
            self.notify(error, title="Federation refresh failed", severity="error")
            self._rows = []
            self._rows_by_host = {}
            self._filtered_hosts = []
            self.query_one("#federation-summary", Static).update("No hosts loaded.")
            self.query_one("#federation-host-cards", Static).update("No host cards to display.")
            self.query_one("#federation-details", Static).update("Refresh failed.")
            return
        self._loaded_hosts_total = len(rows)
        self._loaded_session_total = sum(
            len(row.get("sessions", [])) if isinstance(row.get("sessions"), list) else 0
            for row in rows
        )
        self._rows = rows[: self._host_scan_budget()]
        self._rows_by_host = {
            str(row.get("host", "")).strip(): row
            for row in self._rows
            if str(row.get("host", "")).strip()
        }
        self._render_hosts()

    def _selected_host(self) -> str:
        options = self.query_one("#federation-hosts", OptionList)
        highlighted = options.highlighted
        if highlighted is None or highlighted < 0 or highlighted >= options.option_count:
            return ""
        option = options.get_option_at_index(highlighted)
        return self._option_to_host.get(option.id or "", "")

    def _session_name(self, item: object) -> str:
        if isinstance(item, dict):
            for key in ("display_name", "name", "session_id"):
                value = item.get(key)
                if value:
                    return str(value)
        return str(item)

    def _session_counts(self, sessions: list[object]) -> dict[str, int]:
        attached = detached = stopped = blocked = needs_input = 0
        for item in sessions:
            if not isinstance(item, dict):
                continue
            runtime = str(item.get("runtime", "")).lower()
            task_state = str(item.get("task_state", "")).lower()
            input_state = str(item.get("input_state", "")).lower()
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
        return {
            "attached": attached,
            "detached": detached,
            "stopped": stopped,
            "blocked": blocked,
            "needs_input": needs_input,
        }

    def _is_warning_session(self, item: object) -> bool:
        if not isinstance(item, dict):
            return False
        runtime = str(item.get("runtime", "")).lower()
        task_state = str(item.get("task_state", "")).lower()
        input_state = str(item.get("input_state", "")).lower()
        return (
            runtime in {RuntimeState.STOPPED.value, RuntimeState.FAILED.value}
            or task_state == TaskState.BLOCKED.value
            or input_state == InputState.REQUIRED.value
        )

    def _local_matches_for_host(self, host: str, *, warnings_only: bool = False) -> list[str]:
        row = self._rows_by_host.get(host, {})
        sessions = row.get("sessions", [])
        if not isinstance(sessions, list):
            return []
        local_names = {session.name for session in self.service.list_sessions()}
        matches: list[str] = []
        for item in sessions:
            if warnings_only and not self._is_warning_session(item):
                continue
            candidate = self._session_name(item)
            if candidate in local_names:
                matches.append(candidate)
        return matches

    def _focus_local(self, *, warnings_only: bool = False, post_action: str = "") -> None:
        host = self._selected_host()
        if not host:
            self.notify("No federation host selected.", severity="warning")
            return
        matches = self._local_matches_for_host(host, warnings_only=warnings_only)
        if not matches and warnings_only:
            self.notify("No matching warning sessions found on this host.", severity="warning")
            return
        if not matches:
            matches = self._local_matches_for_host(host, warnings_only=False)
        if not matches:
            self.notify("No matching local session found for this host.")
            return
        action_suffix = f":{post_action}" if post_action else ""
        self.dismiss(f"focus-session:{matches[0]}{action_suffix}")

    def _host_health_label(self, row: dict[str, object], host: str) -> tuple[str, str]:
        error = str(row.get("error", "")).strip()
        sessions = row.get("sessions", [])
        if error:
            return ("down", "DOWN")
        if not isinstance(sessions, list):
            return ("unknown", "UNKNOWN")
        counts = self._session_counts(sessions)
        if counts["stopped"] or counts["blocked"]:
            return ("degraded", "DEGRADED")
        if counts["needs_input"]:
            return ("watch", "WATCH")
        snapshot = self._health_snapshots.get(host)
        if snapshot and snapshot.get("fail", 0):
            return ("degraded", "DEGRADED")
        if snapshot and snapshot.get("warn", 0):
            return ("watch", "WATCH")
        return ("healthy", "HEALTHY")

    def _health_snapshot_label(self, host: str) -> str:
        snapshot = self._health_snapshots.get(host)
        if not snapshot:
            return "not run"
        return (
            f"fail={snapshot.get('fail', 0)} "
            f"warn={snapshot.get('warn', 0)} "
            f"pass={snapshot.get('pass', 0)}"
        )

    def _render_host_cards(self, hosts: list[str]) -> None:
        cards_widget = self.query_one("#federation-host-cards", Static)
        if not hosts:
            cards_widget.update("No hosts match the current filter.")
            return
        cards: list[str] = []
        for host in hosts:
            row = self._rows_by_host[host]
            sessions = row.get("sessions", [])
            safe_sessions = (
                sessions[: self._session_scan_budget()] if isinstance(sessions, list) else []
            )
            counts = self._session_counts(safe_sessions)
            _, health = self._host_health_label(row, host)
            host_label = truncate(
                host,
                34,
                ascii_only=getattr(self.app, "ascii_only", False),
            )
            cards.extend(
                (
                    f"[{host_label}] {health}",
                    (
                        f" sessions={len(safe_sessions)} attached={counts['attached']} "
                        f"detached={counts['detached']} stopped={counts['stopped']}"
                    ),
                    (
                        f" blocked={counts['blocked']} needs-input={counts['needs_input']} "
                        f"last-health={self._health_snapshot_label(host)}"
                    ),
                )
            )
            error = str(row.get("error", "")).strip()
            if error:
                cards.append(f" error={error}")
            cards.append("")
        cards_widget.update("\n".join(cards).strip())

    def _parse_remote_timestamp(self, value: object) -> datetime | None:
        if not value:
            return None
        text = str(value).strip()
        if not text:
            return None
        normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
        with contextlib.suppress(ValueError):
            stamp = datetime.fromisoformat(normalized)
            if stamp.tzinfo is None:
                return stamp.replace(tzinfo=UTC)
            return stamp
        return None

    def _local_ownership_hint(
        self,
        session_name: str,
        local_sessions: dict[str, SessionView],
    ) -> str:
        local = local_sessions.get(session_name)
        if local is None:
            return "not present locally"
        return "managed locally" if local.owned else "present locally (unmanaged)"

    def _render_sla_panel(self, hosts: list[str]) -> None:
        panel = self.query_one("#federation-sla-panel", Static)
        rules = self.service.config.sla_rules
        if not rules:
            panel.update("SLA panel: no sla_rules configured.")
            return
        local_sessions = {
            item.name: item for item in self.service.list_sessions(include_unmanaged=True)
        }
        now = datetime.now(UTC)
        violations: list[str] = []
        for host in hosts:
            row = self._rows_by_host.get(host, {})
            sessions = row.get("sessions", [])
            if not isinstance(sessions, list):
                continue
            for item in sessions[: self._session_scan_budget()]:
                if not isinstance(item, dict):
                    continue
                anchor = self._parse_remote_timestamp(
                    item.get("last_active_at")
                ) or self._parse_remote_timestamp(item.get("created_at"))
                if anchor is None:
                    continue
                age_hours = max(0.0, (now - anchor).total_seconds() / 3600.0)
                runtime = str(item.get("runtime", "")).lower()
                task_state = str(item.get("task_state", "")).lower()
                input_state = str(item.get("input_state", "")).lower()
                for rule in rules:
                    reason = ""
                    threshold = 0.0
                    if runtime == RuntimeState.DETACHED.value and age_hours >= rule.detached_hours:
                        reason, threshold = "detached", rule.detached_hours
                    elif task_state == TaskState.BLOCKED.value and age_hours >= rule.blocked_hours:
                        reason, threshold = "blocked", rule.blocked_hours
                    elif (
                        input_state == InputState.REQUIRED.value
                        and age_hours >= rule.input_required_hours
                    ):
                        reason, threshold = "needs-input", rule.input_required_hours
                    if not reason:
                        continue
                    session_name = self._session_name(item)
                    ownership = self._local_ownership_hint(session_name, local_sessions)
                    violations.append(
                        f"- {host}/{session_name} {reason} {age_hours:.1f}h "
                        f"(>= {threshold:.1f}h, {rule.name}, {ownership})"
                    )
        if not violations:
            panel.update("SLA breaches: none across filtered hosts.")
            return
        capped = violations[:8]
        suffix = (
            f"\n... and {len(violations) - len(capped)} more"
            if len(violations) > len(capped)
            else ""
        )
        panel.update(f"SLA breaches ({len(violations)}):\n" + "\n".join(capped) + suffix)

    def _render_dashboard_list(self) -> None:
        widget = self.query_one("#federation-dashboard-list", Static)
        rows = self.service.list_federation_dashboards()
        if not rows:
            widget.update("Pinned views: none")
            return
        names = [str(row.get("name", "")) for row in rows if str(row.get("name", "")).strip()]
        widget.update(
            "Pinned views: "
            + ", ".join(names[:8])
            + (", ..." if len(names) > 8 else "")
            + f"   (safe mode {'on' if self._safe_mode else 'off'})"
        )

    def _render_fleet_diff_panel(self, payload: dict[str, object] | None = None) -> None:
        panel = self.query_one("#federation-diff-panel", Static)
        if payload is None:
            panel.update("Fleet diff: press 8 to compare current hosts with latest saved snapshot.")
            return
        baseline = (
            str(payload.get("baseline", "")).strip()
            or str(payload.get("left", {}).get("name", "")).strip()
        )
        drifts = payload.get("drifts", [])
        spread = payload.get("host_spread", [])
        if not isinstance(drifts, list) or not isinstance(spread, list):
            panel.update("Fleet diff: unavailable.")
            return
        if not drifts and not spread:
            panel.update(f"Fleet diff vs {baseline or 'baseline'}: no drift detected.")
            return
        lines = [
            f"Fleet diff vs {baseline or 'baseline'}: {len(drifts)} drift item(s), "
            f"{len(spread)} spread signal(s)."
        ]
        for row in drifts[:4]:
            if not isinstance(row, dict):
                continue
            lines.append(
                f"- {row.get('host', '?')} {row.get('field', '?')}: "
                f"{row.get('left', '?')} -> {row.get('right', '?')}"
            )
        for item in spread[:2]:
            if not isinstance(item, dict):
                continue
            lines.append(f"- spread {item.get('field', '?')}: {item.get('spread', '?')}")
        panel.update("\n".join(lines))

    def _current_dashboard_hosts(self) -> list[str]:
        if self._filtered_hosts:
            return list(self._filtered_hosts)
        return sorted(self._rows_by_host)

    def _render_hosts(self) -> None:
        host_filter = self.query_one("#federation-filter", Input).value.strip().lower()
        hosts = sorted(self._rows_by_host)
        if host_filter:
            hosts = [host for host in hosts if host_filter in host.lower()]
        self._filtered_hosts = hosts
        options = self.query_one("#federation-hosts", OptionList)
        options.clear_options()
        self._option_to_host.clear()
        total_sessions = 0
        reachable = 0
        for index, host in enumerate(hosts):
            row = self._rows_by_host[host]
            error = str(row.get("error", "")).strip()
            sessions = row.get("sessions", [])
            session_count = (
                min(len(sessions), self._session_scan_budget()) if isinstance(sessions, list) else 0
            )
            total_sessions += session_count
            _, status = self._host_health_label(row, host)
            reachable += int(not error)
            label = Text()
            label.append(
                f"{truncate(host, 27, ascii_only=getattr(self.app, 'ascii_only', False)):<27} "
            )
            status_style = {
                "HEALTHY": "green",
                "WATCH": "yellow",
                "DEGRADED": "bold red",
                "DOWN": "bold red",
            }.get(status, "dim")
            label.append(f"{status:<8}", status_style)
            label.append(f"{session_count:>8}", "dim")
            option_id = f"federation-host-{index}"
            self._option_to_host[option_id] = host
            options.add_option(Option(label, id=option_id))
        scope = "all filtered hosts" if self._scope_all_hosts else "selected host"
        mode = "safe" if self._safe_mode else "normal"
        extra = ""
        if self._loaded_hosts_total > len(self._rows_by_host):
            extra = (
                f"   Scan budget: {len(self._rows_by_host)}/{self._loaded_hosts_total} hosts, "
                f"{self._session_scan_budget()} sessions/host ({mode})"
            )
        summary_text = (
            f"Hosts: {len(hosts)}/{len(self._rows_by_host)}   Reachable: {reachable}   "
            f"Sessions: {total_sessions}   Action scope: {scope}   Mode: {mode}{extra}"
        )
        self.query_one("#federation-summary", Static).update(summary_text)
        status_widget = self.query_one("#federation-action-status", Static)
        if not str(status_widget.content).strip():
            status_widget.update(
                "Actions are read-only except resume. Use scope=all for bulk operations."
            )
        self._render_host_cards(hosts)
        self._render_sla_panel(hosts)
        if options.option_count:
            options.highlighted = 0
            first = options.get_option_at_index(0)
            if first.id:
                self._render_details(first.id)
        else:
            self.query_one("#federation-details", Static).update(
                "No hosts match the current filter."
            )

    def _render_details(self, option_id: str) -> None:
        host = self._option_to_host.get(option_id)
        if not host:
            return
        row = self._rows_by_host.get(host, {})
        error = str(row.get("error", "")).strip()
        sessions = row.get("sessions", [])
        session_names = (
            [self._session_name(item) for item in sessions[: min(8, self._session_scan_budget())]]
            if isinstance(sessions, list)
            else []
        )
        _, health = self._host_health_label(row, host)
        counts = self._session_counts(sessions if isinstance(sessions, list) else [])
        lines = [
            f"Host: {host}",
            f"Status: {health}",
            f"Health checks: {self._health_snapshot_label(host)}",
            f"Sessions reported: {len(sessions) if isinstance(sessions, list) else 0}",
            (
                f"Attached: {counts['attached']}  Detached: {counts['detached']}  "
                f"Stopped: {counts['stopped']}  Blocked: {counts['blocked']}  "
                f"Needs input: {counts['needs_input']}"
            ),
            "",
            "Recent sessions:",
        ]
        if session_names:
            lines.extend(f"- {name}" for name in session_names)
        else:
            lines.append("- none")
        if error:
            lines.extend(("", f"Error: {error}"))
        self.query_one("#federation-details", Static).update("\n".join(lines))

    @on(Input.Changed, "#federation-filter")
    def filter_changed(self, _event: Input.Changed) -> None:
        self._render_hosts()

    @on(OptionList.OptionHighlighted, "#federation-hosts")
    def host_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        if event.option_id:
            self._render_details(event.option_id)

    @on(OptionList.OptionSelected, "#federation-hosts")
    def host_selected(self, _event: OptionList.OptionSelected) -> None:
        self.action_focus_local()

    @work(thread=True, exclusive=True, group="federation-control-action")
    def _run_remote_action(self, action: str, hosts: list[str]) -> None:
        try:
            rows = self.service.federated_action(action, hosts=hosts)
        except (WsError, OSError) as error:
            self.app.call_from_thread(self._finish_remote_action, action, hosts, [], str(error))
            return
        self.app.call_from_thread(self._finish_remote_action, action, hosts, rows, "")

    def _finish_remote_action(
        self,
        action: str,
        hosts: list[str],
        rows: list[dict[str, object]],
        error: str,
    ) -> None:
        if not self.is_mounted:
            return
        self._set_loading(False)
        if error:
            self.query_one("#federation-action-status", Static).update(
                f"{action} failed for {len(hosts)} host(s): {error}"
            )
            self.notify(error, title="Federation action failed", severity="error")
            return
        ok = sum(1 for row in rows if bool(row.get("ok")))
        failures = len(rows) - ok
        if action == "health":
            for row in rows:
                host = str(row.get("host", "")).strip()
                if not host:
                    continue
                stdout = str(row.get("stdout", "")).strip()
                if not stdout:
                    continue
                try:
                    payload = json.loads(stdout)
                except ValueError:
                    continue
                checks = payload.get("checks") if isinstance(payload, dict) else None
                if not isinstance(checks, list):
                    continue
                status_counts = {"pass": 0, "warn": 0, "fail": 0, "info": 0}
                for check in checks:
                    if not isinstance(check, dict):
                        continue
                    status = str(check.get("status", "")).lower()
                    if status in status_counts:
                        status_counts[status] += 1
                self._health_snapshots[host] = status_counts
            self._render_hosts()
        message = f"{action} completed on {len(hosts)} host(s): {ok} ok, {failures} failed."
        if failures and ok:
            failed_rows = [row for row in rows if not bool(row.get("ok"))]
            detail = ", ".join(
                f"{row.get('host')}: {row.get('failure_summary') or row.get('error') or 'failed'}"
                for row in failed_rows[:2]
            )
            message = f"{message} Partial success. {detail}"
        self.query_one("#federation-action-status", Static).update(message)
        if failures:
            self.notify(f"{action} had {failures} failure(s).", severity="warning")
        else:
            self.notify(f"{action} completed on {len(hosts)} host(s).")
        if action != "health":
            self.action_refresh()

    def _dispatch_action(self, action: str) -> None:
        if self._loading:
            return
        hosts = (
            list(self._filtered_hosts)
            if self._scope_all_hosts
            else ([selected] if (selected := self._selected_host()) else [])
        )
        if not hosts:
            self.notify("No federation host selected.", severity="warning")
            return
        self._set_loading(True)
        self._run_remote_action(action, hosts)

    @on(Button.Pressed)
    def button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "federation-close":
            self.dismiss(None)
        elif event.button.id == "federation-refresh":
            self.action_refresh()
        elif event.button.id == "federation-list":
            self.action_run_list()
        elif event.button.id == "federation-health":
            self.action_run_health()
        elif event.button.id == "federation-report":
            self.action_run_report()
        elif event.button.id == "federation-resume":
            self.action_run_resume()
        elif event.button.id == "federation-save-dashboard":
            self.action_save_dashboard()
        elif event.button.id == "federation-apply-dashboard":
            self.action_apply_dashboard()
        elif event.button.id == "federation-delete-dashboard":
            self.action_delete_dashboard()
        elif event.button.id == "federation-fleet-diff":
            self.action_fleet_diff()
        elif event.button.id == "federation-safe-mode":
            self.action_toggle_safe_mode()
        elif event.button.id == "federation-focus-warning":
            self.action_focus_warning()
        elif event.button.id == "federation-manage-local":
            self.action_manage_local()
        elif event.button.id == "federation-logs-local":
            self.action_logs_local()

    def action_toggle_scope(self) -> None:
        self._scope_all_hosts = not self._scope_all_hosts
        self._render_hosts()

    def action_run_list(self) -> None:
        self._dispatch_action("list")

    def action_run_health(self) -> None:
        self._dispatch_action("health")

    def action_run_report(self) -> None:
        self._dispatch_action("report")

    def action_run_resume(self) -> None:
        self._dispatch_action("resume")

    def action_refresh(self) -> None:
        if self._loading:
            return
        now = perf_counter()
        if now < self._refresh_cooldown_until:
            wait_s = max(0.1, self._refresh_cooldown_until - now)
            self.notify(
                f"Safe refresh pacing active. Retry in {wait_s:.1f}s.",
                severity="warning",
            )
            return
        self._refresh_cooldown_until = now + self._refresh_cooldown_seconds()
        self._set_loading(True)
        self._load_hosts()

    def action_save_dashboard(self) -> None:
        name = self.query_one("#federation-dashboard-name", Input).value.strip()
        if not name:
            self.notify("Enter a pinned view name first.", severity="warning")
            return
        hosts = self._current_dashboard_hosts()
        if not hosts:
            self.notify("No hosts available for pinned view.", severity="warning")
            return
        try:
            saved = self.service.save_federation_dashboard(
                name=name,
                hosts=hosts,
                host_filter=self.query_one("#federation-filter", Input).value.strip(),
                scope_all_hosts=self._scope_all_hosts,
                safe_mode=self._safe_mode,
            )
        except WsError as error:
            self.notify(str(error), severity="error")
            return
        self.query_one("#federation-dashboard-name", Input).value = str(saved.get("name", ""))
        self._render_dashboard_list()
        self.notify(f"Pinned view saved: {saved.get('name', '')}")

    def action_apply_dashboard(self) -> None:
        name = self.query_one("#federation-dashboard-name", Input).value.strip()
        if not name:
            self.notify("Enter a pinned view name to apply.", severity="warning")
            return
        try:
            dashboard = self.service.get_federation_dashboard(name)
        except WsError as error:
            self.notify(str(error), severity="error")
            return
        hosts_raw = dashboard.get("hosts", [])
        hosts = [str(item) for item in hosts_raw] if isinstance(hosts_raw, list) else []
        self._dashboard_hosts_override = hosts
        self._scope_all_hosts = bool(dashboard.get("scope_all_hosts"))
        self._safe_mode = bool(dashboard.get("safe_mode"))
        self.query_one("#federation-filter", Input).value = str(dashboard.get("host_filter", ""))
        self._render_dashboard_list()
        self._refresh_cooldown_until = 0.0
        self.action_refresh()
        self.notify(f"Pinned view applied: {dashboard.get('name', '')}")

    def action_delete_dashboard(self) -> None:
        name = self.query_one("#federation-dashboard-name", Input).value.strip()
        if not name:
            self.notify("Enter a pinned view name to delete.", severity="warning")
            return
        try:
            self.service.delete_federation_dashboard(name)
        except WsError as error:
            self.notify(str(error), severity="error")
            return
        self._render_dashboard_list()
        self.notify(f"Pinned view deleted: {name}")

    def action_fleet_diff(self) -> None:
        try:
            payload = self.service.fleet_diff_live(hosts=self._current_dashboard_hosts())
        except WsError as error:
            self.notify(str(error), severity="error")
            return
        self._render_fleet_diff_panel(payload)
        baseline = str(payload.get("baseline", "")).strip()
        self.notify(f"Fleet diff computed{f' vs {baseline}' if baseline else ''}.")

    def action_toggle_safe_mode(self) -> None:
        self._safe_mode = not self._safe_mode
        self._refresh_cooldown_until = 0.0
        self._render_dashboard_list()
        self.notify(f"Federation safe mode {'enabled' if self._safe_mode else 'disabled'}.")
        self.action_refresh()

    def action_focus_local(self) -> None:
        self._focus_local()

    def action_focus_warning(self) -> None:
        self._focus_local(warnings_only=True)

    def action_manage_local(self) -> None:
        self._focus_local(warnings_only=True, post_action="manage")

    def action_logs_local(self) -> None:
        self._focus_local(warnings_only=True, post_action="logs")

    def action_close(self) -> None:
        self.dismiss(None)


class InterfaceControlsScreen(ModalScreen[None]):
    BINDINGS: ClassVar[list[BindingSpec]] = [
        Binding("escape", "close", "Back"),
        Binding("j", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
        Binding("enter", "choose", "Apply", show=False),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(id="interface-controls-dialog", classes="dialog small-dialog"):
            yield Label("Interface controls", classes="dialog-title")
            yield Static("", id="interface-controls-summary", classes="dialog-context")
            yield OptionList(
                Option("Switch theme", id="interface-theme"),
                Option("Open theme token inspector", id="interface-theme-tokens"),
                Option("Switch density", id="interface-density"),
                Option("Switch text scale", id="interface-text"),
                Option("Switch layout preset", id="interface-layout-preset"),
                Option("Switch motion preset", id="interface-motion"),
                Option("Toggle high contrast", id="interface-contrast"),
                Option("Toggle adaptive contrast", id="interface-auto-contrast"),
                Option("Switch accent style", id="interface-accent"),
                Option("Toggle hint detail", id="interface-hints"),
                Option("Switch hint profile", id="interface-hint-profile"),
                Option("Toggle create advanced default", id="interface-create-advanced"),
                id="interface-controls-options",
            )
            yield Static(
                "INTERFACE  Up/Down or j/k Navigate   Enter Apply   Esc Back",
                classes="mode-help",
            )
            with Horizontal(classes="dialog-actions"):
                yield Button("Close", id="interface-controls-close")

    def on_mount(self) -> None:
        animate_modal_open(self)
        self._render_summary()
        options = self.query_one("#interface-controls-options", OptionList)
        options.highlighted = 0
        options.focus()

    def _render_summary(self) -> None:
        app = self.app
        if not isinstance(app, WsApp):
            return
        self.query_one("#interface-controls-summary", Static).update(
            "Theme: "
            f"{app.ui_theme}  |  Density: {app.density}  |  Text: {app.text_scale}\n"
            f"Layout preset: {app.density}/{app.text_scale}\n"
            "Motion: "
            f"{app.motion_preset} (effective: {app.motion})"
            f"  |  Contrast: {'on' if app.high_contrast else 'off'}\n"
            "Accent: "
            f"{app.accent_mode}  |  Hints: {app.hint_level}/{app.hint_profile}"
            f"  |  Create advanced: {'on' if app.create_advanced_by_default else 'off'}\n"
            f"Adaptive contrast: {'on' if app.auto_contrast else 'off'}"
        )

    def _apply(self, option_id: str | None) -> None:
        app = self.app
        if not isinstance(app, WsApp) or option_id is None:
            return
        if option_id == "interface-theme":
            app.action_cycle_theme()
        elif option_id == "interface-theme-tokens":
            app.push_screen(ThemeTokenScreen())
            return
        elif option_id == "interface-density":
            app.action_toggle_density()
        elif option_id == "interface-text":
            app.action_cycle_text_scale()
        elif option_id == "interface-layout-preset":
            app.action_cycle_layout_preset()
        elif option_id == "interface-motion":
            app.action_cycle_motion_preset()
        elif option_id == "interface-contrast":
            app.action_toggle_high_contrast()
        elif option_id == "interface-auto-contrast":
            app.action_toggle_auto_contrast()
        elif option_id == "interface-accent":
            app.action_cycle_accent_mode()
        elif option_id == "interface-hints":
            app.action_toggle_hint_level()
        elif option_id == "interface-hint-profile":
            app.action_cycle_hint_profile()
        elif option_id == "interface-create-advanced":
            app.action_toggle_create_advanced_default()
        self._render_summary()

    @on(OptionList.OptionSelected, "#interface-controls-options")
    def option_selected(self, event: OptionList.OptionSelected) -> None:
        self._apply(event.option.id)

    @on(Button.Pressed, "#interface-controls-close")
    def close_button(self) -> None:
        self.dismiss(None)

    def action_cursor_down(self) -> None:
        self.query_one("#interface-controls-options", OptionList).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one("#interface-controls-options", OptionList).action_cursor_up()

    def action_choose(self) -> None:
        options = self.query_one("#interface-controls-options", OptionList)
        highlighted = options.highlighted
        if highlighted is None:
            return
        self._apply(options.get_option_at_index(highlighted).id)

    def action_close(self) -> None:
        self.dismiss(None)


class ThemeTokenScreen(ModalScreen[None]):
    BINDINGS: ClassVar[list[BindingSpec]] = [Binding("escape", "close", "Back")]

    def compose(self) -> ComposeResult:
        with Vertical(id="theme-token-dialog", classes="dialog small-dialog"):
            yield Label("Theme token inspector", classes="dialog-title")
            with VerticalScroll(id="theme-token-scroll"):
                yield Static("", id="theme-token-content")
            yield Static("THEME  Esc Back", classes="mode-help")
            with Horizontal(classes="dialog-actions"):
                yield Button("Close", id="theme-token-close", classes="button-secondary")

    def on_mount(self) -> None:
        animate_modal_open(self)
        app = self.app
        if not isinstance(app, WsApp):
            return
        tokens = (
            ("theme", app.ui_theme),
            ("accent_mode", app.accent_mode),
            ("high_contrast", "on" if app.high_contrast else "off"),
            ("auto_contrast", "on" if app.auto_contrast else "off"),
            ("effective_contrast", "high" if app.has_class("high-contrast") else "normal"),
            ("motion_preset", app.motion_preset),
            ("motion_effective", app.motion),
        )
        rows = [f"{label:<20} {value}" for label, value in tokens]
        for key in (
            "primary",
            "accent",
            "warning",
            "error",
            "success",
            "background",
            "surface",
            "foreground",
        ):
            value = app._theme_colors.get(key)
            if value:
                rows.append(f"{key:<20} {value}")
        self.query_one("#theme-token-content", Static).update("\n".join(rows))
        self.query_one("#theme-token-close", Button).focus()

    @on(Button.Pressed, "#theme-token-close")
    def close_button(self) -> None:
        self.dismiss(None)

    def action_close(self) -> None:
        self.dismiss(None)


class OnboardingScreen(ModalScreen[str | None]):
    BINDINGS: ClassVar[list[BindingSpec]] = [Binding("escape", "close", "Close")]

    STEPS: ClassVar[tuple[tuple[str, str], ...]] = (
        (
            "1 of 4 - Sessions",
            "Sessions continue running through tmux after your SSH connection disconnects.",
        ),
        (
            "2 of 4 - Status",
            "ws keeps tmux runtime, task progress, agent state, and input requirements separate.",
        ),
        (
            "3 of 4 - Navigation",
            "Keyboard first: / search, f filter, p palette, d manage, l logs, i timeline.",
        ),
        (
            "4 of 4 - Safety",
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
                yield Button("Skip", id="onboarding-close", classes="button-secondary")
                yield Button("Shortcut guide", id="onboarding-help", classes="button-secondary")
                yield Button("Next", variant="primary", id="onboarding-next")

    def on_mount(self) -> None:
        animate_modal_open(self)
        self.query_one("#onboarding-close", Button).focus()

    def _render_step(self) -> None:
        title, copy = self.STEPS[self.step]
        self.query_one("#onboarding-title", Label).update(title)
        self.query_one("#onboarding-copy", Static).update(copy)
        self.query_one("#onboarding-next", Button).label = (
            "Create first session" if self.step == len(self.STEPS) - 1 else "Next"
        )

    @on(Button.Pressed)
    def button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "onboarding-close":
            self.dismiss(None)
        elif event.button.id == "onboarding-help":
            self.dismiss("help")
        elif event.button.id == "onboarding-next":
            if self.step == len(self.STEPS) - 1:
                self.dismiss("create")
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
        app_now = getattr(self.app, "_now_utc", None)
        current = app_now() if callable(app_now) else ui_now_utc()
        age = max(0, int((current - self.captured_at).total_seconds()))
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
        copied = self.app.copy_to_clipboard(content)
        if copied is False:
            self.notify(
                "Copy failed. Enable terminal clipboard integration or OSC 52 support.",
                severity="warning",
            )
            return
        base = "Selected output copied" if area.selected_text else "Sanitized output copied"
        channel = getattr(self.app, "_last_copy_channel", "native")
        self.notify(base if channel in {"native", "both"} else f"{base} (OSC 52)")

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
            yield Static(modal_breadcrumb("Dashboard", "Search output"), classes="modal-breadcrumb")
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
        options.add_option(
            Option(
                f"Why empty: {message}  |  Primary action: type at least two characters.",
                id="search-output-empty",
                disabled=True,
            )
        )
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
            options.add_option(
                Option(
                    "Why empty: "
                    + message
                    + "  |  Primary action: broaden keywords."
                    + "  |  Secondary shortcut: Esc to close.",
                    id="search-output-empty",
                    disabled=True,
                )
            )
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


class TriageWizardScreen(ModalScreen[str | None]):
    BINDINGS: ClassVar[list[BindingSpec]] = [
        Binding("escape", "cancel", "Close"),
        Binding("j", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
        Binding("enter", "next_step", "Next", show=False),
    ]

    def __init__(
        self,
        service: SessionService,
        warning_sessions: Sequence[SessionView],
        notices: dict[str, ActivityNotice],
    ) -> None:
        super().__init__()
        self.service = service
        self.warning_sessions = tuple(warning_sessions)
        self.notices = dict(notices)
        self.step = 0
        self.selected_index = 0

    def compose(self) -> ComposeResult:
        with Vertical(id="triage-dialog", classes="dialog manage-dialog"):
            yield Label("Warning Triage Wizard", classes="dialog-title")
            yield Static(modal_breadcrumb("Dashboard", "Triage"), classes="modal-breadcrumb")
            yield Static("", id="triage-step")
            yield OptionList(id="triage-options")
            yield Static("", id="triage-detail")
            yield Static(
                "TRIAGE  Enter Next/apply   Up/Down Navigate   Esc Close",
                id="triage-help",
                classes="mode-help",
            )
            with Horizontal(id="triage-actions", classes="dialog-actions"):
                yield Button("Back", id="triage-back")
                yield Button("Next", variant="primary", id="triage-next")
                yield Button("Close", id="triage-close")

    def on_mount(self) -> None:
        animate_modal_open(self)
        self._render_step()

    def _selected_warning(self) -> SessionView | None:
        if not self.warning_sessions:
            return None
        self.selected_index = max(0, min(self.selected_index, len(self.warning_sessions) - 1))
        return self.warning_sessions[self.selected_index]

    def _render_step(self) -> None:
        options = self.query_one("#triage-options", OptionList)
        options.clear_options()
        detail = self.query_one("#triage-detail", Static)
        step_label = self.query_one("#triage-step", Static)
        next_button = self.query_one("#triage-next", Button)
        back_button = self.query_one("#triage-back", Button)
        back_button.disabled = self.step == 0
        if not self.warning_sessions:
            step_label.update("Step 1/3 · Select warning session")
            options.add_option(Option("No warning sessions available right now.", disabled=True))
            detail.update("Apply quick filter [4] or refresh [r] after new warnings appear.")
            next_button.disabled = True
            return
        selected = self._selected_warning()
        if selected is None:
            return
        notice = self.notices.get(selected.name)
        warning_title = notice.title if notice is not None else "No warning signature"
        if self.step == 0:
            step_label.update("Step 1/3 · Select warning session")
            for index, session in enumerate(self.warning_sessions):
                marker = "-> " if index == self.selected_index else "   "
                title = (
                    self.notices.get(session.name).title if session.name in self.notices else "-"
                )
                options.add_option(
                    Option(
                        f"{marker}{session.display_name or session.name}  [{title}]",
                        id=f"triage-session:{index}",
                    )
                )
            options.highlighted = self.selected_index
            detail.update(
                f"Selected: {selected.display_name or selected.name}\n"
                f"Warning: {warning_title}\n"
                f"Runtime: {selected.runtime.value}  Task: {selected.task_state.value}"
            )
            next_button.disabled = False
            next_button.label = "Next"
            return
        if self.step == 1:
            step_label.update("Step 2/3 · Inspect output evidence")
            preview = ""
            with contextlib.suppress(WsError):
                preview = self.service.inspect(selected.name).preview.strip()
            excerpt = "\n".join(preview.splitlines()[-4:]) if preview else "No output captured."
            options.add_option(
                Option(
                    "Review warning evidence below, then continue.",
                    id="triage-inspect",
                    disabled=True,
                )
            )
            detail.update(
                f"Session: {selected.display_name or selected.name}\n"
                f"Warning: {warning_title}\n\n"
                f"Recent output:\n{excerpt}"
            )
            next_button.disabled = False
            next_button.label = "Next"
            return
        step_label.update("Step 3/3 · Choose remediation")
        options.add_option(
            Option("Open logs for selected warning", id=f"triage-open-logs:{selected.name}")
        )
        options.add_option(
            Option(
                "Open manage actions for selected warning",
                id=f"triage-open-manage:{selected.name}",
            )
        )
        options.add_option(
            Option("Open health alerts cockpit", id=f"triage-open-health:{selected.name}")
        )
        options.add_option(Option("Apply warning filter only", id="triage-filter-only"))
        options.highlighted = 0
        detail.update(
            "Choose the most direct remediation path.\n"
            "Logs is fastest for command failures; Manage is best for restart/stop workflows."
        )
        next_button.disabled = False
        next_button.label = "Apply"

    @on(OptionList.OptionHighlighted, "#triage-options")
    def option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        option_id = event.option.id or ""
        if self.step == 0 and option_id.startswith("triage-session:"):
            with contextlib.suppress(ValueError):
                self.selected_index = int(option_id.removeprefix("triage-session:"))
                self._render_step()

    @on(OptionList.OptionSelected, "#triage-options")
    def option_selected(self, event: OptionList.OptionSelected) -> None:
        if self.step == 2:
            self.dismiss(event.option.id)
            return
        option_id = event.option.id or ""
        if self.step == 0 and option_id.startswith("triage-session:"):
            with contextlib.suppress(ValueError):
                self.selected_index = int(option_id.removeprefix("triage-session:"))
        self.action_next_step()

    @on(Button.Pressed)
    def button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "triage-close":
            self.dismiss(None)
        elif event.button.id == "triage-back":
            self.action_back_step()
        elif event.button.id == "triage-next":
            self.action_next_step()

    def action_cursor_down(self) -> None:
        self.query_one("#triage-options", OptionList).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one("#triage-options", OptionList).action_cursor_up()

    def action_back_step(self) -> None:
        if self.step == 0:
            return
        self.step -= 1
        self._render_step()

    def action_next_step(self) -> None:
        if self.step < 2:
            self.step += 1
            self._render_step()
            return
        options = self.query_one("#triage-options", OptionList)
        if options.highlighted is None:
            return
        self.dismiss(options.get_option_at_index(options.highlighted).id)

    def action_cancel(self) -> None:
        self.dismiss(None)


class WsCommandProvider(Provider):
    """Session-aware commands for Textual's built-in fuzzy palette."""

    def _commands(self) -> list[tuple[str, str, Callable[[], object]]]:
        app = self.app
        if not isinstance(app, WsApp):
            return []
        icons = {
            "create": "[+]" if app.ascii_only else "✨",
            "selected": "[S]" if app.ascii_only else "🎯",
            "dashboard": "[D]" if app.ascii_only else "🧭",
            "system": "[!]" if app.ascii_only else "🛠",
            "interface": "[I]" if app.ascii_only else "🎨",
            "help": "[?]" if app.ascii_only else "📘",
        }
        selected = app._selected()

        def unavailable() -> None:
            app.notify("Select a session before using this command.", severity="warning")

        selected_command = unavailable if selected is None else app.action_open
        edit_command = unavailable if selected is None else app.action_edit
        note_command = unavailable if selected is None else app.action_note
        logs_command = unavailable if selected is None else app.action_logs
        timeline_command = unavailable if selected is None else app.action_timeline
        pin_command = unavailable if selected is None else app.action_toggle_pin
        manage_command = unavailable if selected is None else app.action_manage
        availability = "Unavailable: no selected session" if selected is None else "Available"
        create_specs = (
            (Tool.CLAUDE, "Claude Code", "Create a persistent Claude Code session"),
            (Tool.COPILOT, "Copilot", "Create a persistent GitHub Copilot CLI session"),
            (Tool.CODEX, "Codex", "Create a persistent Codex CLI session"),
            (Tool.HERMES, "Hermes", "Create a persistent Hermes Agent session"),
            (Tool.SHELL, "Shell", "Create a persistent Linux shell session"),
        )
        create_commands: list[tuple[str, str, Callable[[], object]]] = []
        for tool, label, description in create_specs:
            profile = app.service.config.tools.get(tool)
            if profile is None or not profile.enabled:
                continue
            create_commands.append(
                (
                    f"Create · {label} session",
                    description,
                    lambda tool=tool: app.action_create(tool),
                )
            )
        if not create_commands:
            create_commands.append(
                (
                    "Create · Session",
                    "Unavailable: all tool profiles are disabled in config.toml",
                    lambda: app.notify(
                        "All tool profiles are disabled in config.toml.", severity="warning"
                    ),
                )
            )
        preset_commands: list[tuple[str, str, Callable[[], object]]] = []
        for preset in app.service.list_presets():
            preset_commands.append(
                (
                    f"Create · Preset: {preset.name}",
                    "Create a new session from the saved preset profile",
                    lambda preset_name=preset.name: app.action_create_from_preset(preset_name),
                )
            )
        filter_preset_commands: list[tuple[str, str, Callable[[], object]]] = []
        for preset in app.service.list_filter_presets():
            filter_preset_commands.append(
                (
                    f"Filter · Preset: {preset.name}",
                    "Apply saved dashboard filter preset",
                    lambda preset_name=preset.name: app.action_apply_filter_preset(preset_name),
                )
            )
        suggested_commands: list[tuple[str, str, Callable[[], object]]] = []
        if selected is None:
            suggested_commands.extend(
                (
                    (
                        "Suggested · Create a new session",
                        "Start a managed session from the default tool profile",
                        app.action_create,
                    ),
                    (
                        "Suggested · Open filters",
                        "Narrow the dashboard by tool/runtime/task/project",
                        app.action_filter,
                    ),
                )
            )
        elif selected.runtime in {RuntimeState.STOPPED, RuntimeState.FAILED}:
            suggested_commands.append(
                (
                    "Suggested · Resume stopped session",
                    "Open protected restart flow for the selected session",
                    app.action_resume_selected,
                )
            )
        else:
            suggested_commands.extend(
                (
                    (
                        "Suggested · Open selected session",
                        "Attach to the selected running session now",
                        app.action_open,
                    ),
                    (
                        "Suggested · Review recent output",
                        "Open full logs for the selected session",
                        app.action_logs,
                    ),
                )
            )
        rows = [
            *suggested_commands,
            *create_commands,
            *preset_commands,
            *filter_preset_commands,
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
                "Selected · View timeline                         i",
                availability,
                timeline_command,
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
                "Dashboard · Preset launcher                      o",
                "Choose blank session or a saved preset before opening Create",
                app.action_preset_launcher,
            ),
            (
                "Dashboard · Attention",
                "Temporarily show sessions with known warnings",
                app.action_attention,
            ),
            (
                "Dashboard · Mark all visible sessions",
                "Add every visible row to bulk selection",
                app.action_bulk_select_visible,
            ),
            (
                "Dashboard · Clear bulk selection",
                "Clear all bulk marks",
                app.action_bulk_clear_selection,
            ),
            (
                "Dashboard · Invert visible bulk selection",
                "Toggle marks for visible rows only",
                app.action_bulk_invert_visible,
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
                "Dashboard · Macro: warnings to logs              0",
                "Apply warning quick filter and open logs for the first warning session",
                app.action_macro_warnings_logs,
            ),
            (
                "Dashboard · Open triage wizard                 W",
                "Guided warning triage: select warning, inspect evidence, and choose remediation",
                app.action_triage_wizard,
            ),
            (
                "Dashboard · Undo last risky action             U",
                "Undo the last supported pin/status/logging/runtime/log action",
                app.action_undo_last_action,
            ),
            (
                "System · Run diagnostics",
                "Check tmux, tools, state, and storage",
                app.action_diagnostics,
            ),
            (
                "System · Review health alerts",
                "Disk space, apt updates, reboot flag, dirty repos, Docker",
                app.action_health_alerts,
            ),
            (
                "Dashboard · View project board",
                "Show todo/doing/blocked/done lanes for current project filter",
                app.action_project_board,
            ),
            (
                "Dashboard · View dependency graph              D",
                "Inspect blocked/unblocks relations and current critical path",
                app.action_dependency_graph,
            ),
            (
                "Dashboard · Federation control center         F",
                "Inspect remote host status and run list/health/report/resume actions",
                app.action_federation_center,
            ),
            (
                "Dashboard · Run policy sandbox                  v",
                "Interactive policy what-if preview (approvals/SLA/archive/idle)",
                app.action_policy_sandbox,
            ),
            (
                "System · Export ops snapshot report",
                "Write a plaintext operations report to state/cache diagnostics",
                app.action_export_ops_snapshot,
            ),
            (
                "Interface · Switch theme                         t",
                "Cycle the full theme pack (dark/light/terminal/high-contrast variants)",
                app.action_cycle_theme,
            ),
            (
                "Interface · Open controls panel                  I",
                "Open one panel for theme, density, text, motion, contrast, accent, and hints",
                app.action_interface_controls,
            ),
            (
                "Interface · Show theme token inspector           T",
                "Inspect active semantic theme tokens and effective contrast mode",
                app.action_theme_tokens,
            ),
            (
                "Interface · Cycle text scale                     w",
                "Cycle compact, comfortable, and readable text density",
                app.action_cycle_text_scale,
            ),
            (
                "Interface · Cycle layout preset",
                "Switch compact, comfortable, and readable layout bundles",
                app.action_cycle_layout_preset,
            ),
            (
                "Interface · Cycle motion preset                   M",
                "Switch auto/off/subtle/full animation intensity with profile-aware caps",
                app.action_cycle_motion_preset,
            ),
            (
                "Interface · Toggle contrast mode                  C",
                "Enable explicit high-contrast rendering mode",
                app.action_toggle_high_contrast,
            ),
            (
                "Interface · Cycle accent style                    A",
                "Tune accent emphasis (default, vivid, calm) and persist preference",
                app.action_cycle_accent_mode,
            ),
            (
                "Interface · Toggle hint density                   K",
                "Switch compact action-bar hints and verbose keyboard hint mode",
                app.action_toggle_hint_level,
            ),
            (
                "Interface · Toggle adaptive contrast              V",
                "Auto-enable high contrast on low-color/narrow terminals",
                app.action_toggle_auto_contrast,
            ),
            (
                "Interface · Cycle hint profile                    P",
                "Switch beginner/advanced keyboard hint profile",
                app.action_cycle_hint_profile,
            ),
            (
                "Interface · Pin common palette commands",
                "Quick-pin frequent command palette actions",
                app.action_palette_pin_defaults,
            ),
            (
                "Interface · Clear pinned palette commands",
                "Remove all pinned command palette boosts",
                app.action_palette_unpin_defaults,
            ),
            (
                "Help · Keyboard reference                        ?",
                "Open contextual keyboard help",
                app.action_help,
            ),
        ]
        decorated: list[tuple[str, str, Callable[[], object]]] = []
        for display, help_text, command in rows:
            if display.startswith("Create ·"):
                display = f"{icons['create']} {display}"
            elif display.startswith("Selected ·"):
                display = f"{icons['selected']} {display}"
            elif display.startswith("Dashboard ·"):
                display = f"{icons['dashboard']} {display}"
            elif display.startswith("System ·"):
                display = f"{icons['system']} {display}"
            elif display.startswith("Interface ·"):
                display = f"{icons['interface']} {display}"
            elif display.startswith("Help ·"):
                display = f"{icons['help']} {display}"
            decorated.append((display, help_text, command))
        return sorted(
            decorated,
            key=lambda item: (app._palette_rank(item[0]), item[0]),
            reverse=True,
        )

    async def discover(self) -> Hits:
        app = self.app
        for display, help_text, command in self._commands():
            if isinstance(app, WsApp):

                def wrapped(command=command, display=display) -> object:
                    app._track_palette_action(display)
                    return command()

                yield DiscoveryHit(display, wrapped, help=help_text)
            else:
                yield DiscoveryHit(display, command, help=help_text)

    async def search(self, query: str) -> Hits:
        app = self.app
        matcher = self.matcher(query)
        normalized_query = normalize_palette_key(query)
        for display, help_text, command in self._commands():
            score = matcher.match(display)
            normalized_display = normalize_palette_key(display)
            alias_boost = 0
            typo_boost = 0
            if normalized_query and normalized_display:
                for prefix, aliases in PALETTE_ALIASES.items():
                    if prefix not in normalized_display:
                        continue
                    if any(
                        normalized_query in alias.casefold() or alias.casefold() in normalized_query
                        for alias in aliases
                    ):
                        alias_boost = 22
                        break
                if score <= 0 and not alias_boost and len(normalized_query) >= 3:
                    ratio = SequenceMatcher(None, normalized_query, normalized_display).ratio()
                    if ratio >= 0.62:
                        typo_boost = int(26 * ratio)
            combined = score + alias_boost + typo_boost
            if combined <= 0:
                continue
            boost = app._palette_rank(display) if isinstance(app, WsApp) else 0
            if isinstance(app, WsApp):

                def wrapped(command=command, display=display) -> object:
                    app._track_palette_action(display)
                    return command()

                yield Hit(combined + boost, matcher.highlight(display), wrapped, help=help_text)
            else:
                yield Hit(combined, matcher.highlight(display), command, help=help_text)


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
        Binding("space", "toggle_bulk_select", "Mark", show=False),
        Binding("ctrl+a", "bulk_select_visible", "Mark all", show=False),
        Binding("ctrl+u", "bulk_clear_selection", "Clear marks", show=False),
        Binding("alt+i", "bulk_invert_visible", "Invert marks", show=False),
        Binding("b", "bulk_actions", "Bulk"),
        Binding("m", "manage", "Manage"),
        Binding("j", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
        Binding("n", "note", "Note"),
        Binding("i", "timeline", "Timeline"),
        Binding("e", "edit", "Edit"),
        Binding("l", "logs", "Logs"),
        Binding("asterisk", "toggle_pin", "Pin"),
        Binding("d", "manage", "Manage"),
        Binding("r", "refresh", "Refresh"),
        Binding("/", "search", "Search"),
        Binding("f", "filter", "Filter"),
        Binding("g", "cycle_grouping", "Group"),
        Binding("z", "toggle_density", "Density"),
        Binding("w", "cycle_text_scale", "Text size", show=False),
        Binding("h", "health_alerts", "Health"),
        Binding("v", "policy_sandbox", "Policy", show=False),
        Binding("D", "dependency_graph", "Dependencies", show=False),
        Binding("F", "federation_center", "Federation", show=False),
        Binding("B", "project_board", "Board", show=False),
        Binding("x", "export_ops_snapshot", "Export report", show=False),
        Binding("I", "interface_controls", "Interface", show=False),
        Binding("1", "quick_filter_all", "All", show=False),
        Binding("2", "quick_filter_active", "Active", show=False),
        Binding("3", "quick_filter_detached", "Detached", show=False),
        Binding("4", "quick_filter_warnings", "Warnings", show=False),
        Binding("5", "quick_filter_stopped", "Stopped", show=False),
        Binding("6", "quick_filter_blocked", "Blocked", show=False),
        Binding("7", "progress_todo", "Todo", show=False),
        Binding("8", "progress_doing", "Doing", show=False),
        Binding("9", "progress_done", "Done", show=False),
        Binding("0", "macro_warnings_logs", "Macro", show=False),
        Binding("W", "triage_wizard", "Triage", show=False),
        Binding("s", "search_output", "Search output"),
        Binding("o", "preset_launcher", "Presets"),
        Binding("u", "toggle_unmanaged", "All sessions", show=False),
        Binding("U", "undo_last_action", "Undo last", show=False),
        Binding("p", "command_palette", "Palette"),
        Binding("a", "advanced_details", "Advanced", show=False),
        Binding("t", "cycle_theme", "Theme", show=False),
        Binding("M", "cycle_motion_preset", "Motion", show=False),
        Binding("C", "toggle_high_contrast", "Contrast", show=False),
        Binding("A", "cycle_accent_mode", "Accent", show=False),
        Binding("K", "toggle_hint_level", "Hint detail", show=False),
        Binding("V", "toggle_auto_contrast", "Auto contrast", show=False),
        Binding("P", "cycle_hint_profile", "Hint profile", show=False),
        Binding("T", "theme_tokens", "Theme tokens", show=False),
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
        snapshot_mode: bool | None = None,
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
        self._bulk_selected: set[tuple[str, str]] = set()
        self._active_session_locks: set[str] = set()
        self._recent_jobs: deque[JobNotice] = deque(maxlen=RECENT_JOB_LIMIT)
        self._session_render_cap = 0
        self._alerts: dict[tuple[str, str], ActivityNotice] = {}
        self._activity_history: dict[tuple[str, str], deque[int]] = {}
        self._last_preview: dict[tuple[str, str], str] = {}
        self._last_preview_truncated: dict[tuple[str, str], bool] = {}
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
        self._spinner_phase = 0
        self._spinner_timer: Timer | None = None
        self.quick_filter = "all"
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
        snapshot_env = os.environ.get("WS_SNAPSHOT_MODE", "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        self.snapshot_mode = snapshot_env if snapshot_mode is None else snapshot_mode
        self._snapshot_now: datetime | None = None
        if self.snapshot_mode:
            fixed_raw = os.environ.get("WS_SNAPSHOT_NOW", "").strip()
            if fixed_raw:
                with contextlib.suppress(ValueError):
                    parsed = datetime.fromisoformat(fixed_raw)
                    self._snapshot_now = (
                        parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
                    )
            if self._snapshot_now is None:
                self._snapshot_now = datetime(2099, 1, 1, tzinfo=UTC)
        self._interface_preferences_store = InterfacePreferencesStore(self.service.paths)
        defaults = InterfacePreferences(
            grouping=self.service.config.interface.default_grouping,
            density=self.service.config.interface.default_density,
            text_scale=self.service.config.interface.default_text_scale,
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
        self.text_scale: TextScaleMode = preferences.text_scale
        self.motion_preset: MotionPresetMode = preferences.motion_preset
        self.accent_mode: AccentMode = preferences.accent_mode
        self.high_contrast = preferences.high_contrast
        self.auto_contrast = preferences.auto_contrast
        self.create_advanced_by_default = preferences.create_advanced_by_default
        self.hint_level: HintLevel = preferences.hint_level
        self.hint_profile: HintProfile = preferences.hint_profile
        self._project_visual_profiles: dict[str, ProjectVisualProfile] = dict(
            preferences.project_profiles
        )
        self._active_project_profile = ""
        self._applying_project_profile = False
        self._auto_high_contrast = False
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
        no_animation_env = os.environ.get("WS_NO_ANIMATION", "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        low_terminal_capability = (
            os.environ.get("TERM", "").strip().lower()
            in {
                "dumb",
                "unknown",
            }
            or self.ascii_only
        )
        self._base_motion = (
            "off"
            if no_animation
            or no_animation_env
            or self.snapshot_mode
            or low_terminal_capability
            or self.service.config.interface.reduce_motion
            or configured_motion == "off"
            or self.monochrome
            else configured_motion
        )
        self.motion = self._base_motion
        self._refresh_latency_ms = 0.0
        self._refresh_latency_ema_ms = 0.0
        self._render_stats_ms = 0.0
        self._render_stats_rows = 0
        self._auto_safe_mode = False
        self._effective_profile = self.service.config.interface.performance_profile
        self._palette_usage: dict[str, int] = {}
        self._palette_recent: deque[str] = deque(maxlen=24)
        self._action_invocations: dict[str, int] = {}
        self._manage_success_snapshots: dict[str, dict[str, str]] = {}
        self._palette_pinned: set[str] = {
            normalize_palette_key("Dashboard · Search sessions"),
            normalize_palette_key("Dashboard · Filter sessions"),
            normalize_palette_key("Create · Session"),
            normalize_palette_key("Dashboard · Preset launcher"),
            normalize_palette_key("Interface · Open controls panel"),
        }
        self._last_copy_channel: Literal["none", "native", "osc52", "both"] = "none"
        self._form_drafts: dict[str, dict[str, object]] = {}
        self._notification_groups: dict[tuple[str, str], tuple[int, datetime]] = {}
        self._shortcut_pulse = ""
        self._shortcut_pulse_timer: Timer | None = None
        self._theme_recommendation_sent = False
        profile = self.service.config.interface.performance_profile
        if self.snapshot_mode:
            self._effective_refresh_interval = 60.0
        elif profile == "ssh-safe":
            self._effective_refresh_interval = max(5.0, self.service.config.refresh_interval)
        elif profile == "rich":
            self._effective_refresh_interval = max(1.0, self.service.config.refresh_interval * 0.75)
        else:
            self._effective_refresh_interval = self.service.config.refresh_interval
        self._effective_refresh_interval = self._adaptive_refresh_interval(
            self._effective_refresh_interval
        )
        self.motion = self._effective_motion(auto_safe=False)

    def _adaptive_refresh_interval(self, base_interval: float) -> float:
        """Scale refresh interval under high load or remote SSH links."""
        interval = base_interval
        if os.environ.get("SSH_CONNECTION"):
            interval *= 1.1
        try:
            load_1m = os.getloadavg()[0]
        except (OSError, AttributeError):
            load_1m = 0.0
        if load_1m >= 8.0:
            interval *= 1.8
        elif load_1m >= 4.0:
            interval *= 1.4
        return min(20.0, max(1.0, interval))

    def _base_profile_interval(self) -> float:
        profile = self.service.config.interface.performance_profile
        if self.snapshot_mode:
            return 60.0
        if profile == "ssh-safe":
            return max(5.0, self.service.config.refresh_interval)
        if profile == "rich":
            return max(1.0, self.service.config.refresh_interval * 0.75)
        return self.service.config.refresh_interval

    def _set_refresh_timer_interval(self, interval: float) -> None:
        normalized = min(30.0, max(1.0, interval))
        if abs(normalized - self._effective_refresh_interval) < 0.05:
            return
        self._effective_refresh_interval = normalized
        if self.snapshot_mode or self._dashboard_refresh_timer is None:
            return
        paused = self._dashboard_refresh_suspensions > 0
        self._dashboard_refresh_timer.stop()
        self._dashboard_refresh_timer = self.set_interval(
            self._effective_refresh_interval, self.refresh_sessions
        )
        if paused:
            self._dashboard_refresh_timer.pause()

    def _update_performance_budget(self) -> None:
        profile = self.service.config.interface.performance_profile
        width = max(40, self.size.width or 40)
        height = max(15, self.size.height or 15)
        latency = self._refresh_latency_ema_ms
        terminal_pressure = 1.0
        if width < 100 or height < 32:
            terminal_pressure = 1.2
        if width < 85 or height < 26:
            terminal_pressure = 1.5
        latency_pressure = 1.0
        if latency >= 1200:
            latency_pressure = 1.8
        elif latency >= 800:
            latency_pressure = 1.5
        elif latency >= 450:
            latency_pressure = 1.25
        interval = self._adaptive_refresh_interval(
            self._base_profile_interval() * terminal_pressure * latency_pressure
        )
        self._set_refresh_timer_interval(interval)
        baseline = max(120, height * 10)
        multiplier = 1.0 if profile == "ssh-safe" else 2.0 if profile == "balanced" else 4.0
        if width < 100:
            multiplier = min(multiplier, 1.5)
        if width < 85:
            multiplier = min(multiplier, 1.0)
        if latency >= 1200:
            multiplier = min(multiplier, 1.0)
        elif latency >= 800:
            multiplier = min(multiplier, 1.5)
        self._session_render_cap = max(120, int(baseline * multiplier))

        auto_safe = (
            not self.snapshot_mode
            and profile != "ssh-safe"
            and ((os.environ.get("SSH_CONNECTION") and latency >= 800) or latency >= 1200)
        )
        self._auto_safe_mode = bool(auto_safe)
        self._effective_profile = "ssh-safe(auto)" if self._auto_safe_mode else profile
        self.motion = self._effective_motion(auto_safe=self._auto_safe_mode)
        self.set_class(self.motion == "off", "motion-off")
        if self.query("#app-header"):
            self._render_header()

    def _now_utc(self) -> datetime:
        if self.snapshot_mode and self._snapshot_now is not None:
            return self._snapshot_now
        return datetime.now(UTC)

    def _persist_interface_preferences(self) -> None:
        if not self._applying_project_profile:
            self._save_project_visual_profile_for_selection()
        try:
            self._interface_preferences_store.save(
                InterfacePreferences(
                    grouping=self.grouping,
                    density=self.density,
                    text_scale=self.text_scale,
                    motion_preset=self.motion_preset,
                    accent_mode=self.accent_mode,
                    high_contrast=self.high_contrast,
                    auto_contrast=self.auto_contrast,
                    create_advanced_by_default=self.create_advanced_by_default,
                    hint_level=self.hint_level,
                    hint_profile=self.hint_profile,
                    project_profiles=self._project_visual_profiles,
                )
            )
        except (OSError, WsError) as error:
            self.notify(str(error), title="Unable to save interface preference", severity="warning")

    def _project_profile_key(self, project: str) -> str:
        return project.strip().casefold()

    def _save_project_visual_profile_for_selection(self) -> None:
        selected = self._selected()
        if selected is None or not selected.project.strip():
            return
        key = self._project_profile_key(selected.project)
        if not key:
            return
        self._active_project_profile = key
        self._project_visual_profiles[key] = ProjectVisualProfile(
            ui_theme=self.ui_theme,
            density=self.density,
            text_scale=self.text_scale,
            motion_preset=self.motion_preset,
            accent_mode=self.accent_mode,
            high_contrast=self.high_contrast,
            auto_contrast=self.auto_contrast,
            hint_level=self.hint_level,
            hint_profile=self.hint_profile,
        )

    def _apply_project_visual_profile(self, project: str) -> None:
        key = self._project_profile_key(project)
        if key == self._active_project_profile and not self._applying_project_profile:
            return
        self._active_project_profile = key
        if not key:
            return
        profile = self._project_visual_profiles.get(key)
        if profile is None:
            return
        self._applying_project_profile = True
        try:
            self.ui_theme = profile.ui_theme if profile.ui_theme in THEME_MODES else self.ui_theme
            self.monochrome = self.ui_theme == "monochrome"
            self.theme = self.ui_theme
            self.density = profile.density
            self.text_scale = profile.text_scale
            self.motion_preset = profile.motion_preset
            self.accent_mode = profile.accent_mode
            self.high_contrast = profile.high_contrast
            self.auto_contrast = profile.auto_contrast
            self.hint_level = profile.hint_level
            self.hint_profile = profile.hint_profile
            self.motion = self._effective_motion(auto_safe=self._auto_safe_mode)
            self._refresh_theme_colors()
            self.set_class(self.motion == "off", "motion-off")
            self.set_class(self.density == "compact", "compact-density")
            self._apply_text_scale_class()
            self._apply_visual_mode_classes()
            self._refresh_auto_contrast()
            self._render_session_toolbar()
            self._render_header()
            self._render_action_bar()
        finally:
            self._applying_project_profile = False

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
        self.call_after_refresh(self._restore_detail_viewport)
        if self.selected_name is not None and self.selected_session_id is not None:
            identity = (self.selected_name, self.selected_session_id)
            stored = self._detail_viewports.get(identity)
            if stored is not None:
                inspector_y, output_y = stored
                inspector = self.query_one("#inspector-scroll", VerticalScroll)
                output = self.query_one("#recent-output-scroll", VerticalScroll)
                self.set_timer(
                    0.01,
                    lambda: inspector.scroll_to(y=inspector_y, animate=False, force=True),
                )
                self.set_timer(
                    0.02,
                    lambda: output.scroll_to(y=output_y, animate=False, force=True),
                )
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
        self._animate_workspace_transition("backward")

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
            yield Static("", id="jobs-row")
        with Horizontal(id="search-mode"):
            yield Static("Search", id="search-label")
            yield Input(placeholder="name, tool, task, project, tag", id="search")
            yield Static("Enter apply  Esc cancel", id="search-hint")
        with Horizontal(id="workspace"):
            with Vertical(id="session-pane"):
                with Horizontal(id="session-toolbar"):
                    yield Button(
                        "+ Create", id="toolbar-create", classes="button-primary", compact=True
                    )
                    yield Button(
                        "Resume", id="toolbar-resume", classes="button-secondary", compact=True
                    )
                    yield Button(
                        "Filter", id="toolbar-filter", classes="button-secondary", compact=True
                    )
                    yield Button(
                        "Group", id="toolbar-group", classes="button-secondary", compact=True
                    )
                    yield Button(
                        "Health", id="toolbar-health", classes="button-secondary", compact=True
                    )
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
                            classes="session-action-button button-primary",
                            compact=True,
                        )
                        yield Button(
                            "▶ Resume",
                            id="action-resume",
                            classes="session-action-button button-primary",
                            compact=True,
                        )
                        yield Button(
                            "Logs",
                            id="action-logs",
                            classes="session-action-button button-secondary",
                            compact=True,
                        )
                        yield Button(
                            "Pin",
                            id="action-pin",
                            classes="session-action-button button-secondary",
                            compact=True,
                        )
                        yield Button(
                            "Manage",
                            id="action-manage",
                            classes="session-action-button button-secondary",
                            compact=True,
                        )
                        yield Button(
                            "Stop",
                            id="action-stop",
                            classes="session-action-button button-danger",
                            compact=True,
                        )
                    yield Static("", id="action-disabled-reason")
        yield Static("", id="action-bar")
        yield Static("", id="small-terminal")

    def on_mount(self) -> None:
        self.set_class(self.motion == "off", "motion-off")
        self._refresh_auto_contrast()
        if self.ascii_only:
            self.query_one("#action-open", Button).label = "> Attach"
            self.query_one("#action-resume", Button).label = "> Resume"
        self._set_interaction_mode(InteractionMode.NORMAL)
        self._set_layout_classes(self.size.width, self.size.height)
        self._refresh_theme_colors()
        self.refresh_sessions()
        self.query_one("#sessions", OptionList).focus()
        if not self.snapshot_mode:
            self._dashboard_refresh_timer = self.set_interval(
                self._effective_refresh_interval,
                self.refresh_sessions,
            )
        # Cache-only read: never blocks first paint on a live health check.
        self._health_checks = self.service.cached_health_alerts()
        self._render_health_row()
        self._render_jobs_row()
        self._start_health_scan()
        self.set_class(self.density == "compact", "compact-density")
        self._apply_text_scale_class()
        if not self.snapshot_mode:
            self._start_spinner()
            self._start_marker_pulse()
        self._maybe_suggest_accessible_theme()
        self.call_after_refresh(self._maybe_show_onboarding)

    def on_unmount(self) -> None:
        self._attention_scan_generation += 1
        self._stop_attention_dots()
        self._stop_spinner()
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
            "viewport-80x24",
            "viewport-100x30",
        ):
            self.remove_class(name)
        if width <= 80 and height <= 24:
            self.add_class("viewport-80x24")
        if width <= 100 and height <= 30:
            self.add_class("viewport-100x30")
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
        self._apply_compact_labels()
        self._refresh_auto_contrast()
        self._update_performance_budget()

    def _apply_compact_labels(self) -> None:
        compact = self.has_class("viewport-80x24")
        selected = self._selected()
        pin_label = "Unpin" if selected is not None and selected.pinned else "Pin"
        if compact:
            labels = {
                "#toolbar-create": "+New",
                "#toolbar-resume": ">Run",
                "#toolbar-filter": "/Fil",
                "#toolbar-group": "#Grp",
                "#toolbar-health": "!Ops",
                "#action-open": ">Open",
                "#action-resume": ">Run",
                "#action-logs": "=Logs",
                "#action-pin": pin_label,
                "#action-manage": "+More",
                "#action-stop": "!Stop",
            }
        else:
            labels = {
                "#toolbar-create": "+ Create",
                "#toolbar-resume": "Resume",
                "#toolbar-filter": "Filter",
                "#toolbar-group": "Group",
                "#toolbar-health": "Health",
                "#action-open": "> Attach" if self.ascii_only else "↗ Attach",
                "#action-resume": "> Resume" if self.ascii_only else "▶ Resume",
                "#action-logs": "Logs",
                "#action-pin": pin_label,
                "#action-manage": "Manage",
                "#action-stop": "Stop",
            }
        for selector, label in labels.items():
            if self.query(selector):
                self.query_one(selector, Button).label = label

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

    def _maybe_suggest_accessible_theme(self) -> None:
        if self._theme_recommendation_sent:
            return
        if not self._terminal_needs_high_contrast():
            return
        if self.high_contrast and self.accent_mode == "safe":
            return
        self._theme_recommendation_sent = True
        self._notify_grouped(
            "Terminal capability looks limited. Recommended: "
            "enable high contrast (C) and safe accent (A).",
            severity="information",
            title="Theme recommendation",
            window_seconds=20,
        )

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

    def _notify_with_severity(self, message: str, *, severity: str, title: str = "") -> None:
        markers = {
            "information": "-" if self.ascii_only else "•",
            "warning": "!" if self.ascii_only else "⚠",
            "error": "x" if self.ascii_only else "✕",
            "success": "OK" if self.ascii_only else "✓",
        }
        marker = markers.get(severity, markers["information"])
        self.notify(f"{marker} {message}", severity=severity, title=title or None)

    def _animate_workspace_transition(self, direction: Literal["forward", "backward"]) -> None:
        if self.motion == "off" or not self.query("#workspace"):
            return
        workspace = self.query_one("#workspace", Horizontal)
        offset = (1, 0) if direction == "forward" else (-1, 0)
        workspace.styles.offset = (0, 0)
        final_offset = workspace.styles.offset
        workspace.styles.opacity = 0.88
        workspace.styles.offset = offset
        workspace.call_after_refresh(workspace.styles.animate, "opacity", 1.0, duration=0.12)
        workspace.call_after_refresh(
            workspace.styles.animate,
            "offset",
            final_offset,
            duration=0.14,
        )

    def _run_with_button_loading(
        self,
        button: Button,
        action: Callable[[], None],
        *,
        loading_label: str = "Working...",
    ) -> None:
        previous_label = str(button.label)
        button.add_class("button-loading")
        button.disabled = True
        button.label = loading_label
        try:
            action()
        finally:
            button.remove_class("button-loading")
            button.label = previous_label
            button.disabled = False

    def _pulse_button_feedback(self, selector: str, *, success: bool = True) -> None:
        if not self.query(selector):
            return
        button = self.query_one(selector, Button)
        transient = "button-feedback-success" if success else "button-feedback-error"
        button.add_class(transient)
        self.set_timer(0.28, lambda: button.remove_class(transient))

    def _save_form_draft(self, key: str, payload: dict[str, object]) -> None:
        self._form_drafts[key] = payload

    def _load_form_draft(self, key: str) -> dict[str, object] | None:
        return self._form_drafts.get(key)

    def _clear_form_draft(self, key: str) -> None:
        self._form_drafts.pop(key, None)

    def _notify_grouped(
        self,
        message: str,
        *,
        severity: str = "information",
        title: str = "",
        window_seconds: float = 6.0,
    ) -> None:
        now = self._now_utc()
        key = (severity, message)
        count, expires = self._notification_groups.get(key, (0, now))
        if now <= expires:
            count += 1
        else:
            count = 1
        self._notification_groups[key] = (count, now + timedelta(seconds=window_seconds))
        rendered = f"{message} (x{count})" if count > 1 else message
        self.notify(rendered, severity=severity, title=title or None)

    def _pulse_shortcut_hint(self, hint: str) -> None:
        self._shortcut_pulse = hint
        if self._shortcut_pulse_timer is not None:
            self._shortcut_pulse_timer.stop()
        self._shortcut_pulse_timer = self.set_timer(2.2, self._clear_shortcut_hint)
        self._render_action_bar()

    def _clear_shortcut_hint(self) -> None:
        self._shortcut_pulse = ""
        self._shortcut_pulse_timer = None
        if self.is_running and self.query("#action-bar"):
            self._render_action_bar()

    def _track_action_invocation(self, action: str, shortest_path: str) -> None:
        count = self._action_invocations.get(action, 0) + 1
        self._action_invocations[action] = count
        if count in {3, 7}:
            self._notify_grouped(
                f"Shortcut optimizer: quickest path for {action} is {shortest_path}.",
                title="Keyboard path",
                severity="information",
                window_seconds=20,
            )

    def _announce_undo_hint(self) -> None:
        self._pulse_shortcut_hint("Undo available: press U (or run ws undo)")

    def _track_palette_action(self, key: str) -> None:
        normalized = normalize_palette_key(key)
        if not normalized:
            return
        self._palette_usage[normalized] = self._palette_usage.get(normalized, 0) + 1
        self._palette_recent.appendleft(normalized)

    def _palette_rank(self, key: str) -> int:
        normalized = normalize_palette_key(key)
        if not normalized:
            return 0
        usage_score = self._palette_usage.get(normalized, 0) * 4
        recent_bonus = 0
        for index, value in enumerate(self._palette_recent):
            if value != normalized:
                continue
            recent_bonus = max(recent_bonus, 8 - min(index, 7))
            break
        pinned_bonus = 12 if any(item in normalized for item in self._palette_pinned) else 0
        context_bonus = 0
        if "suggested ·" in normalized:
            context_bonus = 10
        return usage_score + recent_bonus + pinned_bonus + context_bonus

    def _apply_text_scale_class(self) -> None:
        self.set_class(self.text_scale == "compact", "text-scale-compact")
        self.set_class(self.text_scale == "readable", "text-scale-readable")

    def _apply_visual_mode_classes(self) -> None:
        effective_contrast = self.high_contrast or self._auto_high_contrast
        self.set_class(effective_contrast, "high-contrast")
        self.set_class(self._auto_high_contrast and not self.high_contrast, "high-contrast-auto")
        self.set_class(self.accent_mode == "vivid", "accent-vivid")
        self.set_class(self.accent_mode == "calm", "accent-calm")
        self.set_class(self.accent_mode == "safe", "accent-safe")

    def _terminal_needs_high_contrast(self) -> bool:
        term = os.environ.get("TERM", "").strip().lower()
        if term in {"linux", "vt100", "xterm-mono"}:
            return True
        if (
            self.size.width
            and self.size.height
            and self.size.width <= 80
            and self.size.height <= 24
        ):
            return True
        color_term = os.environ.get("COLORTERM", "").strip().lower()
        if "truecolor" in color_term or "24bit" in color_term:
            return False
        return "256color" not in term

    def _refresh_auto_contrast(self) -> None:
        self._auto_high_contrast = bool(self.auto_contrast and self._terminal_needs_high_contrast())
        self._apply_visual_mode_classes()

    def _osc52_copy(self, text: str) -> bool:
        if not text:
            return False
        if not sys.stdout.isatty():
            return False
        if os.environ.get("WS_DISABLE_OSC52", "").strip().lower() in {"1", "true", "yes", "on"}:
            return False
        payload_bytes = text.encode("utf-8")
        # Keep OSC 52 payloads bounded for terminal and tmux compatibility.
        if len(payload_bytes) > 75_000:
            payload_bytes = payload_bytes[-75_000:]
        payload = base64.b64encode(payload_bytes).decode("ascii")
        sequence = f"\033]52;c;{payload}\a"
        if os.environ.get("TMUX"):
            sequence = f"\033Ptmux;\033{sequence}\033\\"
        try:
            print(sequence, end="", flush=True)
        except OSError:
            return False
        return True

    def copy_to_clipboard(self, text: str) -> bool:
        normalized = text.replace("\r\n", "\n")
        native_copied = False
        try:
            super().copy_to_clipboard(normalized)
            native_copied = True
        except (RuntimeError, OSError):
            native_copied = False
        osc52_copied = self._osc52_copy(normalized)
        if native_copied and osc52_copied:
            self._last_copy_channel = "both"
            return True
        if native_copied:
            self._last_copy_channel = "native"
            return True
        if osc52_copied:
            self._last_copy_channel = "osc52"
            return True
        self._last_copy_channel = "none"
        return False

    @staticmethod
    def _motion_intensity(mode: str) -> int:
        return {"off": 0, "subtle": 1, "full": 2}.get(mode, 1)

    def _profile_motion_target(self, profile: str) -> Literal["off", "subtle", "full"]:
        if profile == "ssh-safe":
            return "off"
        if profile == "rich":
            return "full"
        return "subtle"

    def _effective_motion(self, *, auto_safe: bool) -> Literal["off", "subtle", "full"]:
        if self._base_motion == "off" or auto_safe:
            return "off"
        profile = self.service.config.interface.performance_profile
        profile_target = self._profile_motion_target(profile)
        if self.motion_preset == "auto":
            desired: Literal["off", "subtle", "full"] = self._base_motion
            if profile == "ssh-safe":
                desired = "off"
            elif profile == "rich" and desired == "subtle":
                desired = "full"
            explicit = False
        elif self.motion_preset == "off":
            desired = "off"
            explicit = True
        elif self.motion_preset == "subtle":
            desired = "subtle"
            explicit = True
        else:
            desired = "full"
            explicit = True
        if explicit and self._motion_intensity(desired) > self._motion_intensity(profile_target):
            return profile_target
        return desired

    def _record_job(self, label: str, *, severity: Literal["success", "error", "info"]) -> None:
        self._recent_jobs.appendleft(JobNotice(label=label, severity=severity, at=self._now_utc()))
        self._render_jobs_row()

    def _render_jobs_row(self) -> None:
        if not self.query("#jobs-row"):
            return
        active: list[str] = []
        if self._session_refreshing:
            active.append("refreshing sessions")
        if self._attention_scanning:
            active.append("scanning alerts")
        if self._health_scanning:
            active.append("updating health")
        if self._active_session_locks:
            active.append(f"{len(self._active_session_locks)} session ops running")
        row = self.query_one("#jobs-row", Static)
        if active:
            spinner = self._spinner_glyph() if self.motion != "off" else "..."
            row.update(f"{spinner} Jobs: {', '.join(active)}")
            self.set_class(True, "has-jobs-row")
            return
        if not self._recent_jobs:
            row.update("")
            self.set_class(False, "has-jobs-row")
            return
        icon = {
            "success": ("✓", "OK"),
            "error": ("✕", "XX"),
            "info": ("•", "-"),
        }
        parts: list[str] = []
        now = self._now_utc()
        for event in list(self._recent_jobs)[:3]:
            elapsed = max(0, int((now - event.at).total_seconds()))
            marker = icon[event.severity][1 if self.ascii_only else 0]
            parts.append(f"{marker} {event.label} ({elapsed}s)")
        row.update("Recent jobs: " + ("  |  ".join(parts)))
        self.set_class(True, "has-jobs-row")

    def _run_session_locked(
        self,
        name: str,
        operation: str,
        action: Callable[[], None],
    ) -> bool:
        if name in self._active_session_locks:
            self.notify(
                f"{name} already has an in-flight action; wait before {operation}.",
                severity="warning",
            )
            return False
        self._active_session_locks.add(name)
        self._render_jobs_row()
        try:
            action()
        finally:
            self._active_session_locks.discard(name)
            self._render_jobs_row()
        return True

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
        if self.snapshot_mode:
            return
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
        self._render_jobs_row()
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

    def _start_spinner(self) -> None:
        if self.motion == "off":
            return
        if self._spinner_timer is not None:
            self._spinner_timer.stop()
        self._spinner_timer = self.set_interval(0.45, self._tick_spinner)

    def _stop_spinner(self) -> None:
        if self._spinner_timer is not None:
            self._spinner_timer.stop()
            self._spinner_timer = None

    def _tick_spinner(self) -> None:
        if not self.is_running or self.motion == "off":
            return
        self._spinner_phase = (self._spinner_phase + 1) % 4
        if self._session_refreshing:
            self._render_header()
        if self._attention_scanning:
            self._render_attention_action()

    def _start_marker_pulse(self) -> None:
        if self.motion == "off":
            return
        if self._marker_pulse_timer is not None:
            self._marker_pulse_timer.stop()
        self._marker_pulse_timer = self.set_interval(0.9, self._tick_marker_pulse)

    def _tick_marker_pulse(self) -> None:
        if not self.is_running:
            return
        self._marker_pulse_phase = not self._marker_pulse_phase
        options = self.query_one("#sessions", OptionList)
        for session in self._option_sessions.values():
            notice = self._notice_for(session)
            if session.runtime is not RuntimeState.ATTACHED and not notice.warning:
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
                notice=notice,
                pulse_dim=self._marker_pulse_phase,
                compact=self.density == "compact" or self.has_class("narrow"),
                pulse_active=self._marker_pulse_phase,
                now=self._now_utc(),
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
        self._render_jobs_row()
        request_by_identity = {
            (request.session.name, request.session.session_id): request for request in requests
        }
        errors: list[str] = []
        new_warnings: list[str] = []
        warning_membership_changed = False
        baseline_was_established = self._attention_baseline_established
        observed_at = self._now_utc()
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
        if self.snapshot_mode:
            try:
                self._health_checks = self.service.refresh_health_alerts(force=force)
                self._health_scan_error = ""
            except (OSError, WsError) as exc:
                self._health_checks = self.service.cached_health_alerts()
                self._health_scan_error = str(exc)
            self._health_dismissed_until_refresh = False
            self._render_health_row()
            return
        if (
            self._health_scanning
            or self._dashboard_refresh_suspensions
            or len(self.screen_stack) > 1
        ):
            return
        if not self.service.config.health.enabled:
            return
        stale = self.service.health_stale_names(self._now_utc())
        if not stale and not force:
            return
        self._health_scan_generation += 1
        self._health_scanning = True
        self._render_jobs_row()
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
        self._render_jobs_row()
        self._health_scan_error = error
        self._health_checks = checks
        self._health_dismissed_until_refresh = False
        if error:
            self._record_job("health refresh failed", severity="error")
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
        if self.snapshot_mode:
            self._session_refresh_generation += 1
            generation = self._session_refresh_generation
            try:
                sessions = self.service.list_sessions(include_unmanaged=self.show_unmanaged)
                error = ""
            except WsError as exc:
                sessions = []
                error = str(exc)
            self._finish_session_refresh(generation, sessions, error, 0.0)
            return
        if self._session_refreshing:
            self._pending_session_refresh = True
            return
        self._health_dismissed_until_refresh = False
        self._render_health_row()
        self._session_refreshing = True
        self._render_jobs_row()
        self._session_refresh_generation += 1
        self.add_class("refreshing")
        self._render_header()
        self._load_session_inventory(
            self._session_refresh_generation,
            self.show_unmanaged,
            perf_counter(),
        )

    @work(thread=True, exclusive=True, group="session-refresh")
    def _load_session_inventory(
        self, generation: int, include_unmanaged: bool, started_at: float
    ) -> None:
        try:
            sessions = self.service.list_sessions(include_unmanaged=include_unmanaged)
            error = ""
        except WsError as exc:
            sessions = []
            error = str(exc)
        elapsed_ms = max(0.1, (perf_counter() - started_at) * 1000)
        self.call_from_thread(self._finish_session_refresh, generation, sessions, error, elapsed_ms)

    def _finish_session_refresh(
        self,
        generation: int,
        sessions: list[SessionView],
        error: str,
        elapsed_ms: float,
    ) -> None:
        if generation != self._session_refresh_generation or not self.is_running:
            return
        self._refresh_latency_ms = elapsed_ms
        if elapsed_ms > 0:
            if self._refresh_latency_ema_ms <= 0:
                self._refresh_latency_ema_ms = elapsed_ms
            else:
                self._refresh_latency_ema_ms = (self._refresh_latency_ema_ms * 0.7) + (
                    elapsed_ms * 0.3
                )
        self._update_performance_budget()
        self._session_refreshing = False
        self._render_jobs_row()
        if error:
            self.tmux_connected = False
            self.refresh_error = error
            self.remove_class("refreshing")
            self._render_header()
            self._record_job("session refresh failed", severity="error")
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
            self.last_refreshed_at = self._now_utc()
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
        self._last_preview_truncated = {
            identity: truncated
            for identity, truncated in self._last_preview_truncated.items()
            if identity in valid_identities
        }
        self._bulk_selected = {
            identity for identity in self._bulk_selected if identity in valid_identities
        }
        self._render_options()
        self.remove_class("refreshing")
        self.last_refreshed_at = self._now_utc()
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

    def _render_output_preview(
        self, preview_text: str, notice: ActivityNotice, *, preview_truncated: bool
    ) -> None:
        preview = Text()
        if self.output_mode == "summary":
            preview = summarize_output(
                preview_text, notice, self._theme_colors.get("warning", "yellow")
            )
        else:
            if preview_truncated:
                preview.append("[older output truncated]\n", "dim")
            preview.append(preview_text or "No output captured yet.")
        self.query_one("#recent-output", Static).update(preview)
        line_count = len(preview_text.splitlines())
        truncated = "truncated" if preview_truncated else "complete"
        self.query_one("#output-meta", Static).update(
            f"{self.output_mode.title()}  {line_count} lines  {truncated}  "
            "sanitized  l open full logs"
        )

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
                compact=self.density == "compact" or self.has_class("narrow"),
                pulse_active=self._marker_pulse_phase,
                now=self._now_utc(),
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
                compact=self.density == "compact" or self.has_class("narrow"),
                pulse_active=self._marker_pulse_phase,
                now=self._now_utc(),
            ),
        )

    def _matches_query(self, session: SessionView) -> bool:
        if self.quick_filter == "active" and session.runtime is not RuntimeState.ATTACHED:
            return False
        if self.quick_filter == "detached" and session.runtime is not RuntimeState.DETACHED:
            return False
        if self.quick_filter == "warnings" and not is_warning(session, self._notice_for(session)):
            return False
        if self.quick_filter == "stopped" and session.runtime not in {
            RuntimeState.STOPPED,
            RuntimeState.FAILED,
        }:
            return False
        if self.quick_filter == "blocked" and session.task_state is not TaskState.BLOCKED:
            return False
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

    def _spinner_glyph(self) -> str:
        frames = ("-", "\\", "|", "/") if self.ascii_only else ("◐", "◓", "◑", "◒")
        return frames[self._spinner_phase % len(frames)]

    def _attention_label(self) -> str:
        warnings = self._warning_count() + self._health_warning_count()
        if self._attention_scan_error:
            return f"Attention ({warnings} known, delayed)"
        if warnings:
            return f"Attention ({warnings})"
        if not self._attention_complete():
            if self.motion == "off":
                return "Attention (checking)"
            return f"Attention (checking {self._spinner_glyph()})"
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
        started = perf_counter()
        options = self.query_one("#sessions", OptionList)
        old_scroll = options.scroll_offset.y
        old_identity = (self.selected_name, self.selected_session_id)
        self._rendering_options = True
        options.clear_options()
        self._option_sessions.clear()
        self._option_actions.clear()

        matched_sessions = [item for item in self.sessions if self._matches_query(item)]
        self.visible_sessions = matched_sessions
        render_sessions = matched_sessions
        overflow_count = 0
        cap = self._session_render_cap
        if cap > 0 and len(matched_sessions) > cap:
            overflow_count = len(matched_sessions) - cap
            render_sessions = matched_sessions[:cap]
            selected_identity = (self.selected_name, self.selected_session_id)
            selected = next(
                (
                    item
                    for item in matched_sessions
                    if (item.name, item.session_id) == selected_identity
                ),
                None,
            )
            if selected is not None and selected not in render_sessions and cap > 1:
                render_sessions = [*render_sessions[:-1], selected]
                overflow_count = len(matched_sessions) - len(render_sessions)
        selected_index: int | None = None
        first_session_index: int | None = None
        for group_name, group_sessions in build_session_groups(
            render_sessions,
            grouping=self.grouping,
            notices=self._notice_for,
            now=self._now_utc(),
        ):
            if not group_sessions:
                continue
            options.add_option(self._heading_option(f"{group_name} ({len(group_sessions)})"))
            for session in group_sessions:
                option_id = session_option_id(session.name, session.session_id)
                self._option_sessions[option_id] = session
                prompt = session_row(
                    session,
                    self._row_width(),
                    ascii_only=self.ascii_only,
                    monochrome=self.monochrome,
                    activity_spark=self._activity_spark_for(session),
                    warning_color=self._theme_colors.get("warning", "yellow"),
                    warning_dim_color=self._theme_colors.get("warning-muted", "#8a7a2a"),
                    notice=self._notice_for(session),
                    compact=self.density == "compact" or self.has_class("narrow"),
                    pulse_active=self._marker_pulse_phase,
                    now=self._now_utc(),
                )
                if (session.name, session.session_id) in self._bulk_selected:
                    marker = "[x] " if self.ascii_only else "▣ "
                    prompt = Text.assemble((marker, "bold #8a7fff"), prompt)
                options.add_option(
                    Option(
                        prompt,
                        id=option_id,
                    )
                )
                if first_session_index is None:
                    first_session_index = options.option_count - 1
                if (session.name, session.session_id) == old_identity:
                    selected_index = options.option_count - 1

        if overflow_count > 0:
            rendered = len(render_sessions)
            marker = "..." if self.ascii_only else "⋯"
            options.add_option(
                self._heading_option(
                    f"{marker} Showing {rendered} of {len(matched_sessions)} sessions; "
                    "refine filters to narrow results"
                )
            )

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
        self._render_stats_rows = len(self._option_sessions)
        self._render_stats_ms = max(0.1, (perf_counter() - started) * 1000)
        self.call_after_refresh(options.scroll_to, y=old_scroll, animate=False, force=True)
        self._render_session_toolbar()
        self._render_header()
        self._render_action_bar()

    def _render_session_toolbar(self) -> None:
        if not self.query("#session-toolbar-meta"):
            return
        separator = " / " if self.ascii_only else " · "
        toolbar = self.query_one("#session-toolbar-meta", Static)
        filter_label = self.quick_filter.capitalize()
        shown = len(self._option_sessions)
        total = len(self.visible_sessions)
        inventory = f"{shown}/{total} shown" if total and shown < total else f"{total} shown"
        toolbar.update(
            f"Sessions {inventory}{separator}Group: {GROUPING_LABELS[self.grouping]} (g)"
            f"{separator}Density: {self.density.title()} (z)"
            f"{separator}Text: {self.text_scale.title()} (w)"
            f"{separator}Motion: {self.motion_preset} (M)"
            f"{separator}Contrast: {'on' if self.high_contrast else 'off'} (C)"
            f"{separator}Filter: {filter_label} (1-6)"
            f"{separator}Shortcuts: {self._shortcut_rail()}"
        )

    def _shortcut_rail(self) -> str:
        if self.interaction_mode is InteractionMode.SEARCH:
            shortcuts = ("Enter Apply", "Esc Cancel", "Ctrl+U Clear", "f Filter", "? Help")
        elif self.interaction_mode is InteractionMode.PALETTE:
            shortcuts = ("Type Search", "Enter Run", "Esc Back", "j/k Move", "? Help")
        elif self._selected() is None:
            shortcuts = ("c Create", "f Filter", "W Triage", "0 Macro", "? Help")
        else:
            shortcuts = ("Enter Open", "l Logs", "d Manage", "U Undo", "W Triage")
        return " | ".join(shortcuts[:5])

    def _render_header(self) -> None:
        attached = sum(session.runtime is RuntimeState.ATTACHED for session in self.sessions)
        detached = sum(session.runtime is RuntimeState.DETACHED for session in self.sessions)
        stopped = sum(
            session.runtime in {RuntimeState.STOPPED, RuntimeState.FAILED}
            for session in self.sessions
        )
        doing = sum(session.task_state is TaskState.IN_PROGRESS for session in self.sessions)
        done = sum(session.task_state is TaskState.COMPLETED for session in self.sessions)
        todo = max(0, len(self.sessions) - doing - done)
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
        latency_ms = round(self._refresh_latency_ema_ms) if self._refresh_latency_ema_ms else 0
        render_ms = round(self._render_stats_ms) if self._render_stats_ms else 0
        capability = self._terminal_capability_badge()
        perf_text = (
            f"perf {self._effective_profile}"
            f" {latency_ms}ms"
            f" {self._render_stats_rows}rows/{render_ms}ms"
            f" @{self._effective_refresh_interval:.1f}s"
            f" motion:{self.motion}"
            f" cap:{capability}"
        )
        separator = " | " if self.ascii_only else " • "
        counts = separator.join(
            (
                f"{len(self.sessions)} {session_label}",
                f"{attached} attached",
                f"{detached} detached",
                f"{stopped} stopped",
                f"todo {todo}",
                f"doing {doing}",
                f"done {done}",
                warning_text,
                perf_text,
            )
        )
        active_filters = (
            ["Attention"] if self.has_class("attention-view") else self.filters.labels()
        )
        if self.quick_filter != "all":
            active_filters.insert(0, f"Quick {self.quick_filter}")
        if self.filter_query:
            active_filters.insert(0, f'Search "{self.filter_query}"')
        filter_text = f"{separator}{', '.join(active_filters)}" if active_filters else ""
        connection = "tmux connected" if self.tmux_connected else "tmux unavailable"
        if self.has_class("refreshing"):
            spinner = self._spinner_glyph() if self.motion != "off" else "..."
            connection = f"Refreshing {spinner}"
        elif self.last_refreshed_at is not None:
            age = int((self._now_utc() - self.last_refreshed_at).total_seconds())
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

    def _terminal_capability_badge(self) -> str:
        features: list[str] = []
        features.append("ascii" if self.ascii_only else "unicode")
        features.append("mono" if self.monochrome else "color")
        if self._base_motion == "off":
            features.append("motion-off")
        elif self.motion == "off":
            features.append("motion-safe")
        else:
            features.append("motion-on")
        return "/".join(features)

    def _render_action_bar(self) -> None:
        navigation = "Up/Down/jk" if self.ascii_only else "↑↓/jk"
        default_tool = self._default_create_tool()
        create_hint = (
            f"c Create({CREATE_TOOL_SHORT_LABELS[default_tool]})"
            if default_tool is not None
            else "c Create"
        )
        concise = self.hint_level == "minimal"
        if self.has_class("searching"):
            query = self.query_one("#search", Input).value
            value = (
                f"SEARCH  {query}_   Enter apply   Esc cancel"
                if concise
                else f"SEARCH  {query}_   Enter Apply   Esc Cancel   Ctrl+U Clear"
            )
        elif self.has_class("attention-view"):
            value = (
                f"ATTENTION  {navigation} Nav  Enter Open  Esc Back  ? shortcuts"
                if concise
                else (
                    f"ATTENTION  {navigation} Nav   Enter Open   Esc Back   "
                    "f Filter   r Refresh   ? Help   q Quit"
                )
            )
        elif self.narrow_detail_open:
            selected = self._selected()
            primary = (
                "Enter Manage"
                if selected is not None and selected.runtime is RuntimeState.STOPPED
                else "Enter Attach"
            )
            value = (
                f"DETAIL  Esc Back  {primary}  l Logs  d Manage  ? shortcuts"
                if concise
                else f"Esc Back  {primary}  e Edit  n Task  l Logs  r Reload  * Pin  d Manage"
            )
        elif self.has_class("narrow"):
            value = (
                f"{navigation} Nav  Enter Open  {create_hint}  f Filter  h Health  ? shortcuts"
                if concise
                else (
                    f"{navigation} Nav   Enter Open   {create_hint}   "
                    "Space Mark   Ctrl+A Mark all   Ctrl+U Clear marks   b Bulk"
                    "   1-6 Quick filters   f Filter   g Group   h Health   ? Help"
                )
            )
        elif self.has_class("medium"):
            value = (
                f"{navigation}  Enter Attach  {create_hint}  / Search"
                "  f Filter  d Manage  ? shortcuts"
                if concise
                else (
                    f"{navigation} Nav   Enter Attach   {create_hint}   o Presets   "
                    "/ Search   Space Mark   Ctrl+A Mark all   Ctrl+U Clear marks   b Bulk"
                    "   1-6 Quick filters   f Filter   "
                    "g Group   h Health   d Manage   ? Help"
                )
            )
        else:
            value = (
                f"{navigation}  Enter Attach  {create_hint}  / Search"
                "  p Palette  d Manage  ? shortcuts"
                if concise
                else (
                    f"{navigation} Navigate   Enter Attach   {create_hint}"
                    "   o Presets   / Search   "
                    "Space Mark   Ctrl+A Mark all   Ctrl+U Clear   Alt+I Invert   b Bulk"
                    "   1-6 Quick filters   f Filter   g Group   z Density   w Text"
                    "   h Health   p Palette   d Manage   i Timeline   ? Help"
                )
            )
        if self._shortcut_pulse:
            value = f"{value}\nTip: {self._shortcut_pulse}"
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
        if (
            session.name == self.selected_name
            and session.session_id == self.selected_session_id
            and self._detail_refreshing
        ):
            return
        self.selected_name = session.name
        self.selected_session_id = session.session_id
        self._apply_project_visual_profile(session.project)
        self._render_details(session.name)

    @on(OptionList.OptionHighlighted, "#sessions")
    def option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        if self._expected_option_id is not None:
            if event.option.id != self._expected_option_id:
                return
            self._expected_option_id = None
        if not self._rendering_options and event.option_index == event.option_list.highlighted:
            animate_focus_pulse(event.option_list, motion=self.motion)
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
        disabled_reason = self.query_one("#action-disabled-reason", Static)
        disabled_reason.update("")
        if enabled:
            self.query_one("#action-pin", Button).label = "Unpin" if pinned else "Pin"
            self.query_one("#action-resume", Button).disabled = not resumable
            self.query_one("#action-open", Button).disabled = resumable
            self.query_one("#action-stop", Button).disabled = resumable
            if resumable:
                disabled_reason.update("Attach disabled: session is stopped. Use Resume or Manage.")

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
                else (
                    "Why empty: no warning or blocked sessions match the attention view.\n"
                    "Primary action: press f to inspect all sessions.\n"
                    "Secondary shortcut: press r to refresh signal scans."
                )
            )
            for widget_id in ("#runtime-status", "#activity", "#recent-output"):
                self.query_one(widget_id, Static).update("")
            self.query_one("#output-meta", Static).update(
                "Primary: Esc restore dashboard\nSecondary: f open filters"
            )
            self._set_action_buttons_enabled(False)
            return
        filtered = bool(self.filter_query) or self.filters.active
        if not self.tmux_connected:
            self.query_one("#identity", Static).update("Runtime disconnected")
            self.query_one("#overview", Static).update(
                "Why empty: ws cannot read tmux sessions right now.\n"
                "Primary action: press h to open health details.\n"
                "Secondary shortcut: press r to retry connection."
            )
            for widget_id in ("#runtime-status", "#activity", "#recent-output"):
                self.query_one(widget_id, Static).update("")
            self.query_one("#output-meta", Static).update(
                "Recovery: ws doctor --actionable | ws health --actionable"
            )
            self._set_action_buttons_enabled(False)
            return
        self.query_one("#identity", Static).update("No matches" if filtered else "No sessions")
        self.query_one("#overview", Static).update(
            "Why empty: active query/filter excludes all managed sessions.\n"
            "Primary action: press Esc to clear search or press f to revise filters.\n"
            "Secondary shortcut: press 1 for All sessions."
            if filtered
            else (
                "Why empty: no managed sessions exist yet.\n"
                "Primary action: press c to create a session.\n"
                "Secondary shortcut: press o to launch from a preset."
            )
        )
        for widget_id in ("#runtime-status", "#activity", "#recent-output"):
            self.query_one(widget_id, Static).update("")
        self.query_one("#output-meta", Static).update(
            "Primary: c create session\nSecondary: o presets   p palette"
        )
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
            self.query_one("#recent-output", Static).update("Unable to load output.")
            self.query_one("#output-meta", Static).update("Error  l open full logs")
            self._set_action_buttons_enabled(False)
            return
        assert details is not None
        session = details.session
        stored_viewport = self._detail_viewports.get((session.name, session.session_id))
        if (
            self.narrow_detail_open
            and stored_viewport is not None
            and old_scroll == 0
            and old_output_scroll == 0
        ):
            old_scroll, old_output_scroll = stored_viewport
        notice = detect_activity(session, details.preview)
        self._store_notice(session, notice, observed_at=datetime.now(UTC))
        identity = (session.name, session.session_id)
        self._record_activity_sample(identity, details.preview)
        self._last_preview_truncated[identity] = details.preview_truncated
        if not self._attention_scanning and self._attention_complete():
            self._attention_baseline_established = True
            self._stop_attention_dots()
        separator = " / " if self.ascii_only else " · "
        identity = Text()
        identity.append(
            f"{session.tool.value.upper():<7}",
            style=tool_style(session.tool, monochrome=self.monochrome),
        )
        identity_name = condensed_session_label(session)
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
                    "Last active "
                    f"{relative_activity(session.last_active_at, now=self._now_utc())} ago",
                )
            ),
            runtime_style(session.runtime, monochrome=self.monochrome),
        )
        self.query_one("#identity", Static).update(identity)
        overview_values = [
            ("Display name", condensed_session_label(session)),
            ("Full session ID", session.name),
            ("Tool", TOOL_LABELS[session.tool]),
            ("Task", humanize_task(session.note)),
            ("Project", session.project),
            ("Directory", display_path(session.cwd)),
            ("Owner", "Managed by ws" if session.owned else "Read only"),
        ]
        if session.tags:
            overview_values.append(("Tags", ", ".join(session.tags)))
        status_values = [
            ("Runtime", display_state(session.runtime.value)),
            ("Task", display_state(session.task_state.value)),
            ("Agent", display_state(notice.agent_state.value)),
            ("Input", display_input(session.input_state)),
            ("Windows", str(session.windows)),
            ("Logging", "Enabled" if session.logging_enabled else "Disabled"),
            ("Last active", relative_activity(session.last_active_at, now=self._now_utc())),
        ]
        if self.has_class("medium"):
            overview_values = overview_values[:1]
            status_values = status_values[:4]
        self.query_one("#overview", Static).update(labeled_values(overview_values))
        status_text = status_chip_line(
            session,
            notice,
            ascii_only=self.ascii_only,
            monochrome=self.monochrome,
        )
        status_text.append_text(labeled_values(status_values))
        self.query_one("#runtime-status", Static).update(status_text)
        activity = Text("No action required.", style="bold")
        activity.append("\nSession can continue normally.")
        if notice.warning:
            activity = Text(notice.title, style="bold")
            activity.append(f"\n{notice.detail}")
        recent_events = self.service.timeline(session.name, limit=3)
        if recent_events:
            activity.append("\n\nTimeline")
            for event in recent_events:
                activity.append(
                    f"\n- {event.timestamp.astimezone().strftime('%H:%M')} {event.action}"
                    + (f": {event.detail}" if event.detail else ""),
                    "dim",
                )
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
        self._render_output_preview(
            details.preview, notice, preview_truncated=details.preview_truncated
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

    def _set_output_mode(self, mode: str) -> None:
        self.output_mode = "raw" if mode == "raw" else "summary"
        for mode in ("summary", "raw"):
            self.query_one(f"#output-{mode}", Button).set_class(self.output_mode == mode, "active")
        selected = self._selected()
        identity: tuple[str, str] | None = None
        if selected is not None:
            identity = (selected.name, selected.session_id)
        elif self.selected_name is not None and self.selected_session_id is not None:
            identity = (self.selected_name, self.selected_session_id)
        if identity is not None:
            fallback_notice = (
                detect_activity(selected, "")
                if selected is not None
                else ActivityNotice(
                    "neutral",
                    "No action required",
                    "Session can continue normally.",
                    AgentState.ACTIVE,
                )
            )
            self._render_output_preview(
                self._last_preview.get(identity, ""),
                self._alerts.get(identity, fallback_notice),
                preview_truncated=self._last_preview_truncated.get(identity, False),
            )
        if self.selected_name:
            self._render_details(self.selected_name)

    @on(Button.Pressed)
    def micro_button_feedback(self, event: Button.Pressed) -> None:
        if event.button.disabled:
            return
        event.button.add_class("button-feedback-press")
        self.set_timer(0.12, lambda: event.button.remove_class("button-feedback-press"))

    @on(Button.Pressed, ".output-mode")
    def output_mode_changed(self, event: Button.Pressed) -> None:
        self._set_output_mode("raw" if event.button.id == "output-raw" else "summary")

    @on(Button.Pressed, "#output-summary")
    def output_summary_pressed(self) -> None:
        self._set_output_mode("summary")

    @on(Button.Pressed, "#output-raw")
    def output_raw_pressed(self) -> None:
        self._set_output_mode("raw")

    @on(Button.Pressed, ".session-action-button")
    def session_action_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id
        if button_id:
            self._pulse_button_feedback(f"#{button_id}")
        action: Callable[[], None] | None = None
        loading = "Working..."
        if button_id == "action-open":
            action = self.action_open
            loading = "Opening..."
        elif button_id == "action-resume":
            action = self.action_resume_selected
            loading = "Resuming..."
        elif button_id == "action-manage":
            action = self.action_manage
            loading = "Loading..."
        elif button_id == "action-logs":
            action = self.action_logs
            loading = "Opening..."
        elif button_id == "action-pin":
            action = self.action_toggle_pin
            loading = "Saving..."
        elif button_id == "action-stop":
            action = self.action_stop_selected
            loading = "Stopping..."
        if action is not None:
            self._run_with_button_loading(event.button, action, loading_label=loading)

    @on(Button.Pressed, "#toolbar-create")
    def toolbar_create_pressed(self) -> None:
        self._pulse_button_feedback("#toolbar-create")
        self._run_with_button_loading(
            self.query_one("#toolbar-create", Button),
            self.action_create,
            loading_label="Creating...",
        )

    @on(Button.Pressed, "#toolbar-resume")
    def toolbar_resume_pressed(self) -> None:
        self._pulse_button_feedback("#toolbar-resume")
        self._run_with_button_loading(
            self.query_one("#toolbar-resume", Button),
            self.action_resume,
            loading_label="Resuming...",
        )

    @on(Button.Pressed, "#toolbar-filter")
    def toolbar_filter_pressed(self) -> None:
        self._pulse_button_feedback("#toolbar-filter")
        self._run_with_button_loading(
            self.query_one("#toolbar-filter", Button),
            self.action_filter,
            loading_label="Opening...",
        )

    @on(Button.Pressed, "#toolbar-group")
    def toolbar_group_pressed(self) -> None:
        self._pulse_button_feedback("#toolbar-group")
        self._run_with_button_loading(
            self.query_one("#toolbar-group", Button),
            self.action_cycle_grouping,
            loading_label="Switching...",
        )

    @on(Button.Pressed, "#toolbar-health")
    def toolbar_health_pressed(self) -> None:
        self._pulse_button_feedback("#toolbar-health")
        self._run_with_button_loading(
            self.query_one("#toolbar-health", Button),
            self.action_health_alerts,
            loading_label="Opening...",
        )

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
        self._track_action_invocation("filter", "f")
        if self._attention_context is not None:
            self._restore_attention_view(restore_focus=False)
        if self.narrow_detail_open:
            self._close_narrow_detail()
        self._animate_workspace_transition("forward")
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
        self._pulse_shortcut_hint("Filter mode: Tab next, Enter apply, Esc cancel")

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
        self._animate_workspace_transition("forward")
        self._begin_overlay(InteractionMode.FORM)
        self.push_screen(
            SearchOutputScreen(self.service, self.sessions), self._search_output_result
        )
        self._pulse_shortcut_hint("Search output: type query, Up/Down navigate, Esc back")

    def _search_output_result(self, _result: None) -> None:
        self._restore_dashboard_mode()

    def action_command_palette(self) -> None:
        if self.interaction_mode not in {InteractionMode.NORMAL, InteractionMode.SEARCH}:
            return
        self._animate_workspace_transition("forward")
        self._begin_overlay(InteractionMode.PALETTE)
        super().action_command_palette()
        self._pulse_shortcut_hint("Palette: type command, Enter run, Esc back")

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

    def _set_quick_filter(self, value: str) -> None:
        self.quick_filter = value
        self._render_options()
        self._notify_grouped(f"Filter: {value.capitalize()}")

    def action_quick_filter_all(self) -> None:
        self._set_quick_filter("all")

    def action_quick_filter_active(self) -> None:
        self._set_quick_filter("active")

    def action_quick_filter_detached(self) -> None:
        self._set_quick_filter("detached")

    def action_quick_filter_warnings(self) -> None:
        self._set_quick_filter("warnings")

    def action_quick_filter_stopped(self) -> None:
        self._set_quick_filter("stopped")

    def action_quick_filter_blocked(self) -> None:
        self._set_quick_filter("blocked")

    def action_macro_warnings_logs(self) -> None:
        if self.interaction_mode not in {InteractionMode.NORMAL, InteractionMode.SEARCH}:
            return
        self._set_quick_filter("warnings")
        self._pulse_shortcut_hint("Macro: warnings filter applied, opening logs")
        if not self.visible_sessions:
            self._notify_grouped(
                "Macro stopped: no warning sessions available.", severity="warning"
            )
            return
        target = self.visible_sessions[0]
        self.selected_name = target.name
        self.selected_session_id = target.session_id
        self._render_options()
        self.action_logs()

    def action_triage_wizard(self) -> None:
        if self.interaction_mode not in {InteractionMode.NORMAL, InteractionMode.SEARCH}:
            return
        warning_sessions = [
            session for session in self.sessions if is_warning(session, self._notice_for(session))
        ]
        notices = {session.name: self._notice_for(session) for session in warning_sessions}
        self._animate_workspace_transition("forward")
        self._begin_overlay(InteractionMode.FORM)
        self.push_screen(
            TriageWizardScreen(self.service, warning_sessions, notices),
            self._finish_triage_wizard,
        )

    def _finish_triage_wizard(self, action: str | None) -> None:
        self._restore_dashboard_mode()
        if not action:
            return
        action_name, _, target_name = action.partition(":")
        if action_name == "triage-open-health":
            self.call_after_refresh(self.action_health_alerts)
            return
        if action_name == "triage-filter-only":
            self._set_quick_filter("warnings")
            return
        target = next((session for session in self.sessions if session.name == target_name), None)
        if target is None:
            self.notify("Select a warning session first.", severity="warning")
            return
        self.selected_name = target.name
        self.selected_session_id = target.session_id
        self._render_options()
        if action_name == "triage-open-logs":
            self.call_after_refresh(self.action_logs)
        elif action_name == "triage-open-manage":
            self.call_after_refresh(self.action_manage)

    def _set_progress_state(self, state: TaskState, label: str) -> None:
        session = self._selected()
        if session is None:
            return
        try:
            updated = self.service.organize(session.name, state=state)
        except WsError as error:
            self.notify(str(error), severity="warning")
            return
        self.selected_name = updated.name
        self.selected_session_id = updated.session_id
        self.refresh_sessions()
        self.notify(f"Progress set: {label}")
        self._announce_undo_hint()

    def action_progress_todo(self) -> None:
        self._set_progress_state(TaskState.WAITING, "todo")

    def action_progress_doing(self) -> None:
        self._set_progress_state(TaskState.IN_PROGRESS, "doing")

    def action_progress_done(self) -> None:
        self._set_progress_state(TaskState.COMPLETED, "done")

    def action_toggle_bulk_select(self) -> None:
        session = self._selected()
        if session is None:
            return
        identity = (session.name, session.session_id)
        if identity in self._bulk_selected:
            self._bulk_selected.remove(identity)
        else:
            self._bulk_selected.add(identity)
        self._render_options()
        self._notify_grouped(f"Bulk selected: {len(self._bulk_selected)}")

    def action_bulk_select_visible(self) -> None:
        if self.interaction_mode not in {InteractionMode.NORMAL, InteractionMode.SEARCH}:
            return
        if not self.visible_sessions:
            return
        for session in self.visible_sessions:
            self._bulk_selected.add((session.name, session.session_id))
        self._render_options()
        self._notify_grouped(f"Marked {len(self._bulk_selected)} sessions")

    def action_bulk_clear_selection(self) -> None:
        if self.interaction_mode not in {InteractionMode.NORMAL, InteractionMode.SEARCH}:
            return
        if not self._bulk_selected:
            return
        self._bulk_selected.clear()
        self._render_options()
        self._notify_grouped("Cleared bulk marks")

    def action_bulk_invert_visible(self) -> None:
        if self.interaction_mode not in {InteractionMode.NORMAL, InteractionMode.SEARCH}:
            return
        if not self.visible_sessions:
            return
        visible = {(session.name, session.session_id) for session in self.visible_sessions}
        current = self._bulk_selected & visible
        self._bulk_selected -= current
        self._bulk_selected |= visible - current
        self._render_options()
        self._notify_grouped(f"Bulk selected: {len(self._bulk_selected)}")

    def action_bulk_actions(self) -> None:
        if self.interaction_mode not in {InteractionMode.NORMAL, InteractionMode.SEARCH}:
            return
        if not self._bulk_selected:
            self.notify("No sessions marked. Press Space to mark rows.", severity="warning")
            return
        self._begin_overlay(InteractionMode.MANAGE)
        self.push_screen(BulkActionScreen(len(self._bulk_selected)), self._apply_bulk_action)

    def _apply_bulk_action(self, action_id: str | None) -> None:
        self._restore_dashboard_mode()
        if not action_id:
            return
        selected = list(self._bulk_selected)
        if not selected:
            return
        failures = 0
        skipped = 0
        for name, session_id in selected:
            session = next(
                (
                    item
                    for item in self.sessions
                    if item.name == name and item.session_id == session_id
                ),
                None,
            )
            if session is None:
                continue

            def _run(current_session: SessionView = session) -> None:
                if action_id == "bulk-pin":
                    self.service.organize(current_session.name, pinned=True)
                elif action_id == "bulk-unpin":
                    self.service.organize(current_session.name, pinned=False)
                elif action_id == "bulk-logging-on":
                    self.service.set_logging(current_session.name, True)
                elif action_id == "bulk-logging-off":
                    self.service.set_logging(current_session.name, False)
                elif action_id == "bulk-stop-command":
                    self.service.stop_command(current_session.name)
                elif action_id == "bulk-handoff":
                    details = self.service.inspect(current_session.name)
                    preview_tail = (
                        details.preview.splitlines()[-1] if details.preview else "no output"
                    )
                    self.notify(
                        f"{current_session.name}: {preview_tail}",
                        timeout=4,
                    )

            try:
                if not self._run_session_locked(session.name, "bulk action", _run):
                    skipped += 1
            except (WsError, OSError):
                failures += 1
        if action_id != "bulk-handoff":
            applied = len(selected) - failures - skipped
            self._notify_success(
                f"Bulk action applied to {applied} session(s)"
                + (f", {failures} failed" if failures else "")
                + (f", {skipped} skipped" if skipped else "")
            )
            self._record_job(
                f"bulk {action_id.removeprefix('bulk-')} on {applied} session(s)",
                severity="success" if failures == 0 else "error",
            )
        self._bulk_selected.clear()
        self.refresh_sessions()

    def action_apply_filter_preset(self, preset_name: str) -> None:
        try:
            preset = self.service.get_filter_preset(preset_name)
        except WsError as error:
            self.notify(str(error), severity="warning")
            return
        self.quick_filter = preset.quick_filter
        self.filter_query = preset.query
        self.filters = FilterState(
            tool=preset.tool,
            runtime=preset.runtime,
            task=preset.task,
            tag=preset.tag,
            project=preset.project,
            warnings_only=preset.warnings_only,
            recent_only=preset.recent_only,
        )
        if preset.grouping in GROUPING_MODES:
            self.grouping = preset.grouping
        if preset.density in {"compact", "comfortable"}:
            self.density = preset.density
            self.set_class(self.density == "compact", "compact-density")
        self._persist_interface_preferences()
        with self.prevent(Input.Changed):
            self.query_one("#search", Input).value = self.filter_query
        self._render_options()
        self._notify_grouped(f"Applied filter preset: {preset.name}")

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
        self._apply_visual_mode_classes()
        self._set_layout_classes(self.size.width, self.size.height)
        self._render_health_row()
        self._render_options()
        self._persist_interface_preferences()
        self.notify(
            f"Theme: {self.ui_theme} (accent: {self.accent_mode}, "
            f"contrast: {'on' if self.high_contrast else 'off'})"
        )

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

    def _default_create_tool(self) -> Tool | None:
        return default_enabled_tool(self.service.config)

    def action_create(self, tool: Tool | None = None) -> None:
        if self.interaction_mode not in {InteractionMode.NORMAL, InteractionMode.SEARCH}:
            return
        self._track_action_invocation("create", "c")
        selected_tool = tool or self._default_create_tool()
        if selected_tool is None:
            self.notify("All tool profiles are disabled in config.toml.", severity="warning")
            return
        profile = self.service.config.tools.get(selected_tool)
        if profile is None or not profile.enabled:
            self.notify(
                f"{TOOL_LABELS[selected_tool]} is disabled in config.toml.",
                severity="warning",
            )
            return
        self._animate_workspace_transition("forward")
        self._begin_overlay(InteractionMode.FORM)
        self.push_screen(
            CreateSessionScreen(
                self.default_cwd,
                self.service,
                selected_tool,
                draft=self._load_form_draft("create"),
            ),
            self._create_session,
        )
        self._pulse_shortcut_hint("Create form: Ctrl+Enter submit, Esc cancel")

    def action_new_session(self) -> None:
        self.action_create()

    def action_shell(self) -> None:
        self.action_create(Tool.SHELL)

    def action_preset_launcher(self) -> None:
        if self.interaction_mode not in {InteractionMode.NORMAL, InteractionMode.SEARCH}:
            return
        presets = self.service.list_presets()
        if not presets:
            self.notify("No presets saved yet. Use 'ws preset save' first.", severity="warning")
            return
        self._animate_workspace_transition("forward")
        self._begin_overlay(InteractionMode.FORM)
        self.push_screen(
            PresetLauncherScreen(presets),
            self._preset_launcher_selected,
        )

    def action_create_from_preset(self, preset_name: str) -> None:
        if self.interaction_mode not in {InteractionMode.NORMAL, InteractionMode.SEARCH}:
            return
        try:
            preset = self.service.get_preset(preset_name)
        except WsError as error:
            self.notify(str(error), severity="warning")
            return
        self._animate_workspace_transition("forward")
        self._begin_overlay(InteractionMode.FORM)
        self.push_screen(
            CreateSessionScreen(
                self.default_cwd,
                self.service,
                preset.tool,
                template=preset,
                draft=self._load_form_draft("create"),
            ),
            self._create_session,
        )
        self._pulse_shortcut_hint("Create from preset: review fields then Ctrl+Enter")

    def _preset_launcher_selected(self, preset_name: str | None) -> None:
        if preset_name is None:
            self._restore_dashboard_mode()
            return
        if preset_name == "":
            selected_tool = self._default_create_tool()
            if selected_tool is None:
                self._restore_dashboard_mode()
                self.notify("All tool profiles are disabled in config.toml.", severity="warning")
                return
            self.push_screen(
                CreateSessionScreen(self.default_cwd, self.service, selected_tool),
                self._create_session,
            )
            return
        try:
            preset = self.service.get_preset(preset_name)
        except WsError as error:
            self._restore_dashboard_mode()
            self.notify(str(error), severity="warning")
            return
        self.push_screen(
            CreateSessionScreen(
                self.default_cwd,
                self.service,
                preset.tool,
                template=preset,
            ),
            self._create_session,
        )

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
        self._animate_workspace_transition("forward")
        self._begin_overlay(InteractionMode.MANAGE)
        self._open_manage_confirmation(session, "restart", ManageListState())

    def action_stop_selected(self) -> None:
        session = self._selected()
        if session is None:
            return
        if session.runtime in {RuntimeState.STOPPED, RuntimeState.FAILED}:
            self.notify("Session is already stopped.", severity="information")
            return
        self._animate_workspace_transition("forward")
        self._begin_overlay(InteractionMode.MANAGE)
        self._open_manage_confirmation(session, "stop-command", ManageListState())

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

    def action_cycle_text_scale(self) -> None:
        current = TEXT_SCALE_MODES.index(self.text_scale)
        self.text_scale = TEXT_SCALE_MODES[(current + 1) % len(TEXT_SCALE_MODES)]
        self._apply_text_scale_class()
        self._persist_interface_preferences()
        self.notify(f"Text scale: {self.text_scale}")

    def action_cycle_layout_preset(self) -> None:
        current = (self.density, self.text_scale)
        try:
            index = LAYOUT_PRESETS.index(current)
        except ValueError:
            index = 0
        self.density, self.text_scale = LAYOUT_PRESETS[(index + 1) % len(LAYOUT_PRESETS)]
        self.set_class(self.density == "compact", "compact-density")
        self._apply_text_scale_class()
        self._persist_interface_preferences()
        self._render_options()
        self.notify(f"Layout preset: density={self.density}, text={self.text_scale}")

    def action_cycle_motion_preset(self) -> None:
        if self._base_motion == "off":
            self.notify("Motion is disabled by accessibility/snapshot/terminal constraints.")
            return
        current = MOTION_PRESET_MODES.index(self.motion_preset)
        self.motion_preset = MOTION_PRESET_MODES[(current + 1) % len(MOTION_PRESET_MODES)]
        self.motion = self._effective_motion(auto_safe=self._auto_safe_mode)
        self.set_class(self.motion == "off", "motion-off")
        self._persist_interface_preferences()
        self.notify(f"Motion preset: {self.motion_preset} (effective: {self.motion})")

    def action_toggle_high_contrast(self) -> None:
        self.high_contrast = not self.high_contrast
        self._apply_visual_mode_classes()
        self._persist_interface_preferences()
        self.notify(f"High contrast: {'on' if self.high_contrast else 'off'}")

    def action_cycle_accent_mode(self) -> None:
        current = ACCENT_MODES.index(self.accent_mode)
        self.accent_mode = ACCENT_MODES[(current + 1) % len(ACCENT_MODES)]
        self._apply_visual_mode_classes()
        self._persist_interface_preferences()
        self.notify(f"Accent style: {self.accent_mode}")

    def action_toggle_hint_level(self) -> None:
        self.hint_level = "verbose" if self.hint_level == "minimal" else "minimal"
        self.hint_profile = "beginner" if self.hint_level == "verbose" else "advanced"
        self._persist_interface_preferences()
        self._render_action_bar()
        self.notify(f"Hint detail: {self.hint_level}")

    def action_toggle_create_advanced_default(self) -> None:
        self.create_advanced_by_default = not self.create_advanced_by_default
        self._persist_interface_preferences()
        self.notify(
            "Create advanced default: " + ("on" if self.create_advanced_by_default else "off")
        )

    def action_toggle_auto_contrast(self) -> None:
        self.auto_contrast = not self.auto_contrast
        self._refresh_auto_contrast()
        self._persist_interface_preferences()
        self.notify(f"Auto contrast: {'on' if self.auto_contrast else 'off'}")

    def action_cycle_hint_profile(self) -> None:
        current = HINT_PROFILES.index(self.hint_profile)
        self.hint_profile = HINT_PROFILES[(current + 1) % len(HINT_PROFILES)]
        self.hint_level = "verbose" if self.hint_profile == "beginner" else "minimal"
        self._persist_interface_preferences()
        self._render_action_bar()
        self.notify(f"Hint profile: {self.hint_profile}")

    def action_palette_pin_defaults(self) -> None:
        self._palette_pinned |= {
            normalize_palette_key("Dashboard · Search sessions"),
            normalize_palette_key("Dashboard · Filter sessions"),
            normalize_palette_key("Create · Session"),
            normalize_palette_key("Interface · Open controls panel"),
            normalize_palette_key("Interface · Switch theme"),
            normalize_palette_key("Selected · Open logs"),
            normalize_palette_key("Interface · Cycle layout preset"),
        }
        self.notify("Pinned common command palette actions")

    def action_palette_unpin_defaults(self) -> None:
        self._palette_pinned.clear()
        self.notify("Cleared pinned command palette actions")

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
            compact=self.density == "compact" or self.has_class("narrow"),
            pulse_active=self._marker_pulse_phase,
            now=self._now_utc(),
        )
        prompt.stylize(style)
        self.query_one("#sessions", OptionList).replace_option_prompt(option_id, prompt)

    def _create_session(self, result: CreateFormResult | None) -> None:
        if result is None:
            self._restore_create_mode()
            return
        self._attempt_create(result)

    def _attempt_create(self, result: CreateFormResult) -> None:
        session: SessionView | None = None
        lock_name = result.request.name
        with contextlib.suppress(WsError):
            lock_name = normalized_session_name(
                result.request.tool,
                result.request.name,
                automatic_prefix=result.request.automatic_prefix,
            )
        try:

            def _create() -> None:
                nonlocal session
                session = self.service.create(result.request)

            if not self._run_session_locked(lock_name, "create session", _create):
                return
        except WsError as error:
            self._record_job("create session failed", severity="error")
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
        if session is None:
            return
        self._failed_create_result = None
        self._restore_create_mode()
        self._insert_created_session(session)
        self._record_job(f"created {session.name}", severity="success")
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
                "Run System Diagnostics, verify the configured executable, and retry creation.\n\n"
                "Recovery commands:\n"
                "ws doctor --actionable\n"
                "ws health --actionable\n"
                "ws setup",
            ),
            lambda _result: self._restore_dashboard_mode(),
        )

    def action_edit(self) -> None:
        session = self._selected()
        if session:
            self._begin_overlay(InteractionMode.FORM)
            self.push_screen(
                IdentityOrganizationScreen(
                    self.service,
                    session,
                    draft=self._load_form_draft(f"identity:{session.name}:{session.session_id}"),
                ),
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
                NoteScreen(
                    session,
                    draft=self._load_form_draft(f"note:{session.name}:{session.session_id}"),
                ),
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
        self._announce_undo_hint()
        self.refresh_sessions()
        self._return_to_manage(updated, manage_state)

    def action_logs(self) -> None:
        session = self._selected()
        if session:
            self._track_action_invocation("logs", "l")
            self._animate_workspace_transition("forward")
            self._begin_overlay(InteractionMode.FORM)
            self.push_screen(LogScreen(self.service, session), self._logs_result)

    def _logs_result(self, target: str | None) -> None:
        self._restore_dashboard_mode()
        if target:
            self.exit(target)

    def action_timeline(self) -> None:
        session = self._selected()
        if session is None:
            return
        events = self.service.timeline(session.name, limit=60)
        if not events:
            content = "No timeline events recorded yet."
        else:
            lines = [
                f"{event.timestamp.astimezone().strftime('%Y-%m-%d %H:%M:%S')}  "
                f"{event.action}  {event.detail}".rstrip()
                for event in events
            ]
            content = "\n".join(lines)
        self._begin_overlay(InteractionMode.FORM)
        self.push_screen(
            MessageScreen(
                f"Timeline · {session.display_name or session.name}",
                content + f"\n\nHint: ws timeline {session.name}",
            ),
            lambda _result: self._restore_dashboard_mode(),
        )

    def action_toggle_pin(self) -> None:
        session = self._selected()
        if session is None:
            return
        self._track_action_invocation("pin", "*")
        self._toggle_pin(session)

    def action_undo_last_action(self) -> None:
        if self.interaction_mode not in {InteractionMode.NORMAL, InteractionMode.SEARCH}:
            return
        try:
            result = self.service.undo_last()
        except WsError as error:
            self.notify(str(error), title="Undo unavailable", severity="warning")
            return
        self._notify_success(f"Undo applied: {result}")
        self.refresh_sessions()

    def _toggle_pin(self, session: SessionView) -> None:
        try:
            updated = self.service.organize(session.name, pinned=not session.pinned)
        except WsError as error:
            self.notify(str(error), severity="warning")
            return
        self.selected_name = updated.name
        self.selected_session_id = updated.session_id
        if self.query("#action-pin"):
            self.query_one("#action-pin", Button).label = "Unpin" if updated.pinned else "Pin"
        self._notify_success("Session unpinned" if session.pinned else "Session pinned")
        self._announce_undo_hint()
        self.refresh_sessions()

    def action_manage(self) -> None:
        session = self._selected()
        if session:
            self._track_action_invocation("manage", "d")
            self._animate_workspace_transition("forward")
            self._begin_overlay(InteractionMode.MANAGE)
            self._open_manage_screen(session)
            self._pulse_shortcut_hint("Manage mode: j/k navigate, Enter select, Esc back")

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
        current = next(
            (
                session
                for session in self.sessions
                if session.name == target.name and session.session_id == target.session_id
            ),
            None,
        )
        if current is not None:
            return current
        with contextlib.suppress(WsError):
            return self.service.get(target.name)
        return None

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
            self._announce_undo_hint()
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
            self._announce_undo_hint()
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
            IdentityOrganizationScreen(
                self.service,
                current,
                draft=self._load_form_draft(f"identity:{current.name}:{current.session_id}"),
            ),
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
            NoteScreen(
                current,
                draft=self._load_form_draft(f"note:{current.name}:{current.session_id}"),
            ),
            lambda note, target=current, context=state: self._save_note(note, target, context),
        )

    def _open_status_screen(self, session: SessionView, state: ManageListState) -> None:
        current = self._current_session(session)
        if current is None:
            self._restore_dashboard_mode()
            return
        self.push_screen(
            StatusScreen(
                current,
                draft=self._load_form_draft(f"status:{current.name}:{current.session_id}"),
            ),
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
            CreateSessionScreen(
                self.default_cwd,
                self.service,
                current.tool,
                template=current,
                draft=self._load_form_draft("create"),
            ),
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
                "restart-attach": (
                    "Restart and Attach",
                    "The tool will restart and ws will immediately attach to the session.",
                    "Restart + Attach",
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
            risk_level = self._confirmation_risk_level(current, action)
            impact = self._confirmation_impact(current, action)
            screen = ConfirmActionScreen(
                title,
                current.name,
                consequence,
                confirm_label=confirm_label,
                require_exact_name=action in {"stop-session", "remove-metadata"}
                or risk_level in {"high", "critical"},
                risk_level=risk_level,
                impact_summary=impact,
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

    def _confirmation_risk_level(
        self, session: SessionView, action: str
    ) -> Literal["medium", "high", "critical"]:
        if action == "remove-metadata":
            return "critical"
        if action in {"stop-session", "delete-logs"}:
            if session.runtime is RuntimeState.ATTACHED:
                return "high"
            return "medium"
        if action == "stop-command":
            return "high" if session.runtime is RuntimeState.ATTACHED else "medium"
        if action in {"restart", "restart-attach"}:
            return "high" if session.runtime is RuntimeState.ATTACHED else "medium"
        return "high"

    def _confirmation_impact(self, session: SessionView, action: str) -> str:
        if action == "stop-command":
            return (
                "Interactive command output may be cut mid-step."
                if session.runtime is RuntimeState.ATTACHED
                else "Background command execution will be interrupted."
            )
        if action in {"restart", "restart-attach"}:
            return (
                "Attached workflow context will reset and restart immediately."
                if session.runtime is RuntimeState.ATTACHED
                else "Detached runtime process will be replaced by a fresh command."
            )
        if action == "stop-session":
            return (
                "Attached operators will be disconnected and runtime will stop now."
                if session.runtime is RuntimeState.ATTACHED
                else "Runtime will stop; metadata remains available for recovery."
            )
        if action == "remove-metadata":
            return "Session disappears from managed views until metadata is restored."
        if action == "delete-logs":
            return "Persisted forensic output is permanently removed."
        return "Review impact before continuing."

    def _manage_state_snapshot(self, name: str) -> dict[str, str]:
        with contextlib.suppress(WsError):
            current = self.service.get(name)
            return {
                "runtime": current.runtime.value,
                "task": current.task_state.value,
                "input": current.input_state.value,
                "logging": "on" if current.logging_enabled else "off",
                "project": current.project or "-",
                "session_id": current.session_id,
            }
        return {}

    def _record_manage_success_snapshot(self, name: str) -> None:
        snapshot = self._manage_state_snapshot(name)
        if snapshot:
            self._manage_success_snapshots[name] = snapshot

    def _show_manage_failure_diff(self, action: str, name: str, error: Exception) -> None:
        baseline = self._manage_success_snapshots.get(name, {})
        current = self._manage_state_snapshot(name)
        lines = [str(error), ""]
        if baseline and current:
            lines.append("Failure diff since last success:")
            keys = ("runtime", "task", "input", "logging", "project", "session_id")
            for key in keys:
                before = baseline.get(key, "-")
                after = current.get(key, "-")
                if before != after:
                    lines.append(f"- {key}: {before} -> {after}")
            if len(lines) == 3:
                lines.append("- no state drift detected")
        elif current:
            lines.append("Current state snapshot:")
            lines.extend(f"- {key}: {value}" for key, value in current.items())
        self._begin_overlay(InteractionMode.FORM)
        self.push_screen(
            MessageScreen(f"Failure diff · {display_state(action)}", "\n".join(lines).strip()),
            lambda _result: self._restore_dashboard_mode(),
        )

    def _confirmed_manage(self, action: str, name: str) -> None:
        message = ""
        updated_name: str | None = self.selected_name
        updated_id: str | None = self.selected_session_id
        self._record_manage_success_snapshot(name)

        def _run() -> None:
            nonlocal message, updated_name, updated_id
            if action == "restart":
                updated = self.service.restart(name)
                updated_name = updated.name
                updated_id = updated.session_id
                message = "Session restarted"
            elif action == "restart-attach":
                updated = self.service.restart(name)
                self.exit(updated.name)
            elif action == "stop-command":
                self.service.stop_command(name)
                message = "Interrupt sent"
            elif action == "stop-session":
                updated = self.service.stop_session(name)
                updated_name = updated.name
                updated_id = updated.session_id
                message = "tmux session stopped; metadata retained"
            elif action == "remove-metadata":
                self.service.remove_metadata(name)
                updated_name = None
                updated_id = None
                message = "ws metadata removed"
            elif action == "delete-logs":
                self.service.delete_logs(name)
                message = "Sanitized logs deleted"

        try:
            if action not in {
                "restart",
                "restart-attach",
                "stop-command",
                "stop-session",
                "remove-metadata",
                "delete-logs",
            }:
                return
            if not self._run_session_locked(name, action, _run):
                return
        except (WsError, OSError) as error:
            self._record_job(f"{action} failed for {name}", severity="error")
            self.notify(str(error), title=f"{display_state(action)} failed", severity="error")
            self._show_manage_failure_diff(action, name, error)
            return
        if action == "restart-attach":
            return
        self.selected_name = updated_name
        self.selected_session_id = updated_id
        self._record_job(f"{action} completed for {name}", severity="success")
        self._notify_success(message)
        self._record_manage_success_snapshot(name)
        if action in {"stop-command", "stop-session", "delete-logs"}:
            self._announce_undo_hint()
        self.refresh_sessions()

    def action_advanced_details(self) -> None:
        session = self._selected()
        if session:
            self._begin_overlay(InteractionMode.FORM)
            self._open_advanced_screen(session)

    def _delete_session(self, session: SessionView) -> None:
        try:
            if not self._run_session_locked(
                session.name,
                "delete session",
                lambda: self.service.delete(session.name),
            ):
                return
        except (WsError, OSError) as error:
            self._record_job(f"delete failed for {session.name}", severity="error")
            self.notify(str(error), title="Delete failed", severity="error")
            return
        self.selected_name = None
        self.selected_session_id = None
        self._record_job(f"deleted {session.name}", severity="success")
        self._notify_success(f"Deleted {session.name}")
        self.refresh_sessions()

    def action_diagnostics(self) -> None:
        self._begin_overlay(InteractionMode.FORM)
        self.push_screen(
            DiagnosticsScreen(self.service),
            lambda _result: self._restore_dashboard_mode(),
        )

    def action_export_ops_snapshot(self) -> None:
        sessions = self.service.list_sessions()
        health = self.service.cached_health_alerts()
        generated = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
        destination = self.service.paths.diagnostics_dir / f"ops-report-{generated}.txt"
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        warnings = [
            check for check in health if check.status in {HealthStatus.WARN, HealthStatus.FAIL}
        ]
        attached_count = sum(item.runtime is RuntimeState.ATTACHED for item in sessions)
        detached_count = sum(item.runtime is RuntimeState.DETACHED for item in sessions)
        stopped_count = sum(
            item.runtime in {RuntimeState.STOPPED, RuntimeState.FAILED} for item in sessions
        )
        payload = [
            "WORKSPACE OPERATIONS REPORT",
            f"Generated: {datetime.now().astimezone().isoformat(timespec='seconds')}",
            "",
            (
                f"Sessions: {len(sessions)} total | "
                f"{attached_count} attached | "
                f"{detached_count} detached | "
                f"{stopped_count} stopped"
            ),
            f"Warnings: {len(warnings)}",
            "",
            "ATTENTION SESSIONS",
        ]
        for session in sessions:
            notice = self._notice_for(session)
            if not notice.warning:
                continue
            payload.append(f"- {session.name}: {notice.title}")
        payload.extend(("", "HEALTH WARNINGS"))
        for check in warnings:
            payload.append(f"- {diagnostic_name(check)}: {diagnostic_detail(check, expanded=True)}")
        destination.write_text("\n".join(payload).strip() + "\n", encoding="utf-8")
        self._record_job("exported ops snapshot", severity="success")
        self._notify_success(f"Ops snapshot exported: {destination}", title="Report exported")

    def action_health_alerts(self) -> None:
        self._track_action_invocation("health", "h")
        self._animate_workspace_transition("forward")
        self._begin_overlay(InteractionMode.FORM)
        self.push_screen(
            HealthAlertsScreen(self.service),
            self._finish_health_alerts,
        )

    def action_project_board(self) -> None:
        project = self.filters.project or (self._selected().project if self._selected() else "")
        board = self.service.project_board(project=project)
        lines = [f"Project board: {project or 'all projects'}", ""]
        for lane in ("todo", "doing", "blocked", "done"):
            sessions = board[lane]
            lines.append(f"{lane.upper()} ({len(sessions)}):")
            if sessions:
                lines.extend(f"- {item.display_name or item.name}" for item in sessions[:10])
            else:
                lines.append("- none")
            lines.append("")
        self._animate_workspace_transition("forward")
        self._begin_overlay(InteractionMode.FORM)
        self.push_screen(
            MessageScreen("Project board", "\n".join(lines).strip()),
            lambda _result: self._restore_dashboard_mode(),
        )

    def action_dependency_graph(self) -> None:
        self._animate_workspace_transition("forward")
        self._begin_overlay(InteractionMode.FORM)
        self.push_screen(
            DependencyGraphScreen(self.service, focus_session=self.selected_name or ""),
            lambda _result: self._restore_dashboard_mode(),
        )

    def action_federation_center(self) -> None:
        self._animate_workspace_transition("forward")
        self._begin_overlay(InteractionMode.FORM)
        self.push_screen(
            FederationControlScreen(self.service),
            self._finish_federation_center,
        )

    def action_policy_sandbox(self) -> None:
        self._animate_workspace_transition("forward")
        self._begin_overlay(InteractionMode.FORM)
        self.push_screen(
            PolicySandboxScreen(self.service),
            lambda _result: self._restore_dashboard_mode(),
        )

    def action_interface_controls(self) -> None:
        if self.interaction_mode not in {InteractionMode.NORMAL, InteractionMode.SEARCH}:
            return
        self._animate_workspace_transition("forward")
        self._begin_overlay(InteractionMode.FORM)
        self.push_screen(
            InterfaceControlsScreen(),
            lambda _result: self._restore_dashboard_mode(),
        )

    def action_theme_tokens(self) -> None:
        if self.interaction_mode not in {InteractionMode.NORMAL, InteractionMode.SEARCH}:
            return
        self._animate_workspace_transition("forward")
        self._begin_overlay(InteractionMode.FORM)
        self.push_screen(
            ThemeTokenScreen(),
            lambda _result: self._restore_dashboard_mode(),
        )

    def _finish_health_alerts(self, action: str | None) -> None:
        self._restore_dashboard_mode()
        if action and action.startswith("view-sessions"):
            _, _, payload = action.partition(":")
            related = [item for item in payload.split(",") if item]
            if related:
                self.quick_filter = "warnings"
                self.filters = replace(self.filters, warnings_only=True)
                self.filter_query = ""
                with self.prevent(Input.Changed):
                    self.query_one("#search", Input).value = ""
                self._render_options()
                target = next(
                    (session for session in self.visible_sessions if session.name in set(related)),
                    None,
                )
                if target is not None:
                    self.selected_name = target.name
                    self.selected_session_id = target.session_id
                    self._render_options()
                self.notify(f"Focused warning sessions: {len(related)}")
            else:
                self.call_after_refresh(self.action_attention)
        elif action == "dismiss":
            self._health_dismissed_until_refresh = True
            self._render_health_row()

    def _finish_federation_center(self, action: str | None) -> None:
        self._restore_dashboard_mode()
        if not action or not action.startswith("focus-session:"):
            return
        _, _, payload = action.partition(":")
        target_name, _, follow_up = payload.partition(":")
        if not target_name:
            return
        target = next(
            (session for session in self.visible_sessions if session.name == target_name), None
        )
        if target is None:
            target = next(
                (session for session in self.sessions if session.name == target_name), None
            )
        if target is None:
            self.notify(f"Local session not found: {target_name}", severity="warning")
            return
        self.selected_name = target.name
        self.selected_session_id = target.session_id
        self._render_options()
        if follow_up == "manage":
            self.call_after_refresh(self.action_manage)
            return
        if follow_up == "logs":
            self.call_after_refresh(self.action_logs)
            return
        self.notify(f"Focused local session: {target.display_name or target.name}")

    def action_help(self) -> None:
        default_tool = self._default_create_tool()
        create_help = (
            f"c        Create session (default: {TOOL_LABELS[default_tool]})"
            if default_tool is not None
            else "c        Create session (all tool profiles disabled)"
        )
        content = (
            "Up/Down or j/k  Navigate warnings\n"
            "Enter    Attach or open details\n"
            "Esc      Return to the previous dashboard view\n"
            "f        Open full filters\n"
            "r        Refresh inventory and alerts\n"
            "v        Open policy sandbox\n"
            "D        Open dependency graph\n"
            "F        Open federation control center\n"
            "         (Enter local focus; w warning focus; m/l actions;\n"
            "          5/6/7 views; 8 diff; 9 safe mode)\n"
            "M/C/A    Motion/contrast/accent controls\n"
            "W        Open warning triage wizard\n"
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
            "m        Manage session\n"
            "d        Manage session\n"
            "r        Refresh\n"
            "g        Cycle grouping\n"
            "z        Toggle compact/comfortable density\n"
            "w        Cycle text scale (compact/comfortable/readable)\n"
            "M        Cycle motion preset (auto/off/subtle/full)\n"
            "C        Toggle high-contrast mode\n"
            "A        Cycle accent style\n"
            "K        Toggle hint density\n"
            "h        Open System Health\n"
            "v        Open policy sandbox\n"
            "D        Open dependency graph\n"
            "F        Open federation control center\n"
            "         (Enter local focus; w warning focus; m/l actions;\n"
            "          5/6/7 views; 8 diff; 9 safe mode)\n"
            "B        Show project board lanes\n"
            "U        Undo last risky action\n"
            "W        Open warning triage wizard\n"
            "I        Open interface controls panel\n"
            "t        Cycle color theme\n"
            "q        Quit"
            if self.narrow_detail_open
            else "Up/Down or j/k  Navigate\n"
            "Enter    Attach or open details\n"
            f"{create_help}\n"
            "o        Open preset launcher\n"
            "Space    Mark selected session for bulk actions\n"
            "Ctrl+A   Mark all visible sessions\n"
            "Ctrl+U   Clear bulk marks\n"
            "Alt+I    Invert visible bulk marks\n"
            "b        Apply a bulk action to marked sessions\n"
            "7/8/9    Set selected progress to todo/doing/done\n"
            "1..6     Quick filters (All, Active, Detached, Warnings, Stopped, Blocked)\n"
            "/        Search\n"
            "f        Filter sessions\n"
            "e        Edit selected session\n"
            "n        Edit task\n"
            "l        View logs\n"
            "i        View selected session timeline\n"
            "*        Toggle pin\n"
            "m        Manage session\n"
            "d        Manage session\n"
            "U        Undo last risky action\n"
            "W        Open warning triage wizard\n"
            "u        Show all tmux sessions (unmanaged too)\n"
            "r        Refresh\n"
            "g        Cycle grouping\n"
            "z        Toggle compact/comfortable density\n"
            "w        Cycle text scale (compact/comfortable/readable)\n"
            "M        Cycle motion preset (auto/off/subtle/full)\n"
            "C        Toggle high-contrast mode\n"
            "A        Cycle accent style\n"
            "K        Toggle hint density\n"
            "h        Open System Health\n"
            "v        Open policy sandbox\n"
            "D        Open dependency graph\n"
            "I        Open interface controls panel\n"
            "p        Command palette\n"
            "x        Export ops snapshot report\n"
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
