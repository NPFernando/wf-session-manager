import os
import subprocess
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

from workspace_session_manager.health import (
    apt_updates_check,
    docker_containers_check,
    git_dirty_repos_check,
    idle_live_sessions_check,
    missing_cwd_check,
    orphaned_logs_check,
    reboot_required_check,
    zombie_sessions_check,
)
from workspace_session_manager.models import HealthStatus, SessionMetadata, Tool


class ScriptedRunner:
    """A Runner whose response depends on the invoked command, for tests that
    need different fake subprocess behavior per external tool."""

    def __init__(
        self, respond: Callable[[Sequence[str]], subprocess.CompletedProcess[str]]
    ) -> None:
        self._respond = respond
        self.calls: list[tuple[str, ...]] = []

    def __call__(
        self,
        args: Sequence[str],
        *,
        capture: bool = True,
        timeout: float | None = 5.0,
    ) -> subprocess.CompletedProcess[str]:
        del capture, timeout
        self.calls.append(tuple(args))
        return self._respond(args)


def _completed(args: Sequence[str], *, stdout: str = "", stderr: str = "", returncode: int = 0):
    return subprocess.CompletedProcess(
        args=args, returncode=returncode, stdout=stdout, stderr=stderr
    )


def test_reboot_required_check_pass_when_flag_absent(tmp_path: Path) -> None:
    check = reboot_required_check(tmp_path / "reboot-required")
    assert check.status is HealthStatus.PASS


def test_reboot_required_check_warns_when_flag_present(tmp_path: Path) -> None:
    flag = tmp_path / "reboot-required"
    flag.write_text("*** System restart required ***\n", encoding="utf-8")
    check = reboot_required_check(flag)
    assert check.status is HealthStatus.WARN
    assert check.corrective_action


def test_apt_updates_check_missing_tool_is_info(tmp_path: Path) -> None:
    check = apt_updates_check(apt_check=tmp_path / "does-not-exist.py")
    assert check.status is HealthStatus.INFO


def test_apt_updates_check_parses_stderr_counts(tmp_path: Path) -> None:
    fake = tmp_path / "apt_check.py"
    fake.write_text("#!/bin/true\n", encoding="utf-8")
    fake.chmod(0o755)
    runner = ScriptedRunner(lambda args: _completed(args, stderr="0;0"))
    check = apt_updates_check(runner, apt_check=fake)
    assert check.status is HealthStatus.PASS


def test_apt_updates_check_warns_on_security_updates(tmp_path: Path) -> None:
    fake = tmp_path / "apt_check.py"
    fake.write_text("#!/bin/true\n", encoding="utf-8")
    fake.chmod(0o755)
    runner = ScriptedRunner(lambda args: _completed(args, stderr="5;2"))
    check = apt_updates_check(runner, apt_check=fake)
    assert check.status is HealthStatus.WARN
    assert "security" in check.detail


def test_apt_updates_check_info_when_only_ordinary_updates(tmp_path: Path) -> None:
    fake = tmp_path / "apt_check.py"
    fake.write_text("#!/bin/true\n", encoding="utf-8")
    fake.chmod(0o755)
    runner = ScriptedRunner(lambda args: _completed(args, stderr="3;0"))
    check = apt_updates_check(runner, apt_check=fake)
    assert check.status is HealthStatus.INFO


def test_apt_updates_check_isolates_subprocess_failure(tmp_path: Path) -> None:
    fake = tmp_path / "apt_check.py"
    fake.write_text("#!/bin/true\n", encoding="utf-8")
    fake.chmod(0o755)

    def raise_timeout(
        args: Sequence[str], *, capture: bool = True, timeout: float | None = 5.0
    ) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd=list(args), timeout=5.0)

    check = apt_updates_check(raise_timeout, apt_check=fake)
    assert check.status is HealthStatus.INFO


