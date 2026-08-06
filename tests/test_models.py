from pathlib import Path

import pytest
from pydantic import ValidationError

from workspace_session_manager.models import (
    CreateRequest,
    InputState,
    Preset,
    SessionMetadata,
    TaskState,
    Tool,
)
from workspace_session_manager.security import bounded_output, bounded_preview, redact_text
from workspace_session_manager.service import normalized_session_name, slugify_name


def test_slugify_and_tool_prefix() -> None:
    assert slugify_name(" API Refactor! ") == "api-refactor"
    assert slugify_name("api_refactor") == "api-refactor"
    assert normalized_session_name(Tool.CLAUDE, "API Refactor") == "claude-api-refactor"
    assert normalized_session_name(Tool.CLAUDE, "api_refactor") == "claude-api-refactor"
    assert normalized_session_name(Tool.CLAUDE, "claude-api-refactor") == ("claude-api-refactor")
    assert (
        normalized_session_name(Tool.CLAUDE, "api-refactor", automatic_prefix=False)
        == "api-refactor"
    )
    assert normalized_session_name(Tool.COPILOT, "review") == "copilot-review"
    assert normalized_session_name(Tool.CODEX, "codex-review") == "codex-review"
    assert normalized_session_name(Tool.SHELL, "Diagnostics") == "diagnostics"


def test_metadata_rejects_relative_path_and_invalid_tag() -> None:
    with pytest.raises(ValidationError):
        SessionMetadata(
            tmux_session_id="$1",
            name="claude-test",
            tool=Tool.CLAUDE,
            cwd=Path("relative"),
            tags=["not valid"],
        )


def test_create_request_rejects_relative_cwd() -> None:
    with pytest.raises(ValidationError):
        CreateRequest(name="claude-test", tool=Tool.CLAUDE, cwd=Path("relative"))


def test_preset_rejects_relative_cwd() -> None:
    with pytest.raises(ValidationError):
        Preset(name="backend-dev", tool=Tool.CLAUDE, cwd=Path("relative"))


def test_preview_is_sanitized_redacted_and_bounded() -> None:
    source = (
        "\x1b[31mred\x1b[0m\n"
        "password=correct-horse-battery-staple\n"
        "sk-abcdefghijklmnopqrstuvwxyz1234\n"
        "/home/tester/private 192.168.1.10\n"
        "last"
    )
    redacted = redact_text(source, home=Path("/home/tester"))
    assert "\x1b" not in redacted
    assert "correct-horse" not in redacted
    assert "sk-abc" not in redacted
    assert "192.168" not in redacted
    assert "~/private" in redacted
    assert bounded_preview(source, 2).splitlines()[-1] == "last"


def test_clipboard_title_and_control_sequences_are_removed() -> None:
    source = (
        "before\x1b]0;secret title\x07"
        "\x1b]52;c;Y2xpcGJvYXJk\x1b\\"
        "\x1bPignored-device-control\x1b\\after"
        "\x9d8;hidden hyperlink\x9c"
        "\x90hidden-c1-device-control\x9c"
    )
    clean = redact_text(source)
    assert "\x1b" not in clean
    assert "secret title" not in clean
    assert "Y2xpcGJvYXJk" not in clean
    assert "ignored-device-control" not in clean
    assert "hidden hyperlink" not in clean
    assert "hidden-c1-device-control" not in clean
    assert "before" in clean
    assert "after" in clean


@pytest.mark.parametrize(
    ("legacy", "current"),
    [
        ("active", TaskState.IN_PROGRESS),
        ("waiting", TaskState.WAITING),
        ("blocked", TaskState.BLOCKED),
        ("done", TaskState.COMPLETED),
        ("paused", TaskState.WAITING),
    ],
)
def test_schema_v1_metadata_is_normalized_without_a_file_migration(
    legacy: str,
    current: TaskState,
) -> None:
    record = SessionMetadata.model_validate(
        {
            "schema_version": 1,
            "tmux_session_id": "$1",
            "name": "claude-test",
            "tool": "claude",
            "cwd": "/tmp",
            "state": legacy,
        }
    )
    assert record.schema_version == 2
    assert record.task_state is current
    assert record.input_state is InputState.NONE
    assert "state" not in record.model_dump()
    assert record.display_name == ""


def test_output_is_bounded_by_lines_and_utf8_bytes() -> None:
    result = bounded_output("one\ntwo\nthree\nfour", max_lines=3, max_bytes=10)
    assert result.truncated
    assert result.text == "three\nfour"
    unicode_result = bounded_output("start\n" + "x" * 20 + "£", max_lines=10, max_bytes=8)
    assert unicode_result.truncated
    assert "�" not in unicode_result.text
