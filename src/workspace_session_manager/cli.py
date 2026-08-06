"""Typer command line interface and Textual application entry point."""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import stat
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Annotated
from uuid import UUID

import typer
from rich.console import Console
from rich.markup import escape as escape_markup
from rich.table import Table

from workspace_session_manager import __version__
from workspace_session_manager.config import AppConfig, ToolProfile, default_tools, load_config
from workspace_session_manager.errors import WsError
from workspace_session_manager.legacy import LegacyMetadataReader
from workspace_session_manager.migration import MigrationManager, MigrationPlan
from workspace_session_manager.models import (
    CreateRequest,
    DoctorReport,
    FilterPreset,
    HealthStatus,
    InputState,
    Preset,
    RuntimeState,
    SessionTemplate,
    SessionView,
    TaskState,
    Tool,
)
from workspace_session_manager.paths import AppPaths
from workspace_session_manager.security import redact_text, sanitize_terminal
from workspace_session_manager.service import (
    SessionService,
    command_available,
    default_enabled_tool,
)
from workspace_session_manager.store import MetadataStore
from workspace_session_manager.tmux import TmuxBackend
from workspace_session_manager.tui import THEME_MODES, WsApp

app = typer.Typer(
    name="ws",
    help="Manage persistent AI and shell sessions with tmux.",
    no_args_is_help=False,
    invoke_without_command=True,
    pretty_exceptions_enable=False,
)
console = Console()
error_console = Console(stderr=True)
SETUP_TOOL_ORDER: tuple[Tool, ...] = (
    Tool.CLAUDE,
    Tool.COPILOT,
    Tool.CODEX,
    Tool.HERMES,
    Tool.SHELL,
)
SETUP_TOOL_LABELS = {
    Tool.CLAUDE: "Claude Code",
    Tool.COPILOT: "GitHub Copilot CLI",
    Tool.CODEX: "Codex CLI",
    Tool.HERMES: "Hermes Agent",
    Tool.SHELL: "Shell",
}


def _format_command(command: tuple[str, ...]) -> str:
    return " ".join(shlex.quote(part) for part in command)


def _parse_command(command: str) -> tuple[str, ...]:
    parts = tuple(shlex.split(command))
    if not parts:
        raise WsError("tool command cannot be empty")
    return parts


def _render_tools_toml(tools: dict[Tool, ToolProfile]) -> str:
    lines: list[str] = []
    for tool in SETUP_TOOL_ORDER:
        profile = tools[tool]
        command = ", ".join(json.dumps(part) for part in profile.command)
        lines.extend(
            (
                f"[tools.{tool.value}]",
                f"command = [{command}]",
                f"enabled = {'true' if profile.enabled else 'false'}",
                "",
            )
        )
    return "\n".join(lines).rstrip() + "\n"


def _write_setup_config(path: Path, tools: dict[Tool, ToolProfile]) -> None:
    if path.is_symlink():
        raise WsError(f"refusing symlinked configuration file: {path}")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    payload = _render_tools_toml(tools)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as temporary:
            temporary_name = temporary.name
            temporary.write(payload)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.chmod(temporary_name, 0o600)
        os.replace(temporary_name, path)
    except OSError as error:
        if temporary_name:
            Path(temporary_name).unlink(missing_ok=True)
        raise WsError(f"unable to write configuration: {error}") from error


def _configured_setup_tools(
    existing: dict[Tool, ToolProfile],
    *,
    yes: bool,
) -> dict[Tool, ToolProfile]:
    defaults = default_tools()
    configured_tools: dict[Tool, ToolProfile] = {}
    for tool in SETUP_TOOL_ORDER:
        base = existing.get(tool, defaults[tool])
        if tool is Tool.SHELL:
            configured_tools[tool] = ToolProfile(command=base.command, enabled=True)
            continue

        detected = shutil.which(base.command[0])
        suggested = (detected, *base.command[1:]) if detected is not None else base.command
        enable_default = detected is not None
        if yes:
            enabled = enable_default
            command = suggested if enabled else base.command
        else:
            label = SETUP_TOOL_LABELS[tool]
            status = f"found at {detected}" if detected else "not detected on PATH"
            enabled = typer.confirm(f"Enable {label}? ({status})", default=enable_default)
            command = base.command
            if enabled:
                value = typer.prompt(
                    f"{label} command",
                    default=_format_command(suggested),
                )
                command = _parse_command(value)
        configured_tools[tool] = ToolProfile(command=command, enabled=enabled)
    return configured_tools


def _display_safe(text: str) -> str:
    """Neutralize control bytes and Rich markup before printing untrusted
    text (session notes, legacy-migrated metadata, pane output) -- a stray
    "[/bold]"-shaped substring otherwise raises MarkupError, and terminal
    escape sequences would otherwise render live."""
    return escape_markup(sanitize_terminal(text))
migration_app = typer.Typer(help="Preview, apply, inspect, and roll back session adoption.")
app.add_typer(migration_app, name="migrate")
preset_app = typer.Typer(help="Save, list, and delete create-session presets.")
app.add_typer(preset_app, name="preset")
onboarding_app = typer.Typer(help="Manage onboarding state.")
app.add_typer(onboarding_app, name="onboarding")
filter_preset_app = typer.Typer(help="Save and reuse dashboard filter presets.")
app.add_typer(filter_preset_app, name="filter-preset")
template_app = typer.Typer(help="Save and reuse variable-based session templates.")
app.add_typer(template_app, name="template")
dependency_app = typer.Typer(help="Manage session dependency graph.")
app.add_typer(dependency_app, name="dependency")
profile_app = typer.Typer(help="Manage operator profiles and audit context.")
app.add_typer(profile_app, name="profile")
federation_dashboard_app = typer.Typer(help="Save and reopen named federation host views.")
app.add_typer(federation_dashboard_app, name="federation-dashboard")
fleet_snapshot_app = typer.Typer(help="Capture and inspect federation fleet snapshots.")
app.add_typer(fleet_snapshot_app, name="fleet-snapshot")
incident_app = typer.Typer(help="Coordinate and track incidents.")
app.add_typer(incident_app, name="incident")
approval_app = typer.Typer(help="Issue signed approvals for guarded actions.")
app.add_typer(approval_app, name="approval")


@dataclass(frozen=True, slots=True)
class Runtime:
    paths: AppPaths
    config: AppConfig

    def service(self) -> SessionService:
        return SessionService(
            backend=TmuxBackend(),
            store=MetadataStore(self.paths),
            config=self.config,
            paths=self.paths,
            legacy=LegacyMetadataReader(self.config.legacy_state_dirs),
        )

    def migration(self) -> MigrationManager:
        return MigrationManager(
            backend=TmuxBackend(),
            store=MetadataStore(self.paths),
            legacy=LegacyMetadataReader(self.config.legacy_state_dirs),
            paths=self.paths,
        )


def build_runtime(config_path: Path | None = None) -> Runtime:
    paths = AppPaths.discover()
    return Runtime(paths=paths, config=load_config(paths, config_path))


def runtime_from_context(context: typer.Context) -> Runtime:
    runtime = context.obj
    if not isinstance(runtime, Runtime):
        raise RuntimeError("ws runtime was not initialized")
    return runtime


def abort(error: Exception) -> None:
    error_console.print(f"[red]Error:[/red] {error}")
    raise typer.Exit(1)


def _split_approval_tokens(approval_tokens: list[str] | None) -> tuple[str, ...]:
    values = [token.strip() for token in (approval_tokens or []) if token.strip()]
    return tuple(values)


def _split_approval_code_and_tokens(
    approval: str | None, approval_tokens: list[str] | None
) -> tuple[str, tuple[str, ...]]:
    code = (approval or "").strip()
    tokens = _split_approval_tokens(approval_tokens)
    if "." in code and not tokens:
        return "", (code,)
    return code, tokens


def require_approval(
    runtime: Runtime,
    action: str,
    approval: str | None,
    *,
    approval_tokens: list[str] | None = None,
) -> None:
    service = runtime.service()
    code, tokens = _split_approval_code_and_tokens(approval, approval_tokens)
    if not service.evaluate_approval(action, code=code, approval_tokens=tokens):
        raise WsError(f"approval required for {action}; pass --approval <code> or --approval-token")


def run_classic() -> None:
    """Replace this process with the installer-preserved classic launcher."""
    classic = Path.home() / ".local" / "libexec" / "wf-classic"
    try:
        details = classic.stat(follow_symlinks=False)
    except OSError as error:
        raise WsError(f"classic launcher is unavailable: {classic}") from error
    if (
        not stat.S_ISREG(details.st_mode)
        or details.st_uid != os.getuid()
        or details.st_mode & 0o077
        or not os.access(classic, os.X_OK)
    ):
        raise WsError(f"refusing unsafe classic launcher: {classic}")
    os.execv(classic, [str(classic)])  # noqa: S606 - validated owner-only executable


def run_tui(runtime: Runtime, *, no_animation: bool = False) -> None:
    try:
        result = WsApp(runtime.service(), no_animation=no_animation).run()
        if result:
            runtime.service().attach(result)
    except WsError as error:
        abort(error)


@app.callback()
def root(
    context: typer.Context,
    version: Annotated[
        bool,
        typer.Option("--version", "-V", help="Show the ws version."),
    ] = False,
    config: Annotated[
        Path | None,
        typer.Option("--config", help="Use a specific TOML configuration file."),
    ] = None,
    classic: Annotated[
        bool,
        typer.Option("--classic", help="Run the preserved classic launcher."),
    ] = False,
    no_animation: Annotated[
        bool,
        typer.Option("--no-animation", help="Disable TUI motion effects."),
    ] = False,
    profile: Annotated[
        str | None,
        typer.Option("--profile", help="Activate a named operator profile."),
    ] = None,
) -> None:
    """Open the session manager when no subcommand is supplied."""
    if classic:
        try:
            run_classic()
        except WsError as error:
            abort(error)
    try:
        runtime = build_runtime(config)
    except WsError as error:
        abort(error)
    context.obj = runtime
    if profile:
        try:
            runtime.service().select_operator_profile(profile)
        except WsError as error:
            abort(error)
            return
    if version:
        typer.echo(f"ws {__version__}")
        raise typer.Exit()
    if context.invoked_subcommand is None:
        run_tui(runtime, no_animation=no_animation)


@app.command("list")
def list_command(
    context: typer.Context,
    as_json: Annotated[bool, typer.Option("--json", help="Emit machine-readable JSON.")] = False,
    include_unmanaged: Annotated[
        bool,
        typer.Option("--all", help="Include unmanaged tmux sessions for diagnostics."),
    ] = False,
    tag: Annotated[
        str | None, typer.Option("--tag", help="Only show sessions with this exact tag.")
    ] = None,
    project: Annotated[
        str | None, typer.Option("--project", help="Only show sessions in this exact project.")
    ] = None,
) -> None:
    """List managed sessions without changing them."""
    try:
        sessions = (
            runtime_from_context(context)
            .service()
            .list_sessions(include_unmanaged=include_unmanaged)
        )
    except WsError as error:
        abort(error)
    if tag is not None:
        sessions = [session for session in sessions if tag in session.tags]
    if project is not None:
        sessions = [session for session in sessions if session.project == project]
    if as_json:
        typer.echo(json.dumps([item.model_dump(mode="json") for item in sessions], indent=2))
        return
    table = Table(show_header=True, header_style="bold cyan", box=None)
    table.add_column("Runtime")
    table.add_column("Session")
    table.add_column("Tool")
    table.add_column("Task")
    table.add_column("Input")
    table.add_column("Directory")
    table.add_column("Owner")
    for session in sessions:
        table.add_row(
            session.runtime.value,
            session.name,
            session.tool.value,
            session.task_state.value.replace("_", " "),
            session.input_state.value,
            str(session.cwd),
            "managed" if session.owned else "unmanaged",
        )
    console.print(table)


