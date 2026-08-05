import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from workspace_session_manager.errors import SessionExistsError, TmuxError
from workspace_session_manager.tmux import FIELD_SEPARATOR, TMUX_FORMAT, TmuxBackend


class RecordingRunner:
    def __init__(self, stdout: str = "", returncode: int = 0, stderr: str = "") -> None:
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr
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
        return subprocess.CompletedProcess(
            args=args,
            returncode=self.returncode,
            stdout=self.stdout,
            stderr=self.stderr,
        )


def test_list_sessions_parses_machine_format() -> None:
    line = FIELD_SEPARATOR.join(
        (
            "$3",
            "claude-api",
            "1767225600",
            "1767225660",
            "1",
            "2",
            "/srv/api",
            "claude",
            "",
            "1",
            "0",
            "",
        )
    )
    runner = RecordingRunner(stdout=f"{line}\n")
    sessions = TmuxBackend(runner).list_sessions()
    assert sessions[0].name == "claude-api"
    assert sessions[0].attached
    assert sessions[0].cwd.as_posix() == "/srv/api"
    assert sessions[0].last_activity_at is not None
    assert sessions[0].logging_enabled
    assert not sessions[0].pane_dead
    assert runner.calls[0][:2] == ("tmux", "list-sessions")


@pytest.mark.parametrize(
    "message",
    (
        "no server running on /tmp/tmux",
        "failed to connect to server",
        "error connecting to /tmp/tmux-1001/default (No such file or directory)",
    ),
)
def test_no_server_is_an_empty_inventory(message: str) -> None:
    runner = RecordingRunner(returncode=1, stderr=message)
    assert TmuxBackend(runner).list_sessions() == []


def test_session_option_uses_exact_pane_target() -> None:
    runner = RecordingRunner(stdout="workspace-session-manager\n")
    value = TmuxBackend(runner).get_option("claude-api", "@wf_owner")
    assert value == "workspace-session-manager"
    assert runner.calls[0] == (
        "tmux",
        "show-options",
        "-qv",
        "-t",
        "=claude-api:",
        "@wf_owner",
    )


def test_unset_session_option_uses_exact_pane_target() -> None:
    runner = RecordingRunner()
    TmuxBackend(runner).unset_option("claude-api", "@wf_owner")
    assert runner.calls[0] == (
        "tmux",
        "set-option",
        "-q",
        "-u",
        "-t",
        "=claude-api:",
        "@wf_owner",
    )


def live_session_line(session_id: str = "$3", name: str = "claude-api") -> str:
    return FIELD_SEPARATOR.join(
        (
            session_id,
            name,
            "1767225600",
            "1767225660",
            "0",
            "1",
            "/srv/api",
            "claude",
            "",
            "",
            "0",
            "",
        )
    )


def test_expected_id_is_used_for_final_tmux_targets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TMUX", raising=False)
    capture_runner = RecordingRunner(stdout=f"{live_session_line()}\n")
    TmuxBackend(capture_runner).capture_pane("claude-api", 20, expected_id="$3")
    assert capture_runner.calls[-1] == (
        "tmux",
        "capture-pane",
        "-p",
        "-t",
        "$3:",
        "-S",
        "-20",
    )

    option_runner = RecordingRunner(stdout=f"{live_session_line()}\n")
    TmuxBackend(option_runner).set_option(
        "claude-api", "@wf_owner", "workspace-session-manager", expected_id="$3"
    )
    assert option_runner.calls[-1] == (
        "tmux",
        "set-option",
        "-q",
        "-t",
        "$3:",
        "@wf_owner",
        "workspace-session-manager",
    )

    read_runner = RecordingRunner(stdout=f"{live_session_line()}\n")
    TmuxBackend(read_runner).get_option("claude-api", "@wf_owner", expected_id="$3")
    assert read_runner.calls[-1] == (
        "tmux",
        "show-options",
        "-qv",
        "-t",
        "$3:",
        "@wf_owner",
    )

    unset_runner = RecordingRunner(stdout=f"{live_session_line()}\n")
    TmuxBackend(unset_runner).unset_option("claude-api", "@wf_owner", expected_id="$3")
    assert unset_runner.calls[-1] == (
        "tmux",
        "set-option",
        "-q",
        "-u",
        "-t",
        "$3:",
        "@wf_owner",
    )

    attach_runner = RecordingRunner(stdout=f"{live_session_line()}\n")
    TmuxBackend(attach_runner).attach("claude-api", expected_id="$3")
    assert attach_runner.calls[-1] == ("tmux", "attach-session", "-t", "$3")

    rename_runner = RecordingRunner(stdout=f"{live_session_line()}\n")
    TmuxBackend(rename_runner).rename_session("claude-api", "claude-new", expected_id="$3")
    assert rename_runner.calls[-1] == ("tmux", "rename-session", "-t", "$3", "claude-new")

    kill_runner = RecordingRunner(stdout=f"{live_session_line()}\n")
    TmuxBackend(kill_runner).kill_session("claude-api", expected_id="$3")
    assert kill_runner.calls[-1] == ("tmux", "kill-session", "-t", "$3")