def test_docker_containers_check_counts_running(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/docker")
    runner = ScriptedRunner(lambda args: _completed(args, stdout="a\nb\nc\n"))
    check = docker_containers_check(runner)
    assert check.status is HealthStatus.INFO
    assert "3 running" in check.detail


def test_docker_containers_check_none_installed(monkeypatch) -> None:
    monkeypatch.setattr("shutil.which", lambda name: None)
    check = docker_containers_check()
    assert check.status is HealthStatus.INFO
    assert "not installed" in check.detail


def test_docker_containers_check_daemon_down_is_info(monkeypatch) -> None:
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/docker")
    runner = ScriptedRunner(lambda args: _completed(args, returncode=1, stderr="permission denied"))
    check = docker_containers_check(runner)
    assert check.status is HealthStatus.INFO


def test_git_dirty_repos_check_clean(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/git")
    repo = tmp_path / "repo-one"
    (repo / ".git").mkdir(parents=True)
    runner = ScriptedRunner(lambda args: _completed(args, stdout=""))
    check = git_dirty_repos_check([tmp_path], runner=runner)
    assert check.status is HealthStatus.PASS


def test_git_dirty_repos_check_finds_dirty_repo(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/git")
    repo = tmp_path / "repo-one"
    (repo / ".git").mkdir(parents=True)
    runner = ScriptedRunner(lambda args: _completed(args, stdout=" M some-file.py\n"))
    check = git_dirty_repos_check([tmp_path], runner=runner)
    assert check.status is HealthStatus.WARN
    assert "repo-one" in check.detail


def test_git_dirty_repos_check_respects_budget(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/git")
    for index in range(5):
        (tmp_path / f"repo-{index}" / ".git").mkdir(parents=True)
    runner = ScriptedRunner(lambda args: _completed(args, stdout=""))
    check = git_dirty_repos_check([tmp_path], runner=runner, budget=2)
    assert "2 repo(s)" in check.detail
    assert len(runner.calls) == 2


def test_git_dirty_repos_check_no_git_installed(monkeypatch) -> None:
    monkeypatch.setattr("shutil.which", lambda name: None)
    check = git_dirty_repos_check([Path("/tmp")])
    assert check.status is HealthStatus.INFO


def test_git_dirty_repos_check_isolates_per_repo_failure(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/git")
    (tmp_path / "repo-one" / ".git").mkdir(parents=True)

    def raise_error(
        args: Sequence[str], *, capture: bool = True, timeout: float | None = 5.0
    ) -> subprocess.CompletedProcess[str]:
        raise OSError("boom")

    check = git_dirty_repos_check([tmp_path], runner=raise_error)
    assert check.status is HealthStatus.PASS


def _record(
    name: str, *, last_attached_at: datetime, cwd: Path = Path("/tmp")
) -> SessionMetadata:
    return SessionMetadata(
        tmux_session_id="$1",
        name=name,
        tool=Tool.CLAUDE,
        cwd=cwd,
        last_attached_at=last_attached_at,
    )


def test_zombie_sessions_check_pass_when_no_stale_stopped_sessions() -> None:
    now = datetime.now(UTC)
    records = {"claude-fresh": _record("claude-fresh", last_attached_at=now)}
    check = zombie_sessions_check(
        records, live_names=set(), now=now, stale_after=timedelta(days=14)
    )
    assert check.status is HealthStatus.PASS


def test_zombie_sessions_check_warns_on_stale_stopped_session() -> None:
    now = datetime.now(UTC)
    old = now - timedelta(days=20)
    records = {"claude-old": _record("claude-old", last_attached_at=old)}
    check = zombie_sessions_check(
        records, live_names=set(), now=now, stale_after=timedelta(days=14)
    )
    assert check.status is HealthStatus.WARN
    assert "claude-old" in check.detail
    assert check.corrective_action


def test_zombie_sessions_check_ignores_live_sessions() -> None:
    now = datetime.now(UTC)
    old = now - timedelta(days=20)
    records = {"claude-old": _record("claude-old", last_attached_at=old)}
    check = zombie_sessions_check(
        records, live_names={"claude-old"}, now=now, stale_after=timedelta(days=14)
    )
    assert check.status is HealthStatus.PASS


def test_zombie_sessions_check_truncates_long_lists() -> None:
    now = datetime.now(UTC)
    old = now - timedelta(days=20)
    records = {
        f"claude-old-{index}": _record(f"claude-old-{index}", last_attached_at=old)
        for index in range(7)
    }
    check = zombie_sessions_check(
        records, live_names=set(), now=now, stale_after=timedelta(days=14)
    )
    assert "7 stopped" in check.detail
    assert "2 more" in check.detail


def test_zombie_sessions_check_is_fixable_with_full_affected_list() -> None:
    now = datetime.now(UTC)
    old = now - timedelta(days=20)
    records = {
        f"claude-old-{index}": _record(f"claude-old-{index}", last_attached_at=old)
        for index in range(7)
    }
    check = zombie_sessions_check(
        records, live_names=set(), now=now, stale_after=timedelta(days=14)
    )
    assert check.fixable
    assert set(check.affected) == set(records)


def test_zombie_sessions_check_pass_is_not_fixable() -> None:
    now = datetime.now(UTC)
    records = {"claude-fresh": _record("claude-fresh", last_attached_at=now)}
    check = zombie_sessions_check(
        records, live_names=set(), now=now, stale_after=timedelta(days=14)
    )
    assert not check.fixable
    assert check.affected == []


def test_idle_live_sessions_check_pass_when_none_idle() -> None:
    now = datetime.now(UTC)
    records = {"claude-active": _record("claude-active", last_attached_at=now)}
    check = idle_live_sessions_check(
        records, live_names={"claude-active"}, now=now, idle_after=timedelta(days=30)
    )
    assert check.status is HealthStatus.PASS


def test_idle_live_sessions_check_warns_on_idle_live_session() -> None:
    now = datetime.now(UTC)
    old = now - timedelta(days=45)
    records = {"claude-idle": _record("claude-idle", last_attached_at=old)}
    check = idle_live_sessions_check(
        records, live_names={"claude-idle"}, now=now, idle_after=timedelta(days=30)
    )
    assert check.status is HealthStatus.WARN
    assert "claude-idle" in check.detail


def test_idle_live_sessions_check_ignores_stopped_sessions() -> None:
    now = datetime.now(UTC)
    old = now - timedelta(days=45)
    records = {"claude-idle": _record("claude-idle", last_attached_at=old)}
    check = idle_live_sessions_check(
        records, live_names=set(), now=now, idle_after=timedelta(days=30)
    )
    assert check.status is HealthStatus.PASS


def test_idle_live_sessions_check_is_never_fixable() -> None:
    now = datetime.now(UTC)
    old = now - timedelta(days=45)
    records = {"claude-idle": _record("claude-idle", last_attached_at=old)}
    check = idle_live_sessions_check(
        records, live_names={"claude-idle"}, now=now, idle_after=timedelta(days=30)
    )
    assert check.status is HealthStatus.WARN
    assert not check.fixable
    assert check.affected == []


def test_orphaned_logs_check_pass_when_directory_missing(tmp_path: Path) -> None:
    check = orphaned_logs_check(
        tmp_path / "missing",
        known_record_ids=set(),
        now=datetime.now(UTC),
        min_age=timedelta(hours=1),
    )
    assert check.status is HealthStatus.PASS


def test_orphaned_logs_check_pass_when_all_logs_known(tmp_path: Path) -> None:
    log_path = tmp_path / "abc.log"
    log_path.write_text("output", encoding="utf-8")
    check = orphaned_logs_check(
        tmp_path, known_record_ids={"abc"}, now=datetime.now(UTC), min_age=timedelta(hours=1)
    )
    assert check.status is HealthStatus.PASS


def test_orphaned_logs_check_warns_on_unknown_aged_log(tmp_path: Path) -> None:
    log_path = tmp_path / "orphan.log"
    log_path.write_text("output", encoding="utf-8")
    old_time = (datetime.now(UTC) - timedelta(days=2)).timestamp()
    os.utime(log_path, (old_time, old_time))
    check = orphaned_logs_check(
        tmp_path, known_record_ids=set(), now=datetime.now(UTC), min_age=timedelta(hours=1)
    )
    assert check.status is HealthStatus.WARN
    assert "1 orphaned" in check.detail
    assert check.corrective_action


def test_orphaned_logs_check_respects_min_age_grace_period(tmp_path: Path) -> None:
    log_path = tmp_path / "fresh.log"
    log_path.write_text("output", encoding="utf-8")
    check = orphaned_logs_check(
        tmp_path, known_record_ids=set(), now=datetime.now(UTC), min_age=timedelta(hours=1)
    )
    assert check.status is HealthStatus.PASS


def test_orphaned_logs_check_is_fixable_with_full_paths(tmp_path: Path) -> None:
    log_path = tmp_path / "orphan.log"
    log_path.write_text("output", encoding="utf-8")
    old_time = (datetime.now(UTC) - timedelta(days=2)).timestamp()
    os.utime(log_path, (old_time, old_time))
    check = orphaned_logs_check(
        tmp_path, known_record_ids=set(), now=datetime.now(UTC), min_age=timedelta(hours=1)
    )
    assert check.fixable
    assert check.affected == [str(log_path)]


def test_orphaned_logs_check_pass_is_not_fixable(tmp_path: Path) -> None:
    check = orphaned_logs_check(
        tmp_path, known_record_ids=set(), now=datetime.now(UTC), min_age=timedelta(hours=1)
    )
    assert not check.fixable
    assert check.affected == []


def test_missing_cwd_check_pass_when_directory_exists(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    records = {"claude-ok": _record("claude-ok", last_attached_at=now, cwd=tmp_path)}
    check = missing_cwd_check(records, live_names=set())
    assert check.status is HealthStatus.PASS


def test_missing_cwd_check_warns_on_missing_directory(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    gone = tmp_path / "gone"
    records = {"claude-gone": _record("claude-gone", last_attached_at=now, cwd=gone)}
    check = missing_cwd_check(records, live_names=set())
    assert check.status is HealthStatus.WARN
    assert "claude-gone" in check.detail
    assert check.corrective_action


def test_missing_cwd_check_ignores_live_sessions(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    gone = tmp_path / "gone"
    records = {"claude-gone": _record("claude-gone", last_attached_at=now, cwd=gone)}
    check = missing_cwd_check(records, live_names={"claude-gone"})
    assert check.status is HealthStatus.PASS
