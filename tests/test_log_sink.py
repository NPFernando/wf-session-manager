from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def test_log_sink_writes_owner_only_sanitized_redacted_output(tmp_path: Path) -> None:
    destination = tmp_path / "logs" / "session.log"
    source = (
        "\x1b]0;private title\x07"
        "\x1b]52;c;Y2xpcGJvYXJkLXNlY3JldA==\x07"
        "\x1b[31mstatus\x1b[0m password=not-safe 192.168.1.1\n"
    )
    result = subprocess.run(
        [sys.executable, "-m", "workspace_session_manager.log_sink", str(destination)],
        input=source,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    logged = destination.read_text(encoding="utf-8")
    assert "\x1b" not in logged
    assert "private title" not in logged
    assert "clipboard-secret" not in logged
    assert "not-safe" not in logged
    assert "192.168.1.1" not in logged
    assert "status" in logged
    assert destination.stat().st_mode & 0o777 == 0o600


def test_log_sink_reassembles_multibyte_character_split_across_chunk_boundary(
    tmp_path: Path,
) -> None:
    from workspace_session_manager.log_sink import MAX_INPUT_CHUNK

    destination = tmp_path / "logs" / "session.log"
    # "é" is 2 bytes (0xC3 0xA9); place its first byte as the very last byte
    # of the first MAX_INPUT_CHUNK-sized readline() chunk, so the character
    # is split across the chunk boundary.
    source_bytes = b"a" * (MAX_INPUT_CHUNK - 1) + "é".encode() + b"rest\n"
    result = subprocess.run(
        [sys.executable, "-m", "workspace_session_manager.log_sink", str(destination)],
        input=source_bytes,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    logged = destination.read_text(encoding="utf-8")
    assert "�" not in logged
    assert "é" in logged
    assert "rest" in logged


def test_log_sink_main_reports_oserror_cleanly_instead_of_a_traceback(tmp_path: Path) -> None:
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("x", encoding="utf-8")
    destination = blocker / "session.log"
    result = subprocess.run(
        [sys.executable, "-m", "workspace_session_manager.log_sink", str(destination)],
        input=b"hello\n",
        capture_output=True,
        check=False,
    )
    assert result.returncode != 0
    stderr = result.stderr.decode()
    assert "Traceback" not in stderr
    assert "log_sink:" in stderr