def test_expected_id_mismatch_never_runs_final_tmux_command() -> None:
    runner = RecordingRunner(stdout=f"{live_session_line(session_id='$replacement')}\n")
    with pytest.raises(TmuxError, match="expected tmux ID \\$original"):
        TmuxBackend(runner).kill_session("claude-api", expected_id="$original")
    assert runner.calls == [("tmux", "list-sessions", "-F", TMUX_FORMAT)]


class DispatchRunner:
    """Returns a different canned response depending on the tmux subcommand,
    for tests where the pre-check call (list-sessions) must succeed while a
    later call (new-session/rename-session) fails -- simulating a session
    created by a concurrent caller in the window between the two."""

    def __init__(self, responses: dict[str, subprocess.CompletedProcess[str]]) -> None:
        self.responses = responses
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
        subcommand = args[1]
        return self.responses[subcommand]


def test_create_session_translates_duplicate_race_to_session_exists_error(
    tmp_path: Path,
) -> None:
    runner = DispatchRunner(
        {
            "list-sessions": subprocess.CompletedProcess(
                args=(), returncode=1, stdout="", stderr="no server running"
            ),
            "new-session": subprocess.CompletedProcess(
                args=(),
                returncode=1,
                stdout="",
                stderr="duplicate session: claude-api",
            ),
        }
    )
    with pytest.raises(SessionExistsError, match="claude-api"):
        TmuxBackend(runner).create_session(
            "claude-api", tmp_path, ("/bin/bash", "-l"), None
        )


def test_rename_session_translates_duplicate_race_to_session_exists_error() -> None:
    runner = DispatchRunner(
        {
            "list-sessions": subprocess.CompletedProcess(
                args=(), returncode=0, stdout=f"{live_session_line()}\n", stderr=""
            ),
            "rename-session": subprocess.CompletedProcess(
                args=(),
                returncode=1,
                stdout="",
                stderr="duplicate session: claude-new",
            ),
        }
    )
    with pytest.raises(SessionExistsError, match="claude-new"):
        TmuxBackend(runner).rename_session("claude-api", "claude-new", expected_id="$3")


def test_interrupt_restart_and_logging_use_exact_pane_target(tmp_path: Path) -> None:
    interrupt_runner = RecordingRunner(stdout=f"{live_session_line()}\n")
    TmuxBackend(interrupt_runner).send_interrupt("claude-api", expected_id="$3")
    assert interrupt_runner.calls[-1] == ("tmux", "send-keys", "-t", "$3:", "C-c")

    restart_runner = RecordingRunner(stdout=f"{live_session_line()}\n")
    TmuxBackend(restart_runner).restart_session(
        "claude-api",
        tmp_path,
        ("/bin/bash", "-l"),
        ("codex", "--quiet"),
        expected_id="$3",
    )
    assert (
        "tmux",
        "respawn-pane",
        "-k",
        "-t",
        "$3:",
        "-c",
        str(tmp_path),
        "--",
        "/bin/bash",
        "-l",
    ) in restart_runner.calls
    assert restart_runner.calls[-1] == ("tmux", "send-keys", "-t", "$3:", "Enter")

    logging_runner = RecordingRunner(stdout=f"{live_session_line()}\n")
    log_path = tmp_path / "session.log"
    TmuxBackend(logging_runner).set_logging("claude-api", log_path, expected_id="$3")
    pipe_call = next(call for call in logging_runner.calls if "pipe-pane" in call)
    assert pipe_call[:6] == ("tmux", "pipe-pane", "-o", "-t", "$3:", "--")
    assert "workspace_session_manager.log_sink" in pipe_call[-1]
    assert str(log_path) in pipe_call[-1]


def test_named_socket_is_applied_to_every_tmux_command() -> None:
    runner = RecordingRunner(stdout="tmux 3.4\n")
    assert TmuxBackend(runner, socket_name="wf-test").version() == "tmux 3.4"
    assert runner.calls == [("tmux", "-L", "wf-test", "-V")]


def test_socket_path_is_applied_to_every_tmux_command(tmp_path: Path) -> None:
    socket_path = tmp_path / "tmux.sock"
    runner = RecordingRunner(stdout="tmux 3.4\n")
    assert TmuxBackend(runner, socket_path=socket_path).version() == "tmux 3.4"
    assert runner.calls == [("tmux", "-S", str(socket_path), "-V")]


def test_socket_name_and_path_are_mutually_exclusive(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="not both"):
        TmuxBackend(socket_name="wf-test", socket_path=tmp_path / "tmux.sock")
