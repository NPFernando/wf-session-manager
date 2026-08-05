"""Injection-resistant tmux adapter using argument arrays and exact targets."""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from workspace_session_manager.errors import SessionExistsError, SessionNotFoundError, TmuxError
from workspace_session_manager.models import TmuxSession

# tmux escapes control characters in format output as backslash-octal text.
FIELD_SEPARATOR = "\\037"
TMUX_FORMAT = "\x1f".join(
    (
        "#{session_id}",
        "#{session_name}",
        "#{session_created}",
        "#{session_activity}",
        "#{session_attached}",
        "#{session_windows}",
        "#{pane_current_path}",
        "#{pane_current_command}",
        "#{@wf_owner}",
        "#{@wf_logging}",
        "#{pane_dead}",
        "#{pane_dead_status}",
    )
)


class Runner(Protocol):
    def __call__(
        self,
        args: Sequence[str],
        *,
        capture: bool = True,
        timeout: float | None = 5.0,
    ) -> subprocess.CompletedProcess[str]: ...


def subprocess_runner(
    args: Sequence[str],
    *,
    capture: bool = True,
    timeout: float | None = 5.0,
) -> subprocess.CompletedProcess[str]:
    """Run without a shell; interactive calls inherit the current terminal."""
    return subprocess.run(  # noqa: S603 - fixed executable and shell=False
        list(args),
        check=False,
        capture_output=capture,
        text=True,
        timeout=timeout,
    )


