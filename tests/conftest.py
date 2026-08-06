from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest

from workspace_session_manager.config import AppConfig, HealthConfig, ToolProfile
from workspace_session_manager.errors import SessionExistsError, SessionNotFoundError, TmuxError
from workspace_session_manager.legacy import LegacyMetadataReader
from workspace_session_manager.models import TmuxSession, Tool
from workspace_session_manager.paths import AppPaths
from workspace_session_manager.service import SessionService
from workspace_session_manager.store import MetadataStore


class FakeBackend:
    def __init__(self) -> None:
        self.sessions: dict[str, TmuxSession] = {}
        self.options: dict[tuple[str, str], str] = {}
        self.previews: dict[str, str] = {}
        self.attached: list[str] = []
        self.interrupted: list[str] = []
        self.restarted: list[str] = []
        self.logging_paths: dict[str, Path] = {}
        self.created_commands: list[tuple[tuple[str, ...], tuple[str, ...] | None]] = []
        self._counter = 0

    def add(
        self,
        name: str,
        *,
        session_id: str | None = None,
        cwd: Path = Path("/tmp"),
        command: str = "bash",
        clients: int = 0,
        last_activity_at: datetime | None = None,
        pane_dead: bool = False,
        pane_dead_status: int | None = None,
    ) -> TmuxSession:
        self._counter += 1
        session = TmuxSession(
            session_id=session_id or f"$fake-{self._counter}",
            name=name,
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
            attached_clients=clients,
            windows=1,
            cwd=cwd,
            current_command=command,
            last_activity_at=last_activity_at,
            pane_dead=pane_dead,
            pane_dead_status=pane_dead_status,
        )
        self.sessions[name] = session
        return session

    def version(self) -> str:
        return "tmux fake"

    def list_sessions(self) -> list[TmuxSession]:
        return list(self.sessions.values())

    def get_session(self, name: str) -> TmuxSession:
        try:
            return self.sessions[name]
        except KeyError as error:
            raise SessionNotFoundError(f"session not found: {name}") from error

    def session_exists(self, name: str) -> bool:
        return name in self.sessions

    def require_expected(self, name: str, expected_id: str | None) -> TmuxSession:
        session = self.get_session(name)
        if expected_id is not None and session.session_id != expected_id:
            raise TmuxError("tmux ID mismatch")
        return session

    def create_session(
        self,
        name: str,
        cwd: Path,
        shell_command: Sequence[str],
        agent_command: Sequence[str] | None,
    ) -> TmuxSession:
        if name in self.sessions:
            raise SessionExistsError(name)
        self.created_commands.append(
            (tuple(shell_command), tuple(agent_command) if agent_command else None)
        )
        session = self.add(name, cwd=cwd, command=Path((agent_command or shell_command)[0]).name)
        self.set_option(name, "@wf_owner", "workspace-session-manager")
        return session

    def capture_pane(self, name: str, lines: int, expected_id: str | None = None) -> str:
        self.require_expected(name, expected_id)
        return "\n".join(self.previews.get(name, "").splitlines()[-lines:])

    def attach(self, name: str, expected_id: str | None = None) -> int:
        self.require_expected(name, expected_id)
        self.attached.append(name)
        return 0

    def rename_session(self, old_name: str, new_name: str, expected_id: str | None = None) -> None:
        if new_name in self.sessions:
            raise SessionExistsError(new_name)
        session = self.require_expected(old_name, expected_id)
        del self.sessions[old_name]
        self.sessions[new_name] = session.model_copy(update={"name": new_name})
        if old_name in self.logging_paths:
            self.logging_paths[new_name] = self.logging_paths.pop(old_name)
        for (session_name, option), value in list(self.options.items()):
            if session_name == old_name:
                del self.options[(session_name, option)]
                self.options[(new_name, option)] = value

    def kill_session(self, name: str, expected_id: str | None = None) -> None:
        self.require_expected(name, expected_id)
        del self.sessions[name]
        for key in [key for key in self.options if key[0] == name]:
            del self.options[key]

    def send_interrupt(self, name: str, expected_id: str | None = None) -> None:
        self.require_expected(name, expected_id)
        self.interrupted.append(name)

    def restart_session(
        self,
        name: str,
        cwd: Path,
        shell_command: Sequence[str],
        agent_command: Sequence[str] | None,
        expected_id: str | None = None,
    ) -> None:
        session = self.require_expected(name, expected_id)
        self.restarted.append(name)
        command = Path((agent_command or shell_command)[0]).name
        self.sessions[name] = session.model_copy(
            update={"cwd": cwd, "current_command": command, "pane_dead": False}
        )

    def set_logging(
        self,
        name: str,
        log_path: Path | None,
        expected_id: str | None = None,
    ) -> None:
        session = self.require_expected(name, expected_id)
        if log_path is None:
            self.logging_paths.pop(name, None)
            self.options.pop((name, "@wf_logging"), None)
        else:
            self.logging_paths[name] = log_path
            self.options[(name, "@wf_logging")] = "1"
        self.sessions[name] = session.model_copy(update={"logging_enabled": log_path is not None})

    def set_option(
        self,
        name: str,
        option: str,
        value: str,
        expected_id: str | None = None,
    ) -> None:
        session = self.require_expected(name, expected_id)
        self.options[(name, option)] = value
        if option == "@wf_owner":
            self.sessions[name] = session.model_copy(update={"wf_owner": value})
        elif option == "@wf_logging":
            self.sessions[name] = session.model_copy(update={"logging_enabled": value == "1"})

    def get_option(self, name: str, option: str, expected_id: str | None = None) -> str | None:
        self.require_expected(name, expected_id)
        return self.options.get((name, option))

    def unset_option(self, name: str, option: str, expected_id: str | None = None) -> None:
        session = self.require_expected(name, expected_id)
        self.options.pop((name, option), None)
        if option == "@wf_owner":
            self.sessions[name] = session.model_copy(update={"wf_owner": None})
        elif option == "@wf_logging":
            self.sessions[name] = session.model_copy(update={"logging_enabled": False})


@pytest.fixture
def fake_backend() -> FakeBackend:
    return FakeBackend()


@pytest.fixture
def app_config() -> AppConfig:
    return AppConfig(
        tools={
            Tool.CLAUDE: ToolProfile(command=("/bin/true",)),
            Tool.COPILOT: ToolProfile(command=("/bin/true",)),
            Tool.CODEX: ToolProfile(command=("/bin/true",)),
            Tool.HERMES: ToolProfile(command=("/bin/true",)),
            Tool.SHELL: ToolProfile(command=("/bin/bash", "-l")),
        },
        legacy_state_dirs=(),
        # Hermetic by default: no test should shell out to real apt/docker/git
        # or scan real project directories in the background. Tests targeting
        # the health-cockpit feature construct their own AppConfig instead.
        health=HealthConfig(enabled=False, project_scan_roots=()),
    )


@pytest.fixture
def service(
    tmp_path: Path,
    fake_backend: FakeBackend,
    app_config: AppConfig,
) -> SessionService:
    paths = AppPaths(tmp_path / "config", tmp_path / "state", tmp_path / "cache")
    return SessionService(
        backend=fake_backend,
        store=MetadataStore(paths),
        config=app_config,
        paths=paths,
        legacy=LegacyMetadataReader(()),
    )
