"""Typer command line interface and Textual application entry point."""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated
from uuid import UUID

import typer
from rich.console import Console
from rich.table import Table

from workspace_session_manager import __version__
from workspace_session_manager.config import AppConfig, load_config
from workspace_session_manager.errors import WsError
from workspace_session_manager.legacy import LegacyMetadataReader
from workspace_session_manager.migration import MigrationManager, MigrationPlan
from workspace_session_manager.models import (
    CreateRequest,
    DoctorReport,
    InputState,
    Preset,
    SessionView,
    TaskState,
    Tool,
)
from workspace_session_manager.paths import AppPaths
from workspace_session_manager.security import redact_text
from workspace_session_manager.service import SessionService
from workspace_session_manager.store import MetadataStore
from workspace_session_manager.tmux import TmuxBackend
from workspace_session_manager.tui import WsApp

app = typer.Typer(
    name="ws",
    help="Manage persistent AI and shell sessions with tmux.",
    no_args_is_help=False,
    invoke_without_command=True,
    pretty_exceptions_enable=False,
)
console = Console()
error_console = Console(stderr=True)
migration_app = typer.Typer(help="Preview, apply, inspect, and roll back session adoption.")
app.add_typer(migration_app, name="migrate")
preset_app = typer.Typer(help="Save, list, and delete create-session presets.")
app.add_typer(preset_app, name="preset")


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
    console.print(f"Note: {session.note or '-'}")
    console.rule("Sanitized preview")
    console.print(details.preview or "No pane output")


@app.command()
def create(
    context: typer.Context,
    name: Annotated[str, typer.Option("--name", "-n")],
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
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
    attach: Annotated[bool, typer.Option("--attach")] = False,
) -> None:
    """Create a detached, persistent ws-owned session."""
    if from_preset is not None and from_session is not None:
        abort(WsError("--from-preset and --from-session cannot be combined"))
        return
    service = runtime_from_context(context).service()
    template: Preset | SessionView | None = None
    try:
        if from_preset is not None:
            template = service.get_preset(from_preset)
        elif from_session is not None:
            template = service.get(from_session)
    except WsError as error:
        abort(error)
        return
    resolved_tool = tool if tool is not None else (template.tool if template else None)
    if resolved_tool is None:
        abort(WsError("--tool is required unless --from-preset/--from-session supplies one"))
        return
    resolved_cwd = cwd if cwd is not None else (template.cwd if template else Path.cwd())
    request = CreateRequest(
        name=name,
        tool=resolved_tool,
        cwd=resolved_cwd.expanduser().resolve(),
        project=project or (template.project if template else ""),
        note=note,
        tags=tag if tag is not None else (list(template.tags) if template else []),
        logging_enabled=logging
        if logging is not None
        else (template.logging_enabled if template else True),
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
    presets = runtime_from_context(context).service().list_presets()
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
            preset.project,
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
) -> None:
    """Delete a ws-owned session."""
    if not yes:
        confirmation = typer.prompt(f'Type "{name}" to confirm deletion')
        if confirmation != name:
            typer.echo("Cancelled")
            raise typer.Exit(1)
    try:
        runtime_from_context(context).service().delete(name)
    except WsError as error:
        abort(error)
    typer.echo(f"Deleted {name}")


def _redact_report(report: DoctorReport) -> DoctorReport:
    """Redact check details before they reach the terminal/a pipe -- the
    diagnostics-export path already treats these strings as sensitive."""
    return DoctorReport(
        checks=[
            check.model_copy(update={"detail": redact_text(check.detail)})
            for check in report.checks
        ]
    )


@app.command()
def doctor(
    context: typer.Context,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Check tmux, agent commands, state, and migration readiness."""
    report = _redact_report(runtime_from_context(context).service().doctor())
    if as_json:
        typer.echo(report.model_dump_json(indent=2))
    else:
        table = Table(show_header=True, header_style="bold cyan", box=None)
        table.add_column("Check")
        table.add_column("Result")
        table.add_column("Detail")
        for check in report.checks:
            table.add_row(check.name, check.status.value, check.detail)
        console.print(table)
    if not report.healthy:
        raise typer.Exit(1)


@app.command()
def health(
    context: typer.Context,
    as_json: Annotated[bool, typer.Option("--json")] = False,
    fix: Annotated[
        str | None,
        typer.Option(
            "--fix",
            help="Apply the fix for one fixable check (zombie-sessions or "
            "orphaned-logs), then show the refreshed report.",
        ),
    ] = None,
) -> None:
    """Check disk space, apt updates, reboot flag, dirty repos, and Docker."""
    service = runtime_from_context(context).service()
    if fix is not None:
        try:
            service.apply_health_fix(fix)
        except WsError as error:
            abort(error)
            return
    checks = service.refresh_health_alerts(force=True)
    report = _redact_report(DoctorReport(checks=checks))
    if as_json:
        typer.echo(report.model_dump_json(indent=2))
    else:
        table = Table(show_header=True, header_style="bold cyan", box=None)
        table.add_column("Check")
        table.add_column("Result")
        table.add_column("Detail")
        for check in report.checks:
            table.add_row(check.name, check.status.value, check.detail)
        console.print(table)
    if not report.healthy:
        raise typer.Exit(1)


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
