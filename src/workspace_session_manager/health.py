"""Ambient VM health checks: apt updates, reboot flag, dirty repos, Docker.

Each check is isolated and non-fatal: a missing tool, a daemon that is not
running, or a permission error becomes an INFO/WARN `HealthCheck`, never an
exception. No check here is safe to call from a startup path — callers must
run these from a background worker, never synchronously on first paint.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Mapping, Sequence, Set
from datetime import datetime, timedelta
from pathlib import Path

from workspace_session_manager.models import HealthCheck, HealthStatus, SessionMetadata
from workspace_session_manager.tmux import Runner, subprocess_runner


def reboot_required_check(flag_path: Path = Path("/var/run/reboot-required")) -> HealthCheck:
    if flag_path.is_file():
        return HealthCheck(
            name="reboot-required",
            status=HealthStatus.WARN,
            detail=f"{flag_path} present",
            corrective_action="Reboot the host when convenient.",
        )
    return HealthCheck(name="reboot-required", status=HealthStatus.PASS, detail="no reboot pending")


def apt_updates_check(
    runner: Runner = subprocess_runner,
    *,
    timeout: float = 5.0,
    apt_check: Path = Path("/usr/lib/update-notifier/apt_check.py"),
) -> HealthCheck:
    if not apt_check.is_file():
        return HealthCheck(
            name="apt-updates",
            status=HealthStatus.INFO,
            detail="apt update-notifier not available on this system",
        )
    try:
        result = runner((str(apt_check),), capture=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as error:
        return HealthCheck(
            name="apt-updates",
            status=HealthStatus.INFO,
            detail=f"apt check failed: {error}",
        )
    # apt_check.py writes its "updates;security_updates" counts to stderr, not stdout.
    parsed = _parse_apt_counts(result.stderr) or _parse_apt_counts(result.stdout)
    if parsed is None:
        raw = (result.stderr or result.stdout).strip()[:80]
        return HealthCheck(
            name="apt-updates",
            status=HealthStatus.INFO,
            detail=f"unrecognized apt_check.py output: {raw!r}",
        )
    total, security = parsed
    if total == 0:
        return HealthCheck(
            name="apt-updates", status=HealthStatus.PASS, detail="no updates pending"
        )
    if security > 0:
        return HealthCheck(
            name="apt-updates",
            status=HealthStatus.WARN,
            detail=f"{total} update(s) pending, {security} security",
            corrective_action="Run apt update && apt upgrade.",
        )
    return HealthCheck(
        name="apt-updates",
        status=HealthStatus.INFO,
        detail=f"{total} update(s) pending",
    )


def _parse_apt_counts(stdout: str) -> tuple[int, int] | None:
    parts = stdout.strip().split(";")
    if len(parts) != 2:
        return None
    try:
        return int(parts[0]), int(parts[1])
    except ValueError:
        return None


def docker_containers_check(
    runner: Runner = subprocess_runner,
    *,
    timeout: float = 5.0,
) -> HealthCheck:
    if shutil.which("docker") is None:
        return HealthCheck(
            name="docker-containers", status=HealthStatus.INFO, detail="docker not installed"
        )
    try:
        result = runner(("docker", "ps", "--format", "{{.Names}}"), capture=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as error:
        return HealthCheck(
            name="docker-containers",
            status=HealthStatus.INFO,
            detail=f"docker unavailable: {error}",
        )
    if result.returncode != 0:
        return HealthCheck(
            name="docker-containers",
            status=HealthStatus.INFO,
            detail="docker unavailable (daemon not running or permission denied)",
        )
    names = [line for line in result.stdout.splitlines() if line.strip()]
    return HealthCheck(
        name="docker-containers",
        status=HealthStatus.INFO,
        detail=f"{len(names)} running" if names else "none running",
    )


def git_dirty_repos_check(
    scan_roots: Sequence[Path],
    *,
    runner: Runner = subprocess_runner,
    budget: int = 20,
    timeout: float = 5.0,
) -> HealthCheck:
    if shutil.which("git") is None:
        return HealthCheck(name="git-dirty", status=HealthStatus.INFO, detail="git not installed")

    repos: list[Path] = []
    for root in scan_roots:
        if not root.is_dir():
            continue
        try:
            children = sorted(root.iterdir())
        except OSError:
            continue
        for child in children:
            if len(repos) >= budget:
                break
            if (child / ".git").exists():
                repos.append(child)
        if len(repos) >= budget:
            break

    dirty: list[str] = []
    for repo in repos:
        try:
            result = runner(
                ("git", "-C", str(repo), "status", "--porcelain"),
                capture=True,
                timeout=timeout,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if result.returncode == 0 and result.stdout.strip():
            dirty.append(repo.name)

    if not dirty:
        return HealthCheck(
            name="git-dirty", status=HealthStatus.PASS, detail=f"{len(repos)} repo(s) clean"
        )
    return HealthCheck(
        name="git-dirty",
        status=HealthStatus.WARN,
        detail=f"{len(dirty)} dirty repo(s): {', '.join(dirty[:5])}"
        + (f", and {len(dirty) - 5} more" if len(dirty) > 5 else ""),
        corrective_action="Commit or stash pending changes.",
    )


def _format_names(names: Sequence[str], *, limit: int = 5) -> str:
    shown = ", ".join(sorted(names)[:limit])
    remaining = len(names) - limit
    return shown + (f", and {remaining} more" if remaining > 0 else "")


def _stale_records(
    records: Mapping[str, SessionMetadata],
    live_names: Set[str],
    *,
    now: datetime,
    threshold: timedelta,
    want_live: bool,
) -> list[SessionMetadata]:
    return [
        record
        for name, record in records.items()
        if (name in live_names) == want_live
        and now - (record.last_attached_at or record.updated_at) > threshold
    ]


def zombie_sessions_check(
    records: Mapping[str, SessionMetadata],
    live_names: Set[str],
    *,
    now: datetime,
    stale_after: timedelta,
) -> HealthCheck:
    """Flag ws-owned metadata whose tmux session is gone and has sat untouched."""
    stale = _stale_records(records, live_names, now=now, threshold=stale_after, want_live=False)
    if not stale:
        return HealthCheck(
            name="zombie-sessions", status=HealthStatus.PASS, detail="no stale stopped sessions"
        )
    days = stale_after.days
    return HealthCheck(
        name="zombie-sessions",
        status=HealthStatus.WARN,
        detail=(
            f"{len(stale)} stopped session(s) untouched for {days}+ days: "
            f"{_format_names([record.name for record in stale])}"
        ),
        corrective_action=(
            "Review with `ws list --all` and `ws delete <name>` for ones no longer needed."
        ),
        fixable=True,
        affected=[record.name for record in stale],
    )


def idle_live_sessions_check(
    records: Mapping[str, SessionMetadata],
    live_names: Set[str],
    *,
    now: datetime,
    idle_after: timedelta,
) -> HealthCheck:
    """Flag sessions that are still live in tmux but haven't been touched in a long time."""
    idle = _stale_records(records, live_names, now=now, threshold=idle_after, want_live=True)
    if not idle:
        return HealthCheck(
            name="idle-sessions", status=HealthStatus.PASS, detail="no idle sessions"
        )
    days = idle_after.days
    return HealthCheck(
        name="idle-sessions",
        status=HealthStatus.WARN,
        detail=(
            f"{len(idle)} live session(s) untouched for {days}+ days: "
            f"{_format_names([record.name for record in idle])}"
        ),
        corrective_action=(
            "Attach and finish up, or `ws delete <name>` to reclaim the tmux session."
        ),
    )