class TmuxBackend:
    """Small tmux API with no implicit ownership decisions."""

    def __init__(
        self,
        runner: Runner = subprocess_runner,
        socket_name: str | None = None,
        socket_path: Path | None = None,
    ) -> None:
        if socket_name is not None and socket_path is not None:
            raise ValueError("choose a tmux socket name or socket path, not both")
        self._runner = runner
        self._socket_name = socket_name
        self._socket_path = socket_path

    def _command(self, *args: str) -> tuple[str, ...]:
        prefix: tuple[str, ...]
        if self._socket_path is not None:
            prefix = ("tmux", "-S", str(self._socket_path))
        elif self._socket_name is not None:
            prefix = ("tmux", "-L", self._socket_name)
        else:
            prefix = ("tmux",)
        return (*prefix, *args)

    def _run(
        self,
        *args: str,
        capture: bool = True,
        timeout: float | None = 5.0,
        allow_no_server: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        try:
            result = self._runner(self._command(*args), capture=capture, timeout=timeout)
        except (OSError, subprocess.SubprocessError) as error:
            raise TmuxError(f"unable to run tmux: {error}") from error

        if result.returncode != 0:
            stderr = (result.stderr or "").strip()
            if allow_no_server and (
                "no server running" in stderr.lower()
                or "failed to connect" in stderr.lower()
                or "error connecting to" in stderr.lower()
            ):
                return result
            detail = stderr or f"tmux exited with status {result.returncode}"
            raise TmuxError(detail)
        return result

    def version(self) -> str:
        return self._run("-V").stdout.strip()

    def list_sessions(self) -> list[TmuxSession]:
        result = self._run("list-sessions", "-F", TMUX_FORMAT, allow_no_server=True)
        if result.returncode != 0:
            return []

        sessions: list[TmuxSession] = []
        for line in result.stdout.splitlines():
            if not line:
                continue
            fields = line.split(FIELD_SEPARATOR)
            if len(fields) != 12:
                raise TmuxError("tmux returned an unexpected session record")
            (
                session_id,
                name,
                created,
                activity,
                attached,
                windows,
                cwd,
                command,
                owner,
                logging,
                pane_dead,
                pane_dead_status,
            ) = fields
            try:
                sessions.append(
                    TmuxSession(
                        session_id=session_id,
                        name=name,
                        created_at=datetime.fromtimestamp(int(created), tz=UTC),
                        attached_clients=int(attached),
                        windows=int(windows),
                        cwd=Path(cwd or "/"),
                        current_command=command,
                        wf_owner=owner or None,
                        logging_enabled=logging == "1",
                        last_activity_at=datetime.fromtimestamp(int(activity), tz=UTC),
                        pane_dead=pane_dead == "1",
                        pane_dead_status=int(pane_dead_status) if pane_dead_status else None,
                    )
                )
            except (ValueError, TypeError) as error:
                raise TmuxError(f"invalid tmux session record for {name!r}") from error
        return sessions

    def get_session(self, name: str) -> TmuxSession:
        for session in self.list_sessions():
            if session.name == name:
                return session
        raise SessionNotFoundError(f"session not found: {name}")

    def session_exists(self, name: str) -> bool:
        return any(session.name == name for session in self.list_sessions())

    def _session_target(self, name: str, expected_id: str | None) -> str:
        if expected_id is None:
            return f"={name}"
        session = self.get_session(name)
        if session.session_id != expected_id:
            raise TmuxError(
                f"refusing to target {name}: expected tmux ID {expected_id}, "
                f"found {session.session_id}"
            )
        return expected_id

    def create_session(
        self,
        name: str,
        cwd: Path,
        shell_command: Sequence[str],
        agent_command: Sequence[str] | None,
    ) -> TmuxSession:
        if self.session_exists(name):
            raise SessionExistsError(f"session already exists: {name}")

        try:
            result = self._run(
                "new-session",
                "-d",
                "-P",
                "-F",
                "#{session_id}",
                "-s",
                name,
                "-c",
                str(cwd),
                "--",
                *shell_command,
            )
        except TmuxError as error:
            if "duplicate session" in str(error).lower():
                raise SessionExistsError(f"session already exists: {name}") from error
            raise
        created_id = result.stdout.strip()
        try:
            self.set_option(
                name,
                "@wf_owner",
                "workspace-session-manager",
                expected_id=created_id or None,
            )
            if agent_command:
                self._send_command(created_id or f"={name}", agent_command)
            session = self.get_session(name)
            if created_id and session.session_id != created_id:
                raise TmuxError("tmux session ID changed during creation")
            return session
        except Exception:
            self.kill_session(name, expected_id=created_id or None)
            raise

    def set_option(
        self,
        name: str,
        option: str,
        value: str,
        expected_id: str | None = None,
    ) -> None:
        target = self._session_target(name, expected_id)
        self._run("set-option", "-q", "-t", f"{target}:", option, value)

    def get_option(self, name: str, option: str, expected_id: str | None = None) -> str | None:
        target = self._session_target(name, expected_id)
        result = self._runner(
            self._command("show-options", "-qv", "-t", f"{target}:", option),
            capture=True,
            timeout=5.0,
        )
        if result.returncode != 0:
            return None
        return result.stdout.strip() or None

    def unset_option(self, name: str, option: str, expected_id: str | None = None) -> None:
        target = self._session_target(name, expected_id)
        self._run("set-option", "-q", "-u", "-t", f"{target}:", option)

    def capture_pane(self, name: str, lines: int, expected_id: str | None = None) -> str:
        target = self._session_target(name, expected_id)
        return self._run(
            "capture-pane",
            "-p",
            "-t",
            f"{target}:",
            "-S",
            f"-{lines}",
        ).stdout

    def _send_command(self, target: str, command: Sequence[str]) -> None:
        command_line = shlex.join(command)
        self._run("send-keys", "-t", f"{target}:", "-l", "--", command_line)
        self._run("send-keys", "-t", f"{target}:", "Enter")

    def send_interrupt(self, name: str, expected_id: str | None = None) -> None:
        target = self._session_target(name, expected_id)
        self._run("send-keys", "-t", f"{target}:", "C-c")

    def restart_session(
        self,
        name: str,
        cwd: Path,
        shell_command: Sequence[str],
        agent_command: Sequence[str] | None,
        expected_id: str | None = None,
    ) -> None:
        target = self._session_target(name, expected_id)
        self._run(
            "respawn-pane",
            "-k",
            "-t",
            f"{target}:",
            "-c",
            str(cwd),
            "--",
            *shell_command,
        )
        if agent_command:
            self._send_command(target, agent_command)

    def set_logging(
        self,
        name: str,
        log_path: Path | None,
        expected_id: str | None = None,
    ) -> None:
        target = self._session_target(name, expected_id)
        pane_target = f"{target}:"
        if log_path is None:
            self._run("pipe-pane", "-t", pane_target)
            self.unset_option(name, "@wf_logging", expected_id=expected_id)
            return
        command = shlex.join(
            (sys.executable, "-m", "workspace_session_manager.log_sink", str(log_path))
        )
        self._run("pipe-pane", "-o", "-t", pane_target, "--", command)
        try:
            self.set_option(name, "@wf_logging", "1", expected_id=expected_id)
        except Exception:
            self._run("pipe-pane", "-t", pane_target)
            raise

    def attach(self, name: str, expected_id: str | None = None) -> int:
        target = self._session_target(name, expected_id)
        command = "switch-client" if os.environ.get("TMUX") else "attach-session"
        result = self._run(command, "-t", target, capture=False, timeout=None)
        return result.returncode

    def rename_session(self, old_name: str, new_name: str, expected_id: str | None = None) -> None:
        target = self._session_target(old_name, expected_id)
        if self.session_exists(new_name):
            raise SessionExistsError(f"session already exists: {new_name}")
        try:
            self._run("rename-session", "-t", target, new_name)
        except TmuxError as error:
            if "duplicate session" in str(error).lower():
                raise SessionExistsError(f"session already exists: {new_name}") from error
            raise

    def kill_session(self, name: str, expected_id: str | None = None) -> None:
        target = self._session_target(name, expected_id)
        self._run("kill-session", "-t", target)


BackendFactory = Callable[[], TmuxBackend]