@app.command()
def inspect(
    context: typer.Context,
    name: Annotated[str, typer.Argument(help="Exact tmux session name.")],
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Show metadata and a sanitized pane preview."""
    try:
        details = runtime_from_context(context).service().inspect(name)
    except WsError as error:
        abort(error)
    if as_json:
        typer.echo(details.model_dump_json(indent=2))
        return
    session = details.session
    console.print(f"[bold cyan]{session.name}[/bold cyan]")
    console.print(f"Tool: {session.tool.value}")
    console.print(f"Runtime: {session.runtime.value}")
    console.print(f"Task: {session.task_state.value.replace('_', ' ')}")
    console.print(f"Input: {session.input_state.value}")
    console.print("Ownership: managed")
    console.print(f"Directory: {session.cwd}")
    console.print(f"Note: {_display_safe(session.note) if session.note else '-'}")
    console.rule("Sanitized preview")
    console.print(_display_safe(details.preview) if details.preview else "No pane output")


@app.command("timeline")
def timeline_command(
    context: typer.Context,
    name: Annotated[str, typer.Argument(help="Exact session name.")],
    limit: Annotated[int, typer.Option("--limit", help="Rows to display.")] = 25,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Show recent per-session operational timeline."""
    service = runtime_from_context(context).service()
    try:
        events = service.timeline(name, limit=limit)
    except WsError as error:
        abort(error)
        return
    if as_json:
        typer.echo(json.dumps([item.model_dump(mode="json") for item in events], indent=2))
        return
    table = Table(show_header=True, header_style="bold cyan", box=None)
    table.add_column("Time")
    table.add_column("Action")
    table.add_column("Detail")
    for event in events:
        table.add_row(event.timestamp.isoformat(timespec="seconds"), event.action, event.detail or "-")
    console.print(table)


@app.command("handoff")
def handoff_command(
    context: typer.Context,
    name: Annotated[str, typer.Argument(help="Exact session name.")],
    limit: Annotated[int, typer.Option("--timeline-limit")] = 12,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Create an actionable handoff snapshot for an active session."""
    service = runtime_from_context(context).service()
    try:
        details = service.inspect(name)
        events = service.timeline(name, limit=limit)
    except WsError as error:
        abort(error)
        return
    payload = {
        "session": details.session.model_dump(mode="json"),
        "next_action": "Continue current task unless blocked.",
        "timeline": [item.model_dump(mode="json") for item in events],
        "preview": details.preview,
    }
    if as_json:
        typer.echo(json.dumps(payload, indent=2))
        return
    console.print(f"[bold cyan]Handoff[/bold cyan] {details.session.name}")
    console.print(f"Tool: {details.session.tool.value}")
    console.print(f"Task: {details.session.task_state.value.replace('_', ' ')}")
    console.print(f"Input: {details.session.input_state.value}")
    console.print(f"Project: {details.session.project or '-'}")
    console.print(f"Next action: {payload['next_action']}")
    console.rule("Recent timeline")
    for event in events:
        console.print(
            f"{event.timestamp.isoformat(timespec='seconds')}  {event.action}  {_display_safe(event.detail)}"
        )
    console.rule("Recent output")
    console.print(_display_safe(details.preview) if details.preview else "No output")


@app.command("handoff-auto")
def handoff_auto_command(
    context: typer.Context,
    project: Annotated[str, typer.Option("--project")] = "",
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Generate a multi-session handoff pack."""
    service = runtime_from_context(context).service()
    try:
        payload = service.auto_handoff(project=project)
    except WsError as error:
        abort(error)
        return
    if as_json:
        typer.echo(json.dumps(payload, indent=2))
        return
    console.print("[bold cyan]Auto handoff[/bold cyan]")
    console.print(f"Project filter: {project or 'all'}")
    for session in payload["sessions"]:
        assert isinstance(session, dict)
        console.print(
            f"- {session['name']} | {session['runtime']} | {session['task']} | {session['summary'] or '-'}"
        )


@incident_app.command("start")
def incident_start_command(
    context: typer.Context,
    title: Annotated[str, typer.Argument(help="Incident title.")],
    severity: Annotated[str, typer.Option("--severity")] = "warn",
    owner: Annotated[str, typer.Option("--owner")] = "",
    summary: Annotated[str, typer.Option("--summary")] = "",
    session: Annotated[list[str] | None, typer.Option("--session")] = None,
    host: Annotated[list[str] | None, typer.Option("--host")] = None,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Create a new incident commander record."""
    service = runtime_from_context(context).service()
    try:
        payload = service.start_incident(
            title=title,
            severity=severity,
            owner=owner,
            summary=summary,
            sessions=tuple(session or ()),
            hosts=tuple(host or ()),
        )
    except WsError as error:
        abort(error)
        return
    if as_json:
        typer.echo(json.dumps(payload, indent=2))
        return
    console.print(f"Incident opened: [bold]{payload.get('id','')}[/bold] ({payload.get('severity','')})")


@incident_app.command("status")
def incident_status_command(
    context: typer.Context,
    incident_id: Annotated[str | None, typer.Argument()] = None,
    status: Annotated[str, typer.Option("--status", help="Filter by open|closed.")] = "",
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Show one incident or list incidents."""
    service = runtime_from_context(context).service()
    try:
        if incident_id:
            payload = service.get_incident(incident_id)
            if as_json:
                typer.echo(json.dumps(payload, indent=2))
                return
            console.print(
                f"{payload.get('id')} | {payload.get('status')} | {payload.get('severity')} | {payload.get('title')}"
            )
            console.print(_display_safe(str(payload.get("summary", "")) or "-"))
            return
        rows = service.list_incidents(status=status)
    except WsError as error:
        abort(error)
        return
    if as_json:
        typer.echo(json.dumps(rows, indent=2))
        return
    if not rows:
        console.print("No incidents found.")
        return
    table = Table(show_header=True, header_style="bold cyan", box=None)
    table.add_column("ID")
    table.add_column("Status")
    table.add_column("Severity")
    table.add_column("Owner")
    table.add_column("Title")
    table.add_column("Updated")
    for row in rows:
        table.add_row(
            str(row.get("id", "")),
            str(row.get("status", "")),
            str(row.get("severity", "")),
            str(row.get("owner", "")),
            _display_safe(str(row.get("title", ""))),
            str(row.get("updated_at", "")),
        )
    console.print(table)


@incident_app.command("update")
def incident_update_command(
    context: typer.Context,
    incident_id: Annotated[str, typer.Argument(help="Incident ID.")],
    owner: Annotated[str | None, typer.Option("--owner")] = None,
    summary: Annotated[str | None, typer.Option("--summary")] = None,
    session: Annotated[list[str] | None, typer.Option("--session")] = None,
    host: Annotated[list[str] | None, typer.Option("--host")] = None,
    note: Annotated[str, typer.Option("--note")] = "",
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Update incident ownership/scope/summary and append a note."""
    service = runtime_from_context(context).service()
    try:
        payload = service.update_incident(
            incident_id,
            owner=owner,
            summary=summary,
            sessions=session,
            hosts=host,
            note=note,
        )
    except WsError as error:
        abort(error)
        return
    if as_json:
        typer.echo(json.dumps(payload, indent=2))
        return
    console.print(f"Incident updated: [bold]{payload.get('id','')}[/bold]")


@incident_app.command("close")
def incident_close_command(
    context: typer.Context,
    incident_id: Annotated[str, typer.Argument(help="Incident ID.")],
    resolution: Annotated[str, typer.Option("--resolution")] = "",
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Close an incident commander record."""
    service = runtime_from_context(context).service()
    try:
        payload = service.close_incident(incident_id, resolution=resolution)
    except WsError as error:
        abort(error)
        return
    if as_json:
        typer.echo(json.dumps(payload, indent=2))
        return
    console.print(f"Incident closed: [bold]{payload.get('id','')}[/bold]")


@app.command("incident-bundle")
def incident_bundle_command(
    context: typer.Context,
    session: Annotated[
        list[str] | None,
        typer.Option("--session", help="Include only these session names (repeatable)."),
    ] = None,
    project: Annotated[str, typer.Option("--project")] = "",
    timeline_limit: Annotated[int, typer.Option("--timeline-limit")] = 20,
    audit_limit: Annotated[int, typer.Option("--audit-limit")] = 400,
    include_federation: Annotated[
        bool,
        typer.Option(
            "--include-federation",
            help="Include cross-host federation sessions, health, and report evidence.",
        ),
    ] = False,
    host: Annotated[
        list[str] | None,
        typer.Option("--host", help="Federation host override (repeatable)."),
    ] = None,
    output: Annotated[
        Path | None,
        typer.Option("--output", help="Write bundle archive to this path."),
    ] = None,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Export an incident bundle with local and optional federation evidence."""
    service = runtime_from_context(context).service()
    try:
        destination = service.export_incident_bundle(
            sessions=tuple(session or ()),
            project=project,
            timeline_limit=timeline_limit,
            audit_limit=audit_limit,
            include_federation=include_federation,
            federation_hosts=tuple(host or ()),
            destination=output.expanduser() if output is not None else None,
        )
    except WsError as error:
        abort(error)
        return
    payload = {
        "path": str(destination),
        "project": project,
        "sessions": list(session or ()),
        "federation_included": include_federation,
        "federation_hosts": list(host or ()),
    }
    if as_json:
        typer.echo(json.dumps(payload, indent=2))
        return
    console.print("[bold cyan]Incident bundle exported[/bold cyan]")
    console.print(f"Path: {destination}")


@app.command("correlate")
def correlate_command(
    context: typer.Context,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Group recurring recent-output lines across sessions."""
    service = runtime_from_context(context).service()
    groups = service.correlate_output()
    if as_json:
        typer.echo(json.dumps(groups, indent=2))
        return
    if not groups:
        console.print("No cross-session output correlations found.")
        return
    table = Table(show_header=True, header_style="bold cyan", box=None)
    table.add_column("Shared output pattern")
    table.add_column("Sessions")
    for line, names in sorted(groups.items(), key=lambda item: len(item[1]), reverse=True):
        table.add_row(_display_safe(line), ", ".join(names))
    console.print(table)


@app.command()
def create(
    context: typer.Context,
    name: Annotated[str | None, typer.Option("--name", "-n")] = None,
    tool: Annotated[Tool | None, typer.Option("--tool", "-t")] = None,
    cwd: Annotated[Path | None, typer.Option("--cwd", "-C")] = None,
    project: Annotated[str, typer.Option("--project")] = "",
    note: Annotated[str, typer.Option("--note")] = "",
    tag: Annotated[list[str] | None, typer.Option("--tag")] = None,
    logging: Annotated[
        bool | None,
        typer.Option("--logging/--no-logging", help="Persist sanitized, size-limited output."),
    ] = None,
    from_preset: Annotated[
        str | None,
        typer.Option(
            "--from-preset", help="Load tool/cwd/project/tags/logging from a saved preset."
        ),
    ] = None,
    from_session: Annotated[
        str | None,
        typer.Option(
            "--from-session",
            help="Load tool/cwd/project/tags/logging from an existing session (clone).",
        ),
    ] = None,
    from_template: Annotated[
        str | None,
        typer.Option(
            "--from-template",
            help="Render a saved variable template into create parameters.",
        ),
    ] = None,
    var: Annotated[
        list[str] | None,
        typer.Option("--var", help="Template variable assignment KEY=VALUE."),
    ] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
    attach: Annotated[bool, typer.Option("--attach")] = False,
) -> None:
    """Create a detached, persistent ws-owned session."""
    if sum(int(value is not None) for value in (from_preset, from_session, from_template)) > 1:
        abort(
            WsError(
                "--from-preset, --from-session, and --from-template cannot be combined"
            )
        )
        return
    service = runtime_from_context(context).service()
    template: Preset | SessionView | None = None
    rendered: dict[str, object] | None = None
    try:
        if from_preset is not None:
            template = service.get_preset(from_preset)
        elif from_session is not None:
            template = service.get(from_session)
        elif from_template is not None:
            saved_template = service.get_template(from_template)
            variables: dict[str, str] = {}
            for item in var or []:
                key, sep, value = item.partition("=")
                if not sep or not key:
                    abort(WsError(f"invalid --var value: {item!r}, expected KEY=VALUE"))
                    return
                variables[key.strip()] = value
            rendered = {
                "tool": saved_template.tool,
                "name": service.render_template(saved_template.name_template, variables),
                "cwd": service.render_template(saved_template.cwd_template, variables),
                "project": service.render_template(saved_template.project_template, variables),
                "note": service.render_template(saved_template.note_template, variables),
                "tags": list(saved_template.tags),
                "logging_enabled": saved_template.logging_enabled,
            }
    except WsError as error:
        abort(error)
        return
    resolved_tool = tool if tool is not None else (
        rendered["tool"] if rendered else (template.tool if template else default_enabled_tool(service.config))
    )
    if resolved_tool is None:
        abort(
            WsError(
                "no enabled tool profiles are configured; "
                "enable at least one tool in config.toml"
            )
        )
        return
    resolved_cwd = cwd if cwd is not None else (
        Path(str(rendered["cwd"])).expanduser() if rendered else (template.cwd if template else Path.cwd())
    )
    resolved_name = name if name else (str(rendered["name"]) if rendered else "")
    if not resolved_name:
        abort(WsError("--name is required unless --from-template provides name_template"))
        return
    request = CreateRequest(
        name=resolved_name,
        tool=resolved_tool,
        cwd=resolved_cwd.expanduser().resolve(),
        project=project or (str(rendered["project"]) if rendered else (template.project if template else "")),
        note=note or (str(rendered["note"]) if rendered else ""),
        tags=tag if tag is not None else (
            list(rendered["tags"]) if rendered else (list(template.tags) if template else [])
        ),
        logging_enabled=logging
        if logging is not None
        else (bool(rendered["logging_enabled"]) if rendered else (template.logging_enabled if template else True)),
    )
    try:
        session = service.create(request, dry_run=dry_run)
    except WsError as error:
        abort(error)
    prefix = "Would create" if dry_run else "Created"
    console.print(f"{prefix}: [bold]{session.name}[/bold] in {session.cwd}")
    if attach and not dry_run:
        try:
            service.attach(session.name)
        except WsError as error:
            abort(error)


@preset_app.command("save")
def preset_save(
    context: typer.Context,
    name: Annotated[str, typer.Argument(help="Preset identifier, e.g. backend-dev.")],
    tool: Annotated[Tool, typer.Option("--tool", "-t")],
    cwd: Annotated[Path, typer.Option("--cwd", "-C")],
    project: Annotated[str, typer.Option("--project")] = "",
    tag: Annotated[list[str] | None, typer.Option("--tag")] = None,
    logging: Annotated[
        bool,
        typer.Option("--logging/--no-logging", help="Persist sanitized, size-limited output."),
    ] = True,
) -> None:
    """Save (or overwrite) a named create-session preset."""
    service = runtime_from_context(context).service()
    try:
        preset = service.save_preset(
            name,
            tool=tool,
            cwd=cwd.expanduser().resolve(),
            project=project,
            tags=tag or [],
            logging_enabled=logging,
        )
    except WsError as error:
        abort(error)
        return
    console.print(f"Saved preset: [bold]{preset.name}[/bold]")


@preset_app.command("list")
def preset_list(
    context: typer.Context,
    as_json: Annotated[bool, typer.Option("--json", help="Emit machine-readable JSON.")] = False,
) -> None:
    """List saved create-session presets."""
    try:
        presets = runtime_from_context(context).service().list_presets()
    except WsError as error:
        abort(error)
        return
    if as_json:
        typer.echo(json.dumps([item.model_dump(mode="json") for item in presets], indent=2))
        return
    table = Table(show_header=True, header_style="bold cyan", box=None)
    table.add_column("Name")
    table.add_column("Tool")
    table.add_column("Directory")
    table.add_column("Project")
    table.add_column("Tags")
    table.add_column("Logging")
    for preset in presets:
        table.add_row(
            preset.name,
            preset.tool.value,
            str(preset.cwd),
            _display_safe(preset.project),
            ", ".join(preset.tags),
            "enabled" if preset.logging_enabled else "disabled",
        )
    console.print(table)


@preset_app.command("delete")
def preset_delete(
    context: typer.Context,
    name: Annotated[str, typer.Argument(help="Preset identifier to delete.")],
) -> None:
    """Delete a saved create-session preset."""
    try:
        runtime_from_context(context).service().delete_preset(name)
    except WsError as error:
        abort(error)
        return
    console.print(f"Deleted preset: [bold]{name}[/bold]")


@preset_app.command("validate")
def preset_validate(
    context: typer.Context,
    as_json: Annotated[bool, typer.Option("--json")] = False,
    actionable: Annotated[
        bool,
        typer.Option("--actionable", help="Show only warn/fail preset checks."),
    ] = False,
) -> None:
    """Validate preset tool readiness and working directories."""
    service = runtime_from_context(context).service()
    rows = _preset_validation_rows(service)
    if actionable:
        rows = [row for row in rows if row[1] is not HealthStatus.PASS]
    if as_json:
        payload = [
            {
                "name": name,
                "status": status.value,
                "detail": detail,
                "fix": fix,
            }
            for name, status, detail, fix in rows
        ]
        typer.echo(json.dumps(payload, indent=2))
    else:
        if not rows:
            console.print("No actionable preset issues.")
            return
        table = Table(show_header=True, header_style="bold cyan", box=None)
        table.add_column("Preset")
        table.add_column("Status")
        table.add_column("Detail")
        table.add_column("Fix")
        for name, status, detail, fix in rows:
            table.add_row(name, status.value, detail, fix or "-")
        console.print(table)
    if any(status is HealthStatus.FAIL for _, status, _, _ in rows):
        raise typer.Exit(1)


@filter_preset_app.command("save")
def filter_preset_save(
    context: typer.Context,
    name: Annotated[str, typer.Argument(help="Filter preset name.")],
    query: Annotated[str, typer.Option("--query")] = "",
    tool: Annotated[Tool | None, typer.Option("--tool")] = None,
    runtime: Annotated[RuntimeState | None, typer.Option("--runtime")] = None,
    task: Annotated[TaskState | None, typer.Option("--task")] = None,
    tag: Annotated[str | None, typer.Option("--tag")] = None,
    project: Annotated[str | None, typer.Option("--project")] = None,
    warnings_only: Annotated[bool, typer.Option("--warnings-only")] = False,
    recent_only: Annotated[bool, typer.Option("--recent-only")] = False,
    quick_filter: Annotated[
        str,
        typer.Option("--quick-filter", help="all|active|detached|warnings|stopped|blocked"),
    ] = "all",
    grouping: Annotated[
        str | None,
        typer.Option("--grouping", help="attention|runtime|agent|project|warning|recent"),
    ] = None,
    density: Annotated[
        str | None,
        typer.Option("--density", help="compact|comfortable"),
    ] = None,
) -> None:
    service = runtime_from_context(context).service()
    allowed_quick = {"all", "active", "detached", "warnings", "stopped", "blocked"}
    allowed_grouping = {"attention", "runtime", "agent", "project", "warning", "recent"}
    allowed_density = {"compact", "comfortable"}
    if quick_filter not in allowed_quick:
        abort(WsError(f"invalid quick filter: {quick_filter}"))
        return
    if grouping is not None and grouping not in allowed_grouping:
        abort(WsError(f"invalid grouping: {grouping}"))
        return
    if density is not None and density not in allowed_density:
        abort(WsError(f"invalid density: {density}"))
        return
    try:
        saved = service.save_filter_preset(
            FilterPreset(
                name=name,
                query=query,
                tool=tool,
                runtime=runtime,
                task=task,
                tag=tag,
                project=project,
                warnings_only=warnings_only,
                recent_only=recent_only,
                quick_filter=quick_filter,
                grouping=grouping,
                density=density,
            )
        )
    except WsError as error:
        abort(error)
        return
    console.print(f"Saved filter preset: [bold]{saved.name}[/bold]")


@filter_preset_app.command("list")
def filter_preset_list(
    context: typer.Context,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    service = runtime_from_context(context).service()
    try:
        presets = service.list_filter_presets()
    except WsError as error:
        abort(error)
        return
    if as_json:
        typer.echo(json.dumps([item.model_dump(mode="json") for item in presets], indent=2))
        return
    table = Table(show_header=True, header_style="bold cyan", box=None)
    table.add_column("Name")
    table.add_column("Quick")
    table.add_column("Query")
    table.add_column("Tool")
    table.add_column("Runtime")
    table.add_column("Task")
    table.add_column("Grouping")
    table.add_column("Density")
    table.add_column("Tag")
    table.add_column("Project")
    for item in presets:
        table.add_row(
            item.name,
            item.quick_filter,
            item.query,
            item.tool.value if item.tool else "-",
            item.runtime.value if item.runtime else "-",
            item.task.value if item.task else "-",
            item.grouping or "-",
            item.density or "-",
            item.tag or "-",
            item.project or "-",
        )
    console.print(table)


@filter_preset_app.command("delete")
def filter_preset_delete(
    context: typer.Context,
    name: Annotated[str, typer.Argument(help="Filter preset name to remove.")],
) -> None:
    try:
        runtime_from_context(context).service().delete_filter_preset(name)
    except WsError as error:
        abort(error)
        return
    console.print(f"Deleted filter preset: [bold]{name}[/bold]")


@federation_dashboard_app.command("save")
def federation_dashboard_save(
    context: typer.Context,
    name: Annotated[str, typer.Argument(help="Dashboard name.")],
    host: Annotated[list[str], typer.Option("--host", help="Host entry (repeatable).")],
    host_filter: Annotated[str, typer.Option("--host-filter")] = "",
    scope_all_hosts: Annotated[bool, typer.Option("--scope-all-hosts")] = False,
    safe_mode: Annotated[bool, typer.Option("--safe-mode")] = False,
) -> None:
    service = runtime_from_context(context).service()
    try:
        saved = service.save_federation_dashboard(
            name=name,
            hosts=host,
            host_filter=host_filter,
            scope_all_hosts=scope_all_hosts,
            safe_mode=safe_mode,
        )
    except WsError as error:
        abort(error)
        return
    console.print(f"Saved federation dashboard: [bold]{saved['name']}[/bold]")


@federation_dashboard_app.command("list")
def federation_dashboard_list(
    context: typer.Context,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    service = runtime_from_context(context).service()
    rows = service.list_federation_dashboards()
    if as_json:
        typer.echo(json.dumps(rows, indent=2))
        return
    table = Table(show_header=True, header_style="bold cyan", box=None)
    table.add_column("Name")
    table.add_column("Hosts")
    table.add_column("Host filter")
    table.add_column("Scope")
    table.add_column("Safe mode")
    table.add_column("Updated")
    for row in rows:
        hosts_raw = row.get("hosts", [])
        hosts = hosts_raw if isinstance(hosts_raw, list) else []
        table.add_row(
            str(row.get("name", "")),
            ", ".join(str(host) for host in hosts),
            str(row.get("host_filter", "")) or "-",
            "all" if row.get("scope_all_hosts") else "selected",
            "on" if row.get("safe_mode") else "off",
            str(row.get("updated_at", "")) or "-",
        )
    console.print(table)


@federation_dashboard_app.command("delete")
def federation_dashboard_delete(
    context: typer.Context,
    name: Annotated[str, typer.Argument(help="Dashboard name to remove.")],
) -> None:
    service = runtime_from_context(context).service()
    try:
        service.delete_federation_dashboard(name)
    except WsError as error:
        abort(error)
        return
    console.print(f"Deleted federation dashboard: [bold]{name}[/bold]")


@federation_dashboard_app.command("open")
def federation_dashboard_open(
    context: typer.Context,
    name: Annotated[str, typer.Argument(help="Dashboard name to open.")],
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    service = runtime_from_context(context).service()
    try:
        dashboard = service.get_federation_dashboard(name)
    except WsError as error:
        abort(error)
        return
    hosts_raw = dashboard.get("hosts", [])
    hosts = [str(host) for host in hosts_raw] if isinstance(hosts_raw, list) else []
    rows = service.federated_sessions(hosts)
    payload = {"dashboard": dashboard, "rows": rows}
    if as_json:
        typer.echo(json.dumps(payload, indent=2))
        return
    table = Table(show_header=True, header_style="bold cyan", box=None)
    table.add_column("Host")
    table.add_column("Sessions")
    table.add_column("Status")
    for row in rows:
        sessions = row.get("sessions", [])
        error = row.get("error", "")
        table.add_row(
            str(row.get("host", "")),
            str(len(sessions) if isinstance(sessions, list) else 0),
            _display_safe(str(error or "ok")),
        )
    console.print(
        f"Opened federation dashboard: [bold]{dashboard.get('name','')}[/bold] "
        f"({len(hosts)} host(s))"
    )
    console.print(table)


@fleet_snapshot_app.command("save")
def fleet_snapshot_save(
    context: typer.Context,
    name: Annotated[str, typer.Argument(help="Snapshot name.")],
    host: Annotated[list[str] | None, typer.Option("--host")] = None,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    service = runtime_from_context(context).service()
    try:
        snapshot = service.save_fleet_snapshot(name=name, hosts=tuple(host or ()))
    except WsError as error:
        abort(error)
        return
    if as_json:
        typer.echo(json.dumps(snapshot, indent=2))
        return
    console.print(
        f"Saved fleet snapshot: [bold]{snapshot.get('name','')}[/bold] "
        f"({len(snapshot.get('hosts', []))} host(s))"
    )


@fleet_snapshot_app.command("list")
def fleet_snapshot_list(
    context: typer.Context,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    service = runtime_from_context(context).service()
    rows = service.list_fleet_snapshots()
    if as_json:
        typer.echo(json.dumps(rows, indent=2))
        return
    table = Table(show_header=True, header_style="bold cyan", box=None)
    table.add_column("Name")
    table.add_column("Hosts")
    table.add_column("Created")
    for row in rows:
        table.add_row(
            str(row.get("name", "")),
            str(row.get("host_count", 0)),
            str(row.get("created_at", "")),
        )
    console.print(table)


@app.command("fleet-diff")
def fleet_diff_command(
    context: typer.Context,
    left: Annotated[str, typer.Option("--left", help="Baseline fleet snapshot name.")],
    right: Annotated[
        str,
        typer.Option("--right", help="Comparison fleet snapshot name. If omitted, compares to live."),
    ] = "",
    host: Annotated[list[str] | None, typer.Option("--host")] = None,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Compare fleet snapshots across time, and highlight cross-host drift in the comparison side."""
    service = runtime_from_context(context).service()
    try:
        payload = service.fleet_diff(left=left, right=right, hosts=tuple(host or ()))
    except WsError as error:
        abort(error)
        return
    if as_json:
        typer.echo(json.dumps(payload, indent=2))
        return
    drifts = payload.get("drifts", [])
    spread = payload.get("host_spread", [])
    anomalies = payload.get("anomalies", [])
    left_meta = payload.get("left", {})
    right_meta = payload.get("right", {})
    console.print(
        f"Fleet diff: [bold]{left_meta.get('name','left')}[/bold] -> [bold]{right_meta.get('name','right')}[/bold]"
    )
    console.print(
        f"Host drift items: {len(drifts)} | Cross-host spread signals: {len(spread)} | "
        f"Anomalies: {len(anomalies)}"
    )
    if drifts:
        table = Table(show_header=True, header_style="bold cyan", box=None)
        table.add_column("Host")
        table.add_column("Field")
        table.add_column("Left")
        table.add_column("Right")
        table.add_column("Delta")
        for row in drifts[:20]:
            table.add_row(
                str(row.get("host", "")),
                str(row.get("field", "")),
                str(row.get("left", "")),
                str(row.get("right", "")),
                str(row.get("delta", "")) if row.get("delta") is not None else "-",
            )
        console.print(table)
    if spread:
        spread_table = Table(show_header=True, header_style="bold cyan", box=None)
        spread_table.add_column("Metric")
        spread_table.add_column("Spread")
        spread_table.add_column("Values")
        for item in spread[:10]:
            values = item.get("values", {})
            values_text = (
                ", ".join(f"{host_name}={value}" for host_name, value in values.items())
                if isinstance(values, dict)
                else "-"
            )
            spread_table.add_row(
                str(item.get("field", "")),
                str(item.get("spread", "")),
                _display_safe(values_text),
            )
        console.print(spread_table)
    if anomalies:
        anomaly_table = Table(show_header=True, header_style="bold cyan", box=None)
        anomaly_table.add_column("Type")
        anomaly_table.add_column("Target")
        anomaly_table.add_column("Field")
        anomaly_table.add_column("Severity")
        anomaly_table.add_column("Score")
        anomaly_table.add_column("Reason")
        for item in anomalies[:10]:
            anomaly_table.add_row(
                str(item.get("type", "")),
                str(item.get("host", "-")),
                str(item.get("field", "")),
                str(item.get("severity", "")),
                str(item.get("score", "")),
                _display_safe(str(item.get("reason", ""))),
            )
        console.print(anomaly_table)


@template_app.command("save")
def template_save(
    context: typer.Context,
    name: Annotated[str, typer.Argument(help="Template identifier.")],
    tool: Annotated[Tool, typer.Option("--tool", "-t")],
    name_template: Annotated[str, typer.Option("--name-template")],
    cwd_template: Annotated[str, typer.Option("--cwd-template")],
    project_template: Annotated[str, typer.Option("--project-template")] = "",
    note_template: Annotated[str, typer.Option("--note-template")] = "",
    tag: Annotated[list[str] | None, typer.Option("--tag")] = None,
    logging: Annotated[bool, typer.Option("--logging/--no-logging")] = True,
) -> None:
    service = runtime_from_context(context).service()
    try:
        saved = service.save_template(
            SessionTemplate(
                name=name,
                tool=tool,
                name_template=name_template,
                cwd_template=cwd_template,
                project_template=project_template,
                note_template=note_template,
                tags=tag or [],
                logging_enabled=logging,
            )
        )
    except WsError as error:
        abort(error)
        return
    console.print(f"Saved template: [bold]{saved.name}[/bold]")


@template_app.command("list")
def template_list(
    context: typer.Context,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    service = runtime_from_context(context).service()
    templates = service.list_templates()
    if as_json:
        typer.echo(json.dumps([item.model_dump(mode="json") for item in templates], indent=2))
        return
    table = Table(show_header=True, header_style="bold cyan", box=None)
    table.add_column("Name")
    table.add_column("Tool")
    table.add_column("Name template")
    table.add_column("CWD template")
    for item in templates:
        table.add_row(item.name, item.tool.value, item.name_template, item.cwd_template)
    console.print(table)


@template_app.command("delete")
def template_delete(
    context: typer.Context,
    name: Annotated[str, typer.Argument(help="Template name.")],
) -> None:
    try:
        runtime_from_context(context).service().delete_template(name)
    except WsError as error:
        abort(error)
        return
    console.print(f"Deleted template: [bold]{name}[/bold]")


@profile_app.command("list")
def profile_list(context: typer.Context) -> None:
    """List configured operator profiles."""
    profiles = runtime_from_context(context).config.operator_profiles
    if not profiles:
        console.print("No operator profiles configured.")
        return
    table = Table(show_header=True, header_style="bold cyan", box=None)
    table.add_column("Name")
    table.add_column("Default project")
    table.add_column("Require approvals")
    table.add_column("Allowed actions")
    for item in profiles:
        table.add_row(
            item.name,
            item.default_project or "-",
            "yes" if item.require_approvals else "no",
            ", ".join(item.allowed_actions) or "*",
        )
    console.print(table)


@profile_app.command("select")
def profile_select(
    context: typer.Context,
    name: Annotated[str, typer.Argument(help="Operator profile name.")],
) -> None:
    """Activate an operator profile for future commands."""
    try:
        payload = runtime_from_context(context).service().select_operator_profile(name)
    except WsError as error:
        abort(error)
        return
    console.print(f"Active profile: [bold]{payload['name']}[/bold]")


@profile_app.command("active")
def profile_active(
    context: typer.Context,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Show the active operator profile."""
    payload = runtime_from_context(context).service().active_operator_profile()
    if as_json:
        typer.echo(json.dumps(payload, indent=2))
        return
    if not payload:
        console.print("No active operator profile.")
        return
    console.print(f"Name: [bold]{payload.get('name', '-')}[/bold]")
    console.print(f"Default project: {payload.get('default_project', '-') or '-'}")
    console.print(
        f"Require approvals: {'yes' if payload.get('require_approvals') else 'no'}"
    )
    actions = payload.get("allowed_actions") or []
    console.print(f"Allowed actions: {', '.join(str(item) for item in actions) or '*'}")


@dependency_app.command("list")
def dependency_list(
    context: typer.Context,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """List dependency edges across sessions."""
    graph = runtime_from_context(context).service().dependency_graph()
    if as_json:
        typer.echo(json.dumps(graph, indent=2))
        return
    if not graph:
        console.print("No dependencies recorded.")
        return
    table = Table(show_header=True, header_style="bold cyan", box=None)
    table.add_column("Session")
    table.add_column("Depends on")
    for session, dependencies in sorted(graph.items()):
        table.add_row(session, ", ".join(dependencies) or "-")
    console.print(table)


@dependency_app.command("add")
def dependency_add(
    context: typer.Context,
    session: Annotated[str, typer.Argument(help="Dependent session name.")],
    depends_on: Annotated[str, typer.Argument(help="Upstream dependency session.")],
) -> None:
    """Add a dependency edge."""
    try:
        runtime_from_context(context).service().add_dependency(session, depends_on)
    except WsError as error:
        abort(error)
        return
    console.print(f"Added dependency: [bold]{session}[/bold] -> [bold]{depends_on}[/bold]")


@dependency_app.command("remove")
def dependency_remove(
    context: typer.Context,
    session: Annotated[str, typer.Argument(help="Dependent session name.")],
    depends_on: Annotated[str, typer.Argument(help="Upstream dependency session.")],
) -> None:
    """Remove a dependency edge."""
    try:
        runtime_from_context(context).service().remove_dependency(session, depends_on)
    except WsError as error:
        abort(error)
        return
    console.print(f"Removed dependency: [bold]{session}[/bold] -> [bold]{depends_on}[/bold]")


@dependency_app.command("critical-path")
def dependency_critical_path(
    context: typer.Context,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Show the current longest dependency chain."""
    path = runtime_from_context(context).service().dependency_critical_path()
    if as_json:
        typer.echo(json.dumps(path, indent=2))
        return
    console.print(" -> ".join(path) if path else "No dependency chain found.")


@app.command("policy-simulate")
def policy_simulate_command(
    context: typer.Context,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Simulate current policy impact (archive/SLA/approvals)."""
    payload = runtime_from_context(context).service().policy_simulation()
    if as_json:
        typer.echo(json.dumps(payload, indent=2))
        return
    console.print("[bold cyan]Policy simulation[/bold cyan]")
    console.print(f"Sessions total: {payload.get('sessions_total', 0)}")
    archive_candidates = payload.get("archive_candidates", [])
    console.print(f"Archive candidates: {len(archive_candidates)}")
    approvals = payload.get("approvals", {})
    if isinstance(approvals, dict):
        console.print(f"Approvals enabled: {'yes' if approvals.get('enabled') else 'no'}")


@approval_app.command("issue")
def approval_issue_command(
    context: typer.Context,
    action: Annotated[str, typer.Argument(help="Guarded action context, e.g. delete or federation-action:resume:vm-a.")],
    operator: Annotated[str, typer.Option("--operator")] = "",
    ttl: Annotated[int | None, typer.Option("--ttl", min=30, max=86400)] = None,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Issue a signed and expiring approval token."""
    service = runtime_from_context(context).service()
    try:
        payload = service.issue_approval_token(action=action, operator=operator, ttl_seconds=ttl)
    except WsError as error:
        abort(error)
        return
    if as_json:
        typer.echo(json.dumps(payload, indent=2))
        return
    console.print("[bold cyan]Approval token issued[/bold cyan]")
    console.print(f"Action: {payload.get('action')}")
    console.print(f"Operator: {payload.get('operator')}")
    console.print(f"Expires: {payload.get('expires_at')}")
    console.print(f"Token: {payload.get('token')}")


@app.command("search")
def unified_search_command(
    context: typer.Context,
    query: Annotated[str | None, typer.Argument(help="Query text; supports facets like tool:copilot project:api state:blocked tag:backend.")] = None,
    saved: Annotated[str | None, typer.Option("--saved", help="Run a saved query by name.")] = None,
    limit: Annotated[int, typer.Option("--limit")] = 50,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Search sessions, notes, timelines, and preview content via unified index."""
    service = runtime_from_context(context).service()
    if saved:
        try:
            effective_query = service.get_search_query(saved)
        except WsError as error:
            abort(error)
            return
    else:
        effective_query = (query or "").strip()
    rows = service.unified_search(effective_query, limit=limit)
    if as_json:
        typer.echo(json.dumps(rows, indent=2))
        return
    if not rows:
        console.print("No matches found.")
        return
    table = Table(show_header=True, header_style="bold cyan", box=None)
    table.add_column("Session")
    table.add_column("Tool")
    table.add_column("Project")
    table.add_column("State")
    table.add_column("Match")
    table.add_column("Related")
    for row in rows:
        table.add_row(
            str(row.get("session", "")),
            str(row.get("tool", "")),
            str(row.get("project", "") or "-"),
            str(row.get("state", "")),
            _display_safe(str(row.get("match", ""))),
            ", ".join(str(item) for item in row.get("related_sessions", [])) or "-",
        )
    console.print(table)


@app.command("search-reindex")
def search_reindex_command(
    context: typer.Context,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Rebuild the unified search index."""
    index = runtime_from_context(context).service().rebuild_search_index()
    if as_json:
        typer.echo(json.dumps(index, indent=2))
        return
    console.print(f"Indexed sessions: {len(index)}")


@app.command("search-save")
def search_save_command(
    context: typer.Context,
    name: Annotated[str, typer.Argument(help="Saved query name.")],
    query: Annotated[str, typer.Argument(help="Query with optional facets.")],
) -> None:
    """Save a reusable unified search query."""
    service = runtime_from_context(context).service()
    try:
        saved = service.save_search_query(name, query)
    except WsError as error:
        abort(error)
        return
    console.print(f"Saved query: [bold]{saved['name']}[/bold]")


@app.command("search-saved")
def search_saved_command(
    context: typer.Context,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """List saved unified search queries."""
    rows = runtime_from_context(context).service().list_search_queries()
    if as_json:
        typer.echo(json.dumps(rows, indent=2))
        return
    if not rows:
        console.print("No saved queries.")
        return
    table = Table(show_header=True, header_style="bold cyan", box=None)
    table.add_column("Name")
    table.add_column("Query")
    for row in rows:
        table.add_row(str(row.get("name", "")), _display_safe(str(row.get("query", ""))))
    console.print(table)


@app.command("search-delete")
def search_delete_command(
    context: typer.Context,
    name: Annotated[str, typer.Argument(help="Saved query name.")],
) -> None:
    """Delete a saved unified search query."""
    try:
        runtime_from_context(context).service().delete_search_query(name)
    except WsError as error:
        abort(error)
        return
    console.print(f"Deleted saved query: [bold]{name}[/bold]")


@app.command("drill")
def drill_command(
    context: typer.Context,
    name: Annotated[str, typer.Argument(help="Session name.")],
    action: Annotated[str, typer.Option("--action")] = "",
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Drill into session timeline and output tail."""
    service = runtime_from_context(context).service()
    try:
        payload = service.timeline_drill(name, action=action)
    except WsError as error:
        abort(error)
        return
    if as_json:
        typer.echo(json.dumps(payload, indent=2))
        return
    console.print(f"[bold cyan]Drill[/bold cyan] {name}")
    console.print(f"Output lines: {payload.get('output_line_count', 0)}")
    for line in payload.get("output_tail", [])[-5:]:
        console.print(_display_safe(str(line)))


@app.command("playbook")
def playbook_command(
    context: typer.Context,
    name: Annotated[str, typer.Argument(help="Playbook name.")],
    session: Annotated[str, typer.Option("--session")] = "",
    preview: Annotated[bool, typer.Option("--preview")] = False,
    yes: Annotated[bool, typer.Option("--yes", help="Skip execution confirmation.")] = False,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Run configured remediation playbook commands."""
    service = runtime_from_context(context).service()
    if not preview and not yes and not typer.confirm(f"Run playbook '{name}' now?", default=False):
        console.print("Cancelled.")
        return
    try:
        rows = service.run_playbook(name, session=session, preview=preview)
    except WsError as error:
        abort(error)
        return
    if as_json:
        typer.echo(json.dumps(rows, indent=2))
        return
    table = Table(show_header=True, header_style="bold cyan", box=None)
    table.add_column("Command")
    table.add_column("Code")
    table.add_column("Output")
    for row in rows:
        output = str(row.get("stdout") or row.get("stderr") or "")
        table.add_row(
            " ".join(str(part) for part in row.get("command", [])),
            "preview" if preview else str(row.get("returncode", "")),
            _display_safe(output),
        )
    console.print(table)


@app.command("playbook-list")
def playbook_list_command(
    context: typer.Context,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """List configured and built-in remediation playbooks."""
    rows = runtime_from_context(context).service().list_playbooks()
    if as_json:
        typer.echo(json.dumps(rows, indent=2))
        return
    table = Table(show_header=True, header_style="bold cyan", box=None)
    table.add_column("Name")
    table.add_column("Source")
    table.add_column("Commands")
    table.add_column("Match any")
    for row in rows:
        commands = row.get("commands", [])
        command_preview = "; ".join(" ".join(str(part) for part in command) for command in commands[:2])
        if len(commands) > 2:
            command_preview += f" (+{len(commands) - 2})"
        table.add_row(
            str(row.get("name", "")),
            str(row.get("source", "")),
            command_preview or "-",
            ", ".join(str(item) for item in row.get("match_any", [])) or "-",
        )
    console.print(table)


@app.command("snapshot-diff")
def snapshot_diff_command(
    context: typer.Context,
    left: Annotated[Path, typer.Argument(help="First snapshot/report path.")],
    right: Annotated[Path, typer.Argument(help="Second snapshot/report path.")],
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Compare two JSON/plaintext snapshot files."""
    service = runtime_from_context(context).service()
    try:
        payload = service.snapshot_diff(left.expanduser(), right.expanduser())
    except WsError as error:
        abort(error)
        return
    if as_json:
        typer.echo(json.dumps(payload, indent=2))
        return
    if payload.get("equal"):
        console.print("Snapshots are equal.")
        return
    changes = payload.get("changes", [])
    console.print(f"Changed fields: {', '.join(str(item) for item in changes) or '-'}")


@app.command("audit")
def audit_command(
    context: typer.Context,
    limit: Annotated[int, typer.Option("--limit")] = 200,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Show recent audit lines."""
    lines = runtime_from_context(context).service().read_audit(limit=limit)
    if as_json:
        typer.echo(json.dumps(lines, indent=2))
        return
    if not lines:
        console.print("No audit entries.")
        return
    for line in lines:
        console.print(_display_safe(line))


@app.command("chaos-check")
def chaos_check_command(
    context: typer.Context,
    apply: Annotated[bool, typer.Option("--apply")] = False,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Run health diagnostics with optional synthetic artifact injection."""
    payload = runtime_from_context(context).service().chaos_check(apply=apply)
    if as_json:
        typer.echo(json.dumps(payload, indent=2))
        return
    console.print(
        f"Warnings: {len(payload.get('warnings', []))} | Failures: {len(payload.get('failures', []))}"
    )
    for artifact in payload.get("artifacts", []):
        console.print(f"Artifact: {artifact}")


@app.command("self-heal")
def self_heal_command(
    context: typer.Context,
    apply: Annotated[bool, typer.Option("--apply/--preview")] = False,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Run configured self-heal remediation policies."""
    payload = runtime_from_context(context).service().self_heal(apply=apply)
    if as_json:
        typer.echo(json.dumps(payload, indent=2))
        return
    if not payload.get("enabled"):
        console.print("Self-heal is disabled in configuration.")
        return
    actions = payload.get("actions", [])
    console.print(
        f"Self-heal {'apply' if apply else 'preview'} completed. "
        f"Alerts: {len(payload.get('alerts', []))} | Rules triggered: {len(actions)}"
    )
    if not actions:
        return
    table = Table(show_header=True, header_style="bold cyan", box=None)
    table.add_column("Rule")
    table.add_column("Action")
    table.add_column("Mode")
    table.add_column("OK")
    for row in actions:
        table.add_row(
            str(row.get("rule", "")),
            str(row.get("action", "")),
            str(row.get("mode", "")),
            "yes" if row.get("ok") else "no",
        )
    console.print(table)


@app.command("remediation-chain")
def remediation_chain_command(
    context: typer.Context,
    name: Annotated[str, typer.Argument(help="Remediation chain name.")],
    apply: Annotated[bool, typer.Option("--apply/--preview")] = False,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Run one remediation chain with conditional playbook/archive/repair steps."""
    service = runtime_from_context(context).service()
    try:
        payload = service.run_remediation_chain(name, apply=apply)
    except WsError as error:
        abort(error)
        return
    if as_json:
        typer.echo(json.dumps(payload, indent=2))
        return
    console.print(
        f"Remediation chain {'apply' if apply else 'preview'}: "
        f"{'triggered' if payload.get('triggered') else 'skipped'}"
    )
    steps = payload.get("steps", [])
    if not steps:
        return
    table = Table(show_header=True, header_style="bold cyan", box=None)
    table.add_column("Step")
    table.add_column("OK")
    table.add_column("Detail")
    for row in steps:
        table.add_row(
            str(row.get("step", "")),
            "yes" if row.get("ok") else "no",
            _display_safe(str(row.get("error", "")) or "done"),
        )
    console.print(table)


@app.command("ssh-profiler")
def ssh_profiler_command(
    context: typer.Context,
    latency_ms: Annotated[
        float | None,
        typer.Option("--latency-ms", help="Observed round-trip latency in milliseconds."),
    ] = None,
    width: Annotated[int | None, typer.Option("--width")] = None,
    height: Annotated[int | None, typer.Option("--height")] = None,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Profile terminal/host conditions and recommend the best performance mode."""
    runtime = runtime_from_context(context)
    profile = runtime.config.interface.performance_profile
    terminal = shutil.get_terminal_size(fallback=(120, 35))
    terminal_width = width or terminal.columns
    terminal_height = height or terminal.lines
    observed_latency_ms = max(0.0, float(latency_ms or 0.0))
    ssh_active = bool(os.environ.get("SSH_CONNECTION"))
    try:
        load_1m = float(os.getloadavg()[0])
    except (OSError, AttributeError):
        load_1m = 0.0

    score = 0
    reasons: list[str] = []
    if ssh_active:
        score += 2
        reasons.append("ssh-session")
    if observed_latency_ms >= 1200:
        score += 3
        reasons.append("very-high-latency")
    elif observed_latency_ms >= 800:
        score += 2
        reasons.append("high-latency")
    elif observed_latency_ms >= 450:
        score += 1
        reasons.append("moderate-latency")
    if terminal_width < 100 or terminal_height < 30:
        score += 1
        reasons.append("small-terminal")
    if load_1m >= 8.0:
        score += 2
        reasons.append("high-host-load")
    elif load_1m >= 4.0:
        score += 1
        reasons.append("moderate-host-load")

    recommended = "ssh-safe" if score >= 4 else "balanced" if score >= 2 else "rich"
    base_interval = runtime.config.refresh_interval
    if recommended == "ssh-safe":
        refresh_interval = max(5.0, base_interval * 1.6)
    elif recommended == "balanced":
        refresh_interval = max(2.0, base_interval * 1.2)
    else:
        refresh_interval = max(1.0, base_interval * 0.85)

    payload = {
        "configured_profile": profile,
        "recommended_profile": recommended,
        "auto_safe_mode": recommended == "ssh-safe",
        "refresh_interval_seconds": round(refresh_interval, 2),
        "signals": {
            "ssh_active": ssh_active,
            "latency_ms": observed_latency_ms,
            "terminal_width": terminal_width,
            "terminal_height": terminal_height,
            "load_1m": round(load_1m, 2),
            "score": score,
            "reasons": reasons,
        },
    }
    if as_json:
        typer.echo(json.dumps(payload, indent=2))
        return
    console.print("[bold cyan]SSH performance profiler[/bold cyan]")
    console.print(f"Configured profile: {payload['configured_profile']}")
    console.print(f"Recommended profile: {payload['recommended_profile']}")
    console.print(f"Suggested refresh interval: {payload['refresh_interval_seconds']}s")
    if reasons:
        console.print(f"Signals: {', '.join(reasons)}")


@app.command()
def attach(context: typer.Context, name: str) -> None:
    """Attach or switch to an existing tmux session."""
    try:
        runtime_from_context(context).service().attach(name)
    except WsError as error:
        abort(error)


@app.command()
def resume(context: typer.Context) -> None:
    """Attach to the most relevant detached session."""
    service = runtime_from_context(context).service()
    try:
        service.attach(service.resume_target().name)
    except WsError as error:
        abort(error)


@app.command()
def note(context: typer.Context, name: str, text: str) -> None:
    """Update the note for a ws-owned session."""
    try:
        runtime_from_context(context).service().update_note(name, text)
    except WsError as error:
        abort(error)
    typer.echo(f"Updated note for {name}")


@app.command("note-structured")
def note_structured(
    context: typer.Context,
    name: Annotated[str, typer.Argument(help="Session name.")],
    goal: Annotated[str, typer.Option("--goal")],
    blockers: Annotated[str, typer.Option("--blockers")] = "",
    next_step: Annotated[str, typer.Option("--next-step")] = "",
    owner: Annotated[str, typer.Option("--owner")] = "",
) -> None:
    """Store a structured note (goal, blockers, next step, owner)."""
    payload = (
        f"Goal: {goal.strip()}\n"
        f"Blockers: {blockers.strip() or '-'}\n"
        f"Next step: {next_step.strip() or '-'}\n"
        f"Owner: {owner.strip() or '-'}"
    )
    if len(payload) > 2000:
        abort(WsError("structured note exceeds 2000 characters"))
        return
    try:
        runtime_from_context(context).service().update_note(name, payload)
    except WsError as error:
        abort(error)
        return
    typer.echo(f"Updated structured note for {name}")


@app.command("edit")
def edit_session(
    context: typer.Context,
    name: str,
    tag: Annotated[list[str] | None, typer.Option("--tag")] = None,
    state: Annotated[TaskState | None, typer.Option("--state")] = None,
    input_state: Annotated[InputState | None, typer.Option("--input")] = None,
    project: Annotated[str | None, typer.Option("--project")] = None,
    pin: Annotated[bool | None, typer.Option("--pin/--unpin")] = None,
) -> None:
    """Set tags, task state, or pin status on a ws-owned session."""
    try:
        runtime_from_context(context).service().organize(
            name,
            tags=tag,
            state=state,
            input_state=input_state,
            project=project,
            pinned=pin,
        )
    except WsError as error:
        abort(error)
    typer.echo(f"Updated {name}")


@app.command("organize", hidden=True)
def organize_compatibility(
    context: typer.Context,
    name: str,
    tag: Annotated[list[str] | None, typer.Option("--tag")] = None,
    state: Annotated[TaskState | None, typer.Option("--state")] = None,
    input_state: Annotated[InputState | None, typer.Option("--input")] = None,
    project: Annotated[str | None, typer.Option("--project")] = None,
    pin: Annotated[bool | None, typer.Option("--pin/--unpin")] = None,
) -> None:
    """Compatibility alias for the explicit edit command."""
    edit_session(context, name, tag, state, input_state, project, pin)


@app.command("bulk")
def bulk_command(
    context: typer.Context,
    names: Annotated[list[str], typer.Argument(help="One or more exact session names.")],
    pin: Annotated[bool | None, typer.Option("--pin/--unpin")] = None,
    logging: Annotated[bool | None, typer.Option("--logging/--no-logging")] = None,
    stop_command: Annotated[bool, typer.Option("--stop-command")] = False,
    export_handoff: Annotated[bool, typer.Option("--export-handoff")] = False,
    approval: Annotated[str | None, typer.Option("--approval")] = None,
    approval_token: Annotated[
        list[str] | None,
        typer.Option("--approval-token", help="Signed approval token (repeatable)."),
    ] = None,
) -> None:
    """Apply one operation to many sessions in one command."""
    runtime = runtime_from_context(context)
    service = runtime.service()
    if not names:
        abort(WsError("provide at least one session name"))
        return
    if (
        sum(
            int(value)
            for value in (
                pin is not None,
                logging is not None,
                stop_command,
                export_handoff,
            )
        )
        != 1
    ):
        abort(WsError("choose exactly one bulk operation"))
        return
    if stop_command:
        try:
            require_approval(
                runtime,
                "bulk-stop-command",
                approval,
                approval_tokens=approval_token,
            )
        except WsError as error:
            abort(error)
            return
    for name in names:
        try:
            if pin is not None:
                service.organize(name, pinned=pin)
            elif logging is not None:
                service.set_logging(name, logging)
            elif stop_command:
                service.stop_command(name)
            elif export_handoff:
                details = service.inspect(name)
                console.print(
                    f"{name}: {_display_safe(details.preview.splitlines()[-1] if details.preview else 'no output')}"
                )
        except WsError as error:
            abort(error)
            return
    console.print(f"Bulk operation completed for {len(names)} session(s).")


@app.command("undo")
def undo_command(context: typer.Context) -> None:
    """Undo the most recent supported risky operation."""
    service = runtime_from_context(context).service()
    try:
        result = service.undo_last()
    except WsError as error:
        abort(error)
        return
    console.print(f"Undo applied: {result}")


@app.command("backup")
def backup_command(
    context: typer.Context,
    output: Annotated[Path | None, typer.Option("--output")] = None,
) -> None:
    """Create a portable backup archive for ws state."""
    service = runtime_from_context(context).service()
    try:
        destination = service.backup_state(output)
    except WsError as error:
        abort(error)
        return
    console.print(f"Backup created: [bold]{destination}[/bold]")


@app.command("restore")
def restore_command(
    context: typer.Context,
    archive: Annotated[Path, typer.Argument(help="Backup archive path.")],
) -> None:
    """Restore ws state from a backup archive."""
    service = runtime_from_context(context).service()
    try:
        service.restore_state(archive)
    except WsError as error:
        abort(error)
        return
    console.print(f"Restore completed from: [bold]{archive}[/bold]")


@app.command("federation")
def federation_command(
    context: typer.Context,
    host: Annotated[list[str] | None, typer.Option("--host")] = None,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Aggregate `ws list --json` from remote hosts over SSH."""
    service = runtime_from_context(context).service()
    rows = service.federated_sessions(host)
    if as_json:
        typer.echo(json.dumps(rows, indent=2))
        return
    table = Table(show_header=True, header_style="bold cyan", box=None)
    table.add_column("Host")
    table.add_column("Sessions")
    table.add_column("Status")
    for row in rows:
        sessions = row.get("sessions", [])
        error = row.get("error", "")
        table.add_row(
            str(row.get("host", "")),
            str(len(sessions) if isinstance(sessions, list) else 0),
            _display_safe(str(error or "ok")),
        )
    console.print(table)


@app.command("federation-action")
def federation_action_command(
    context: typer.Context,
    action: Annotated[str, typer.Argument(help="list|resume|health|report|attach")],
    host: Annotated[list[str] | None, typer.Option("--host")] = None,
    arg: Annotated[list[str] | None, typer.Option("--arg", help="Extra remote argument.")]=None,
    approval: Annotated[
        str | None,
        typer.Option("--approval", help="Approval code for guarded remote actions."),
    ] = None,
    approval_token: Annotated[
        list[str] | None,
        typer.Option("--approval-token", help="Signed approval token (repeatable)."),
    ] = None,
    retries: Annotated[
        int | None,
        typer.Option("--retries", min=1, max=5, help="Retry attempts per host."),
    ] = None,
    retry_delay: Annotated[
        float | None,
        typer.Option("--retry-delay", min=0.0, max=5.0, help="Retry delay seconds."),
    ] = None,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Run a supported ws action on one or more federation hosts."""
    service = runtime_from_context(context).service()
    try:
        approval_code, approval_tokens = _split_approval_code_and_tokens(approval, approval_token)
        rows = service.federated_action(
            action,
            hosts=host,
            args=tuple(arg or ()),
            approval_code=approval_code,
            approval_tokens=approval_tokens,
            retry_attempts=retries,
            retry_delay_seconds=retry_delay,
        )
    except WsError as error:
        abort(error)
        return
    if as_json:
        typer.echo(json.dumps(rows, indent=2))
        return
    table = Table(show_header=True, header_style="bold cyan", box=None)
    table.add_column("Host")
    table.add_column("OK")
    table.add_column("Attempts")
    table.add_column("Result")
    for row in rows:
        result = row.get("stdout", "") if row.get("ok") else (
            row.get("failure_summary", "") or row.get("error", "")
        )
        table.add_row(
            str(row.get("host", "")),
            "yes" if row.get("ok") else "no",
            str(row.get("attempt_count", 0)),
            _display_safe(str(result)[:120]),
        )
    console.print(table)
    failures = [row for row in rows if not row.get("ok")]
    if failures and len(failures) < len(rows):
        failure_reasons = ", ".join(
            f"{row.get('host')}: {_display_safe(str(row.get('failure_summary') or row.get('error') or 'failed'))}"
            for row in failures[:3]
        )
        console.print(f"Partial success: {len(rows)-len(failures)}/{len(rows)} hosts succeeded.")
        console.print(f"Failures: {failure_reasons}")


@app.command("federation-capabilities")
def federation_capabilities_command(
    context: typer.Context,
    host: Annotated[list[str] | None, typer.Option("--host")] = None,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Display host capability matrix for remote commands and tools."""
    service = runtime_from_context(context).service()
    rows = service.federation_capability_matrix(hosts=host)
    if as_json:
        typer.echo(json.dumps(rows, indent=2))
        return
    table = Table(show_header=True, header_style="bold cyan", box=None)
    table.add_column("Host")
    table.add_column("Sessions")
    table.add_column("Commands")
    table.add_column("Tools")
    table.add_column("Unavailable reasons")
    for row in rows:
        host_name = str(row.get("host", ""))
        session_count = str(row.get("session_count", 0))
        commands = row.get("commands", {})
        tools = row.get("tools", {})
        command_supported = []
        command_missing = []
        if isinstance(commands, dict):
            for name, payload in commands.items():
                if not isinstance(payload, dict):
                    continue
                if bool(payload.get("supported")):
                    command_supported.append(str(name))
                else:
                    command_missing.append(str(name))
        tool_supported = []
        tool_missing = []
        if isinstance(tools, dict):
            for name, payload in tools.items():
                if not isinstance(payload, dict):
                    continue
                if bool(payload.get("supported")):
                    tool_supported.append(str(name))
                else:
                    tool_missing.append(str(name))
        reasons: list[str] = []
        session_error = str(row.get("session_error", "")).strip()
        if session_error:
            reasons.append(f"sessions: {session_error}")
        if isinstance(commands, dict):
            for name in command_missing:
                payload = commands.get(name)
                if isinstance(payload, dict):
                    reason = str(payload.get("reason", "")).strip()
                    if reason:
                        reasons.append(f"{name}: {reason}")
        if isinstance(tools, dict):
            for name in tool_missing:
                payload = tools.get(name)
                if isinstance(payload, dict):
                    reason = str(payload.get("reason", "")).strip()
                    if reason:
                        reasons.append(f"{name}: {reason}")
        table.add_row(
            host_name,
            session_count,
            (
                f"{len(command_supported)}/{len(command_supported) + len(command_missing)} supported"
                + (f" ({', '.join(sorted(command_supported))})" if command_supported else "")
            ),
            (
                f"{len(tool_supported)}/{len(tool_supported) + len(tool_missing)} supported"
                + (f" ({', '.join(sorted(tool_supported))})" if tool_supported else "")
            ),
            _display_safe("; ".join(reasons[:6]) if reasons else "none"),
        )
    console.print(table)


@app.command("federation-action-plan")
def federation_action_plan_command(
    context: typer.Context,
    action: Annotated[str, typer.Argument(help="list|resume|health|report|attach")],
    host: Annotated[list[str] | None, typer.Option("--host")] = None,
    arg: Annotated[list[str] | None, typer.Option("--arg", help="Extra remote argument.")] = None,
    approval: Annotated[str | None, typer.Option("--approval")] = None,
    approval_token: Annotated[
        list[str] | None,
        typer.Option("--approval-token", help="Signed approval token (repeatable)."),
    ] = None,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Preview remote action blast radius and approval state without execution."""
    service = runtime_from_context(context).service()
    code, tokens = _split_approval_code_and_tokens(approval, approval_token)
    try:
        payload = service.federation_action_plan(
            action,
            hosts=host,
            args=tuple(arg or ()),
            approval_code=code,
            approval_tokens=tokens,
        )
    except WsError as error:
        abort(error)
        return
    if as_json:
        typer.echo(json.dumps(payload, indent=2))
        return
    targets = payload.get("targets", {})
    radius = payload.get("blast_radius", {})
    approval_state = payload.get("approval", {})
    console.print(
        f"Plan: [bold]{action}[/bold] on {targets.get('host_count', 0)} host(s) "
        f"(reachable {targets.get('reachable_hosts', 0)})"
    )
    console.print(
        f"Blast radius: sessions={radius.get('session_count', 0)} "
        f"blocked={radius.get('blocked_sessions', 0)} "
        f"needs-input={radius.get('needs_input_sessions', 0)} "
        f"risk={radius.get('risk_level', 'unknown')}"
    )
    if approval_state.get("required"):
        console.print(
            "Approval: "
            + ("satisfied" if approval_state.get("satisfied") else "missing")
        )
        missing = approval_state.get("missing_contexts", [])
        if missing:
            console.print(f"Missing contexts: {', '.join(str(item) for item in missing)}")


@app.command("report")
def report_command(
    context: typer.Context,
    as_json: Annotated[bool, typer.Option("--json")] = False,
    output: Annotated[
        Path | None,
        typer.Option("--output", help="Write a plaintext handoff report to this file."),
    ] = None,
) -> None:
    """Export operational summary suitable for daily checks."""
    service = runtime_from_context(context).service()
    sessions = service.list_sessions()
    health = service.cached_health_alerts()
    payload = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "sessions": {
            "total": len(sessions),
            "attached": sum(item.runtime is RuntimeState.ATTACHED for item in sessions),
            "detached": sum(item.runtime is RuntimeState.DETACHED for item in sessions),
            "stopped": sum(
                item.runtime in {RuntimeState.STOPPED, RuntimeState.FAILED} for item in sessions
            ),
            "blocked": sum(item.task_state is TaskState.BLOCKED for item in sessions),
            "needs_input": sum(item.input_state is InputState.REQUIRED for item in sessions),
        },
        "warnings": [
            check.model_dump(mode="json")
            for check in health
            if check.status in {HealthStatus.WARN, HealthStatus.FAIL}
        ],
        "stale_sessions": sorted(
            (
                session.name
                for session in sessions
                if session.runtime is RuntimeState.DETACHED
                and session.last_active_at is not None
                and (datetime.now(session.last_active_at.tzinfo) - session.last_active_at).days >= 7
            )
        )[:20],
        "top_projects": sorted(
            {
                session.project
                for session in sessions
                if session.project
            }
        )[:10],
    }
    plaintext = (
        "WORKSPACE OPERATIONS REPORT\n"
        f"Generated: {payload['generated_at']}\n\n"
        f"Sessions: {payload['sessions']['total']} total | "
        f"{payload['sessions']['attached']} attached | "
        f"{payload['sessions']['detached']} detached | "
        f"{payload['sessions']['stopped']} stopped | "
        f"{payload['sessions']['blocked']} blocked | "
        f"{payload['sessions']['needs_input']} needs-input\n"
        f"Health warnings: {len(payload['warnings'])}\n"
        f"Top projects: {', '.join(payload['top_projects']) or '-'}\n"
        f"Stale detached sessions (7d+): {', '.join(payload['stale_sessions']) or '-'}\n"
    )
    if as_json:
        typer.echo(json.dumps(payload, indent=2))
        return
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(plaintext, encoding="utf-8")
        console.print(f"Saved report: [bold]{output}[/bold]")
        return
    console.print("[bold cyan]Workspace operations report[/bold cyan]")
    console.print(plaintext.rstrip())


@app.command("board")
def board_command(
    context: typer.Context,
    project: Annotated[str, typer.Option("--project")] = "",
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Show per-project todo/doing/blocked/done lanes."""
    service = runtime_from_context(context).service()
    board = service.project_board(project=project)
    if as_json:
        payload = {
            key: [session.model_dump(mode="json") for session in sessions]
            for key, sessions in board.items()
        }
        typer.echo(json.dumps(payload, indent=2))
        return
    table = Table(show_header=True, header_style="bold cyan", box=None)
    table.add_column("Lane")
    table.add_column("Count")
    table.add_column("Sessions")
    for key in ("todo", "doing", "blocked", "done"):
        sessions = board[key]
        table.add_row(key, str(len(sessions)), ", ".join(item.name for item in sessions[:8]) or "-")
    console.print(table)


@app.command("archive")
def archive_command(
    context: typer.Context,
    dry_run: Annotated[bool, typer.Option("--dry-run/--apply")] = True,
) -> None:
    """Archive stale completed sessions according to archive policy."""
    service = runtime_from_context(context).service()
    names = service.archive_completed_sessions(dry_run=dry_run)
    mode = "Would archive" if dry_run else "Archived"
    console.print(f"{mode}: {len(names)} session(s)")
    if names:
        console.print(", ".join(names))


@app.command("timeline-global")
def timeline_global_command(
    context: typer.Context,
    action: Annotated[str, typer.Option("--action")] = "",
    limit: Annotated[int, typer.Option("--limit")] = 100,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Show global timeline across sessions."""
    service = runtime_from_context(context).service()
    rows = service.global_timeline(action_filter=action)[: max(1, limit)]
    if as_json:
        payload = [
            {"session": name, **event.model_dump(mode="json")}
            for name, event in rows
        ]
        typer.echo(json.dumps(payload, indent=2))
        return
    table = Table(show_header=True, header_style="bold cyan", box=None)
    table.add_column("Time")
    table.add_column("Session")
    table.add_column("Action")
    table.add_column("Detail")
    for name, event in rows:
        table.add_row(event.timestamp.isoformat(timespec="seconds"), name, event.action, _display_safe(event.detail or "-"))
    console.print(table)


@app.command("suggest")
def suggest_command(
    context: typer.Context,
    name: Annotated[str, typer.Argument(help="Session name")],
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Suggest next troubleshooting steps from recent output patterns."""
    service = runtime_from_context(context).service()
    try:
        details = service.inspect(name)
    except WsError as error:
        abort(error)
        return
    suggestions = service.suggest_fixes(details.preview)
    if as_json:
        typer.echo(json.dumps({"session": name, "suggestions": suggestions}, indent=2))
        return
    if not suggestions:
        console.print("No known failure patterns found.")
        return
    for item in suggestions:
        console.print(f"- {_display_safe(item)}")


@app.command("recover")
def recover_command(
    context: typer.Context,
    repair: Annotated[bool, typer.Option("--repair")] = False,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Check state integrity and optionally repair corrupt auxiliary files."""
    service = runtime_from_context(context).service()
    report = service.integrity_report()
    repaired: dict[str, list[str]] = {}
    if repair:
        repaired = service.repair_integrity()
    payload = {"report": report, "repaired": repaired}
    if as_json:
        typer.echo(json.dumps(payload, indent=2))
        return
    for key, issues in report.items():
        console.print(f"{key}: {len(issues)} issue(s)")
        for issue in issues[:5]:
            console.print(f"  - {_display_safe(issue)}")
    if repair:
        total = sum(len(items) for items in repaired.values())
        console.print(f"Repair actions: {total}")


@app.command("hooks")
def hooks_command(context: typer.Context) -> None:
    """List configured automation hooks."""
    hooks = runtime_from_context(context).config.automation_hooks
    table = Table(show_header=True, header_style="bold cyan", box=None)
    table.add_column("Event")
    table.add_column("Command")
    table.add_column("Timeout")
    for hook in hooks:
        table.add_row(hook.event, " ".join(hook.command), str(hook.timeout_seconds))
    console.print(table)


@app.command("sla")
def sla_command(
    context: typer.Context,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Run SLA rule evaluation and print violations."""
    service = runtime_from_context(context).service()
    checks = service.refresh_health_alerts(force=True)
    sla = next((check for check in checks if check.name == "sla-alerts"), None)
    if sla is None:
        abort(WsError("sla rules are not configured"))
        return
    if as_json:
        typer.echo(json.dumps(sla.model_dump(mode="json"), indent=2))
        return
    console.print(f"SLA status: {sla.status.value}")
    console.print(_display_safe(sla.detail))
    if sla.corrective_action:
        console.print(f"Action: {_display_safe(sla.corrective_action)}")


@app.command()
def rename(context: typer.Context, old_name: str, new_name: str) -> None:
    """Rename a ws-owned session and its metadata atomically."""
    try:
        session = runtime_from_context(context).service().rename(old_name, new_name)
    except WsError as error:
        abort(error)
    typer.echo(f"Renamed {old_name} to {session.name}")


@app.command()
def delete(
    context: typer.Context,
    name: str,
    yes: Annotated[bool, typer.Option("--yes", help="Skip typed confirmation.")] = False,
    approval: Annotated[str | None, typer.Option("--approval")] = None,
    approval_token: Annotated[
        list[str] | None,
        typer.Option("--approval-token", help="Signed approval token (repeatable)."),
    ] = None,
) -> None:
    """Delete a ws-owned session."""
    runtime = runtime_from_context(context)
    try:
        require_approval(runtime, "delete", approval, approval_tokens=approval_token)
    except WsError as error:
        abort(error)
        return
    if not yes:
        confirmation = typer.prompt(f'Type "{name}" to confirm deletion')
        if confirmation != name:
            typer.echo("Cancelled")
            raise typer.Exit(1)
    try:
        runtime.service().delete(name)
    except WsError as error:
        abort(error)
    typer.echo(f"Deleted {name}")


def _redact_report(report: DoctorReport) -> DoctorReport:
    """Redact check details before they reach the terminal/a pipe -- the
    diagnostics-export path already treats these strings as sensitive."""
    return DoctorReport(
        checks=[
            check.model_copy(
                update={
                    "detail": redact_text(check.detail),
                    "corrective_action": redact_text(check.corrective_action),
                }
            )
            for check in report.checks
        ]
    )


def _print_report_table(report: DoctorReport) -> None:
    table = Table(show_header=True, header_style="bold cyan", box=None)
    table.add_column("Check")
    table.add_column("Result")
    table.add_column("Detail")
    table.add_column("Fix")
    for check in report.checks:
        table.add_row(
            check.name,
            check.status.value,
            check.detail,
            check.corrective_action or "-",
        )
    console.print(table)


def _preset_validation_rows(service: SessionService) -> list[tuple[str, HealthStatus, str, str]]:
    rows: list[tuple[str, HealthStatus, str, str]] = []
    for preset in service.list_presets():
        profile = service.config.tools.get(preset.tool)
        if profile is None:
            rows.append(
                (
                    preset.name,
                    HealthStatus.FAIL,
                    f"missing tool profile: {preset.tool.value}",
                    "Add or repair the tool profile in config.toml.",
                )
            )
            continue
        if not profile.enabled:
            rows.append(
                (
                    preset.name,
                    HealthStatus.WARN,
                    f"{preset.tool.value} profile is disabled",
                    f"Enable [tools.{preset.tool.value}] or choose another preset tool.",
                )
            )
            continue
        if not command_available(profile.command):
            rows.append(
                (
                    preset.name,
                    HealthStatus.FAIL,
                    f"command not found: {profile.command[0]}",
                    "Install the CLI or update the preset's tool profile command.",
                )
            )
            continue
        if not preset.cwd.is_dir():
            rows.append(
                (
                    preset.name,
                    HealthStatus.WARN,
                    f"working directory missing: {preset.cwd}",
                    "Update the preset cwd or recreate the directory.",
                )
            )
            continue
        rows.append((preset.name, HealthStatus.PASS, "ready", ""))
    return rows


def _actionable_report(report: DoctorReport) -> DoctorReport:
    checks = [
        check
        for check in report.checks
        if check.status is not HealthStatus.PASS or bool(check.corrective_action)
    ]
    return DoctorReport(checks=checks)


@app.command("ux-audit")
def ux_audit_command(
    context: typer.Context,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Run a lightweight UI/UX consistency audit against the shipped TUI sources."""
    del context
    source_root = Path(__file__).resolve().parent
    tui_source = (source_root / "tui.py").read_text(encoding="utf-8")
    css_source = (source_root / "wf.tcss").read_text(encoding="utf-8")

    checks: list[dict[str, str]] = []

    def add(name: str, ok: bool, detail: str, fix: str = "") -> None:
        checks.append(
            {
                "check": name,
                "status": "pass" if ok else "fail",
                "detail": detail,
                "fix": fix,
            }
        )

    add(
        "theme-pack-size",
        len(THEME_MODES) >= 10,
        f"{len(THEME_MODES)} built-in themes available",
        "Expand THEME_MODES so light/dark/high-contrast options stay diverse.",
    )
    add(
        "button-variant-coverage",
        all(token in css_source for token in (".button-primary", ".button-secondary", ".button-danger")),
        "Primary/secondary/danger button variants detected in wf.tcss",
        "Define missing button variant classes in wf.tcss and apply them consistently.",
    )
    add(
        "motion-controls",
        "action_cycle_motion_preset" in tui_source and "motion_preset" in tui_source,
        "Profile-aware motion controls are wired in the app",
        "Add motion preset actions and persistable interface preference fields.",
    )
    add(
        "contrast-controls",
        "action_toggle_high_contrast" in tui_source and ".high-contrast" in css_source,
        "Explicit high-contrast mode is present",
        "Add a contrast toggle action and high-contrast CSS class rules.",
    )
    add(
        "command-palette-suggestions",
        "Suggested ·" in tui_source and "_palette_rank" in tui_source,
        "Suggested next actions and palette ranking hooks found",
        "Add suggested command rows and ranking boosts for recency/context.",
    )
    add(
        "label-length-outliers",
        not bool(re.search(r'yield Button\("[^"]{41,}"', tui_source)),
        "No button labels longer than 40 characters",
        "Shorten long button labels to keep dialogs scan-friendly in narrow terminals.",
    )
    add(
        "keyboard-hint-layering",
        "hint_level" in tui_source and "action_toggle_hint_level" in tui_source,
        "Compact/verbose keyboard hint layering detected",
        "Add hint-level preference and a toggle action for action-bar density.",
    )
    add(
        "shortcut-discoverability",
        tui_source.count('classes="mode-help"') >= 8 and "command palette" in tui_source.lower(),
        "Mode help rows and command palette entry points are present",
        "Add mode-help rows and expose command palette labels on every major workflow screen.",
    )
    add(
        "button-state-coverage",
        "_run_with_button_loading" in tui_source and tui_source.count("_run_with_button_loading(") >= 4,
        "Button loading-state helper is defined and reused in action handlers",
        "Wrap async-heavy button handlers with _run_with_button_loading for consistent progress feedback.",
    )
    add(
        "empty-state-actionability",
        "Why empty:" in tui_source and "Primary action:" in tui_source and "Secondary shortcut:" in tui_source,
        "Empty states include reason plus primary/secondary next steps",
        "Update empty-state copy to explain why the view is empty and what to do next.",
    )
    add(
        "button-micro-interactions",
        "button-feedback-press" in tui_source and "button-loading" in css_source,
        "Press and loading feedback classes are wired for action buttons",
        "Add button press/loading classes and apply them during actions to improve interaction clarity.",
    )
    add(
        "palette-alias-fuzzy",
        "PALETTE_ALIASES" in tui_source and "SequenceMatcher" in tui_source,
        "Command palette alias and typo-tolerant search hooks detected",
        "Add alias mappings and typo-tolerant ranking so palette discovery survives imperfect queries.",
    )
    add(
        "layout-presets",
        "action_cycle_layout_preset" in tui_source and "interface-layout-preset" in tui_source,
        "Layout presets are available from interface controls and command palette",
        "Expose compact/comfortable/readable layout presets for quick density+text scale switching.",
    )
    add(
        "accent-accessibility",
        "accent-safe" in css_source and '"safe"' in tui_source,
        "Color-blind-safe accent mode is present",
        "Add a safe accent mode with high-contrast friendly foreground/background pairing.",
    )
    add(
        "mode-breadcrumbs",
        "modal-breadcrumb" in tui_source and ".modal-breadcrumb" in css_source,
        "Modal breadcrumb trail styling and usage are present",
        "Add modal breadcrumb text to major workflows so mode transitions stay explicit.",
    )
    add(
        "quick-macro-flow",
        "action_macro_warnings_logs" in tui_source and "Macro: warnings to logs" in tui_source,
        "Quick macro flow is wired to filter warnings then open logs",
        "Add keyboard macro actions for common triage workflows.",
    )
    add(
        "draft-persistence",
        "_form_drafts" in tui_source and "_save_form_draft" in tui_source and "_load_form_draft" in tui_source,
        "Draft persistence helpers exist for form reopen flows",
        "Persist unsent form inputs and restore them on modal reopen.",
    )
    add(
        "grouped-notifications",
        "_notify_grouped" in tui_source and "_notification_groups" in tui_source,
        "Grouped notification helper and counters are present",
        "Collapse repeated alerts into grouped notifications with repeat counters.",
    )
    add(
        "guided-recovery-copy",
        "Recovery commands: ws doctor --actionable" in tui_source,
        "Recovery command guidance is embedded in failure/validation paths",
        "Show copyable recovery commands when validation or startup checks fail.",
    )

    if as_json:
        typer.echo(json.dumps({"checks": checks}, indent=2))
    else:
        table = Table(title="UX Audit")
        table.add_column("Check")
        table.add_column("Status")
        table.add_column("Detail")
        table.add_column("Fix")
        for check in checks:
            status = "[green]pass[/green]" if check["status"] == "pass" else "[red]fail[/red]"
            table.add_row(check["check"], status, check["detail"], check["fix"] or "-")
        console.print(table)
    if any(check["status"] == "fail" for check in checks):
        raise typer.Exit(1)


@app.command("ux-a11y-audit")
def ux_a11y_audit_command(
    context: typer.Context,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Run accessibility-focused audit checks for the shipped TUI."""
    del context
    source_root = Path(__file__).resolve().parent
    tui_source = (source_root / "tui.py").read_text(encoding="utf-8")
    css_source = (source_root / "wf.tcss").read_text(encoding="utf-8")

    checks: list[dict[str, str]] = []

    def add(name: str, ok: bool, detail: str, fix: str = "") -> None:
        checks.append(
            {
                "check": name,
                "status": "pass" if ok else "fail",
                "detail": detail,
                "fix": fix,
            }
        )

    add(
        "high-contrast-toggle",
        "action_toggle_high_contrast" in tui_source and ".high-contrast" in css_source,
        "High-contrast mode toggle and style hooks are present",
        "Add an explicit high-contrast action and CSS class rules.",
    )
    add(
        "adaptive-contrast",
        "action_toggle_auto_contrast" in tui_source and "_terminal_needs_high_contrast" in tui_source,
        "Adaptive contrast checks are wired to terminal capabilities",
        "Implement terminal capability checks and adaptive contrast toggle.",
    )
    add(
        "safe-accent-mode",
        '"safe"' in tui_source and "accent-safe" in css_source,
        "Safe accent mode is available for limited color environments",
        "Add safe accent mode and corresponding CSS class mappings.",
    )
    add(
        "motion-reduction",
        "motion_preset" in tui_source and "WS_NO_ANIMATION" in tui_source and "motion-off" in css_source,
        "Motion can be reduced through presets and environment overrides",
        "Support an off preset plus environment-driven motion disable behavior.",
    )
    add(
        "readable-text-scale",
        '"readable"' in tui_source and ".text-scale-readable" in css_source,
        "Readable text scale mode is supported",
        "Provide compact/comfortable/readable text scale classes and toggles.",
    )
    add(
        "keyboard-discoverability",
        tui_source.count('classes="mode-help"') >= 8 and "action_help" in tui_source,
        "Mode-help rows and keyboard reference surfaces are present",
        "Add mode-help hints and a dedicated keyboard help surface.",
    )
    add(
        "undo-discoverability",
        "action_undo_last_action" in tui_source and "ws undo" in tui_source,
        "Undo path is exposed in TUI and CLI wording",
        "Wire a dedicated undo action and surface the CLI undo command.",
    )

    if as_json:
        typer.echo(json.dumps({"checks": checks}, indent=2))
    else:
        table = Table(title="UX Accessibility Audit")
        table.add_column("Check")
        table.add_column("Status")
        table.add_column("Detail")
        table.add_column("Fix")
        for check in checks:
            status = "[green]pass[/green]" if check["status"] == "pass" else "[red]fail[/red]"
            table.add_row(check["check"], status, check["detail"], check["fix"] or "-")
        console.print(table)
    if any(check["status"] == "fail" for check in checks):
        raise typer.Exit(1)


@app.command()
def doctor(
    context: typer.Context,
    as_json: Annotated[bool, typer.Option("--json")] = False,
    actionable: Annotated[
        bool,
        typer.Option(
            "--actionable",
            help="Show only non-pass checks or checks with corrective actions.",
        ),
    ] = False,
) -> None:
    """Check tmux, agent commands, state, and migration readiness."""
    report = _redact_report(runtime_from_context(context).service().doctor())
    if actionable:
        report = _actionable_report(report)
    if as_json:
        typer.echo(report.model_dump_json(indent=2))
    else:
        if not report.checks:
            console.print("No actionable checks.")
            return
        _print_report_table(report)
    if not report.healthy:
        raise typer.Exit(1)


@app.command()
def health(
    context: typer.Context,
    as_json: Annotated[bool, typer.Option("--json")] = False,
    actionable: Annotated[
        bool,
        typer.Option(
            "--actionable",
            help="Show only non-pass checks or checks with corrective actions.",
        ),
    ] = False,
) -> None:
    """Check disk space, apt updates, reboot flag, dirty repos, and Docker."""
    checks = runtime_from_context(context).service().refresh_health_alerts(force=True)
    report = _redact_report(DoctorReport(checks=checks))
    if actionable:
        report = _actionable_report(report)
    if as_json:
        typer.echo(report.model_dump_json(indent=2))
    else:
        if not report.checks:
            console.print("No actionable checks.")
            return
        _print_report_table(report)
    if not report.healthy:
        raise typer.Exit(1)


@app.command()
def setup(
    context: typer.Context,
    yes: Annotated[
        bool,
        typer.Option("--yes", help="Apply detected defaults without interactive prompts."),
    ] = False,
    force: Annotated[
        bool,
        typer.Option("--force", help="Overwrite an existing config.toml file."),
    ] = False,
) -> None:
    """Interactive first-run wizard for tool profiles."""
    runtime = runtime_from_context(context)
    config_path = runtime.paths.config_file
    if config_path.exists() and not force:
        abort(WsError(f"configuration already exists at {config_path}; use --force to overwrite"))
        return

    try:
        configured_tools = _configured_setup_tools(runtime.config.tools, yes=yes)
    except WsError as error:
        abort(error)
        return
    except ValueError as error:
        abort(WsError(str(error)))
        return

    if not yes and not typer.confirm("Write setup configuration?", default=True):
        console.print("Canceled.")
        return

    try:
        _write_setup_config(config_path, configured_tools)
        runtime.service().mark_onboarding_seen()
    except WsError as error:
        abort(error)
        return

    configured = runtime.config.model_copy(update={"tools": configured_tools})
    default_tool = default_enabled_tool(configured)
    default_label = SETUP_TOOL_LABELS[default_tool] if default_tool else "none"
    console.print(f"Configured: [bold]{config_path}[/bold]")
    console.print(f"Default create tool: [bold]{default_label}[/bold]")


@app.command()
def quickstart(
    context: typer.Context,
    name: Annotated[str, typer.Option("--name", "-n")] = "quickstart",
    preset: Annotated[
        str | None,
        typer.Option("--preset", help="Create from a saved preset profile."),
    ] = None,
    tool: Annotated[Tool | None, typer.Option("--tool", "-t")] = None,
    cwd: Annotated[Path | None, typer.Option("--cwd", "-C")] = None,
    attach: Annotated[
        bool,
        typer.Option("--attach/--no-attach", help="Attach immediately after creation."),
    ] = True,
) -> None:
    """Bootstrap configuration if needed, create a first session, and optionally attach."""
    runtime = runtime_from_context(context)
    service = runtime.service()
    effective_config = runtime.config
    config_path = runtime.paths.config_file
    if not config_path.exists():
        try:
            configured_tools = _configured_setup_tools(runtime.config.tools, yes=True)
            _write_setup_config(config_path, configured_tools)
            service.mark_onboarding_seen()
            effective_config = runtime.config.model_copy(update={"tools": configured_tools})
        except WsError as error:
            abort(error)
            return
        except ValueError as error:
            abort(WsError(str(error)))
            return
        console.print(f"Created setup config: [bold]{config_path}[/bold]")

    preset_model: Preset | None = None
    if preset is not None:
        try:
            preset_model = service.get_preset(preset)
        except WsError as error:
            abort(error)
            return
    selected_tool = tool if tool is not None else (
        preset_model.tool if preset_model else default_enabled_tool(effective_config)
    )
    if selected_tool is None:
        abort(
            WsError(
                "no enabled tool profiles are configured; "
                "enable at least one tool in config.toml"
            )
        )
        return
    profile = effective_config.tools.get(selected_tool)
    if profile is None or not profile.enabled:
        abort(WsError(f"{selected_tool.value} is disabled in configuration"))
        return

    request = CreateRequest(
        name=name,
        tool=selected_tool,
        cwd=(cwd or (preset_model.cwd if preset_model else Path.cwd())).expanduser().resolve(),
        project=preset_model.project if preset_model else "",
        tags=list(preset_model.tags) if preset_model else [],
        logging_enabled=preset_model.logging_enabled if preset_model else True,
        note=f"Quickstart session{f' ({preset_model.name})' if preset_model else ''}",
    )
    try:
        session = service.create(request)
    except WsError as error:
        abort(error)
        return
    console.print(f"Quickstart created: [bold]{session.name}[/bold] in {session.cwd}")
    if attach:
        try:
            service.attach(session.name)
        except WsError as error:
            abort(error)


@onboarding_app.command("reset")
def onboarding_reset(context: typer.Context) -> None:
    """Reset onboarding markers so first-run tutorial/setup can be shown again."""
    paths = runtime_from_context(context).service().paths
    try:
        if paths.onboarding_file.is_symlink():
            raise WsError(f"refusing symlinked onboarding marker: {paths.onboarding_file}")
        paths.onboarding_file.unlink(missing_ok=True)
    except OSError as error:
        abort(WsError(f"unable to reset onboarding marker: {error}"))
        return
    console.print("Onboarding state reset.")


def _migration_manager(context: typer.Context) -> MigrationManager:
    return runtime_from_context(context).migration()


def _print_migration_plan(plan: MigrationPlan) -> None:
    table = Table(show_header=True, header_style="bold cyan", box=None)
    table.add_column("Session")
    table.add_column("Tmux ID")
    table.add_column("Tool")
    table.add_column("Directory")
    table.add_column("Sources")
    table.add_column("Warnings")
    for item in plan.items:
        table.add_row(
            item.name,
            item.tmux_session_id,
            item.tool.value,
            str(item.cwd),
            str(len(item.sources)),
            "; ".join(item.warnings) or "-",
        )
    console.print(table)
    console.print(f"Plan ID: {plan.plan_id}")
    console.print(f"Snapshot: {plan.snapshot_digest}")
    console.print("Notes are included in the private plan file but redacted from this view.")


@migration_app.command("preview")
def migration_preview(
    context: typer.Context,
    sessions: Annotated[
        list[str] | None,
        typer.Option("--session", "-s", help="Exact tmux session name; repeat as needed."),
    ] = None,
    all_sessions: Annotated[
        bool,
        typer.Option("--all", help="Select every eligible unmanaged session."),
    ] = False,
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="Write an approval plan with mode 0600."),
    ] = None,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Build a read-only, exact-ID adoption plan."""
    if all_sessions == bool(sessions):
        error_console.print("[red]Error:[/red] choose --all or at least one --session")
        raise typer.Exit(2)
    try:
        manager = _migration_manager(context)
        plan = manager.preview(None if all_sessions else sessions)
        if output is not None:
            manager.write_plan(plan, output.expanduser())
    except WsError as error:
        abort(error)
    if as_json:
        typer.echo(plan.model_dump_json(indent=2))
    else:
        _print_migration_plan(plan)
    if output is not None:
        console.print(f"Plan written: {output.expanduser()}")


@migration_app.command("validate")
def migration_validate(
    context: typer.Context,
    plan_path: Annotated[Path, typer.Argument(help="Reviewed migration plan JSON file.")],
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Verify that a plan is private, unused, and matches live state."""
    try:
        plan = _migration_manager(context).validate_plan(plan_path.expanduser())
    except WsError as error:
        abort(error)
    if as_json:
        typer.echo(
            json.dumps(
                {
                    "valid": True,
                    "plan_id": str(plan.plan_id),
                    "snapshot_digest": plan.snapshot_digest,
                    "sessions": [
                        {
                            "name": item.name,
                            "tmux_session_id": item.tmux_session_id,
                            "warnings": item.warnings,
                        }
                        for item in plan.items
                    ],
                },
                indent=2,
            )
        )
        return
    _print_migration_plan(plan)
    console.print(f"Plan is current and ready for explicit apply: {len(plan.items)} sessions")


@migration_app.command("apply")
def migration_apply(
    context: typer.Context,
    plan_path: Annotated[Path, typer.Argument(help="Reviewed migration plan JSON file.")],
    approve: Annotated[
        bool,
        typer.Option("--approve", help="Approve adoption of every exact session in the plan."),
    ] = False,
) -> None:
    """Adopt sessions only when the reviewed snapshot is unchanged."""
    if not approve:
        error_console.print("[red]Error:[/red] refusing migration without --approve")
        raise typer.Exit(2)
    try:
        journal = _migration_manager(context).apply(plan_path.expanduser())
    except WsError as error:
        abort(error)
    console.print(f"Applied migration {journal.migration_id}: {len(journal.items)} sessions")


@migration_app.command("status")
def migration_status(
    context: typer.Context,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Show migration journals without changing sessions."""
    try:
        journals = _migration_manager(context).status()
    except WsError as error:
        abort(error)
    if as_json:
        typer.echo(json.dumps([item.model_dump(mode="json") for item in journals], indent=2))
        return
    table = Table(show_header=True, header_style="bold cyan", box=None)
    table.add_column("Migration")
    table.add_column("Status")
    table.add_column("Sessions")
    table.add_column("Updated")
    for journal in journals:
        table.add_row(
            str(journal.migration_id),
            journal.status,
            str(len(journal.items)),
            journal.updated_at.isoformat(),
        )
    console.print(table)


@migration_app.command("rollback")
def migration_rollback(
    context: typer.Context,
    migration_id: Annotated[UUID, typer.Argument(help="Applied migration ID.")],
    approve: Annotated[
        bool,
        typer.Option("--approve", help="Approve removal of this migration's ownership records."),
    ] = False,
) -> None:
    """Return an unchanged migration batch to unmanaged status."""
    if not approve:
        error_console.print("[red]Error:[/red] refusing rollback without --approve")
        raise typer.Exit(2)
    try:
        journal = _migration_manager(context).rollback(migration_id)
    except WsError as error:
        abort(error)
    console.print(f"Rolled back migration {journal.migration_id}: {len(journal.items)} sessions")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