def missing_cwd_check(
    records: Mapping[str, SessionMetadata],
    live_names: Set[str],
) -> HealthCheck:
    """Flag stopped sessions whose recorded cwd no longer exists.

    tmux silently falls back to the process's default directory when `-c`
    points at a missing path, so resuming one of these sessions would land
    in the wrong place with no error -- this surfaces it ahead of time.
    """
    missing = [
        record
        for name, record in records.items()
        if name not in live_names and not record.cwd.is_dir()
    ]
    if not missing:
        return HealthCheck(
            name="missing-cwd", status=HealthStatus.PASS, detail="no missing working directories"
        )
    return HealthCheck(
        name="missing-cwd",
        status=HealthStatus.WARN,
        detail=(
            f"{len(missing)} stopped session(s) with a missing working directory: "
            f"{_format_names([record.name for record in missing])}"
        ),
        corrective_action=(
            "Resuming would silently fall back to the home directory; "
            "`ws delete <name>` if the session is no longer needed."
        ),
    )


def orphaned_logs_check(
    logs_dir: Path,
    known_record_ids: Set[str],
    *,
    now: datetime,
    min_age: timedelta,
) -> HealthCheck:
    """Flag saved log files with no matching session metadata record."""
    if not logs_dir.is_dir():
        return HealthCheck(
            name="orphaned-logs", status=HealthStatus.PASS, detail="no log directory"
        )
    orphans: list[Path] = []
    for path in logs_dir.glob("*.log"):
        if path.stem in known_record_ids:
            continue
        try:
            modified_at = datetime.fromtimestamp(path.stat().st_mtime, tz=now.tzinfo)
        except OSError:
            continue
        if now - modified_at >= min_age:
            orphans.append(path)
    if not orphans:
        return HealthCheck(
            name="orphaned-logs", status=HealthStatus.PASS, detail="no orphaned log files"
        )
    total_bytes = sum(path.stat().st_size for path in orphans if path.exists())
    return HealthCheck(
        name="orphaned-logs",
        status=HealthStatus.WARN,
        detail=(
            f"{len(orphans)} orphaned log file(s) ({total_bytes // 1024} KB) "
            "with no matching session"
        ),
        corrective_action=f"Safe to delete manually from {logs_dir}.",
        fixable=True,
        affected=[str(path) for path in orphans],
    )
