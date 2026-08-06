"""Sanitize tmux pipe-pane output before appending it to an owner-only log."""

from __future__ import annotations

import codecs
import os
import stat
import sys
import tempfile
from pathlib import Path

from workspace_session_manager.security import redact_text

MAX_LOG_BYTES = 5 * 1024 * 1024
RETAIN_BYTES = 2 * 1024 * 1024
MAX_INPUT_CHUNK = 64 * 1024


def _write_all(descriptor: int, content: bytes) -> None:
    view = memoryview(content)
    while view:
        written = os.write(descriptor, view)
        if written == 0:
            raise OSError("unable to write sanitized log")
        view = view[written:]


def _open_log(path: Path) -> int:
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    details = os.fstat(descriptor)
    if not stat.S_ISREG(details.st_mode) or details.st_uid != os.getuid():
        os.close(descriptor)
        raise OSError("refusing unsafe log destination")
    os.fchmod(descriptor, 0o600)
    return descriptor


def _rotate(path: Path, descriptor: int) -> int:
    if os.fstat(descriptor).st_size <= MAX_LOG_BYTES:
        return descriptor
    os.close(descriptor)
    read_flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        read_flags |= os.O_NOFOLLOW
    source = os.open(path, read_flags)
    try:
        size = os.fstat(source).st_size
        os.lseek(source, max(0, size - RETAIN_BYTES), os.SEEK_SET)
        retained = os.read(source, RETAIN_BYTES)
    finally:
        os.close(source)
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", delete=False) as tmp:
        temporary = Path(tmp.name)
        tmp.write(retained)
        tmp.flush()
        os.fsync(tmp.fileno())
    try:
        temporary.chmod(0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return _open_log(path)


def stream_to_log(path: Path) -> int:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    descriptor = _open_log(path)
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    try:
        while chunk := sys.stdin.buffer.readline(MAX_INPUT_CHUNK):
            clean = redact_text(decoder.decode(chunk))
            _write_all(descriptor, clean.encode("utf-8"))
            descriptor = _rotate(path, descriptor)
        tail = redact_text(decoder.decode(b"", final=True))
        if tail:
            _write_all(descriptor, tail.encode("utf-8"))
    finally:
        os.close(descriptor)
    return 0


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: python -m workspace_session_manager.log_sink LOG_PATH")
    try:
        exit_code = stream_to_log(Path(sys.argv[1]))
    except OSError as error:
        raise SystemExit(f"log_sink: {error}") from error
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
