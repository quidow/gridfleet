import asyncio
import sys
from pathlib import Path

import pytest

from agent_app.appium import log_files
from agent_app.appium.log_files import (
    appium_log_dir,
    appium_log_path,
    open_log_file,
    sweep_log_dir,
    tail_lines,
    truncate_if_oversized,
    truncate_oversized_logs,
)


def test_log_path_lives_under_runtime_root(tmp_path: Path) -> None:
    assert appium_log_dir() == tmp_path / "appium-logs"
    assert appium_log_path(4723) == tmp_path / "appium-logs" / "appium-4723.log"


def test_open_log_file_creates_dir_and_appends() -> None:
    with open_log_file(4723) as fh:
        fh.write(b"first\n")
    with open_log_file(4723) as fh:
        fh.write(b"second\n")
    assert appium_log_path(4723).read_text() == "first\nsecond\n"


def test_tail_lines_missing_file_returns_empty() -> None:
    assert tail_lines(appium_log_path(9999), 100) == []


def test_tail_lines_returns_last_n() -> None:
    path = appium_log_path(4723)
    path.parent.mkdir(parents=True)
    path.write_text("".join(f"line {i}\n" for i in range(10)))
    assert tail_lines(path, 3) == ["line 7", "line 8", "line 9"]
    assert tail_lines(path, 100) == [f"line {i}" for i in range(10)]


def test_tail_lines_drops_partial_first_line_on_oversized_file(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(log_files, "TAIL_READ_BYTES", 16)
    path = appium_log_path(4723)
    path.parent.mkdir(parents=True)
    path.write_text("aaaaaaaaaa\nbbbb\ncccc\n")  # 21 bytes; 16-byte tail starts mid-"aaaa…" line
    assert tail_lines(path, 100) == ["bbbb", "cccc"]


def test_truncate_noop_under_cap() -> None:
    path = appium_log_path(4723)
    path.parent.mkdir(parents=True)
    path.write_text("small\n")
    assert truncate_if_oversized(path) is False
    assert path.read_text() == "small\n"


def test_truncate_missing_file_is_noop() -> None:
    assert truncate_if_oversized(appium_log_path(9999)) is False


def test_truncate_keeps_tail(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(log_files, "MAX_LOG_BYTES", 100)
    monkeypatch.setattr(log_files, "TAIL_READ_BYTES", 30)
    path = appium_log_path(4723)
    path.parent.mkdir(parents=True)
    content = "".join(f"line {i:04d}\n" for i in range(20))  # 200 bytes
    path.write_text(content)
    assert truncate_if_oversized(path) is True
    assert path.stat().st_size == 30
    assert path.read_bytes() == content.encode()[-30:]


def test_truncate_preserves_child_append_offset(monkeypatch: pytest.MonkeyPatch) -> None:
    """The child's O_APPEND fd must keep appending at the new EOF after copy-truncate."""
    monkeypatch.setattr(log_files, "MAX_LOG_BYTES", 50)
    monkeypatch.setattr(log_files, "TAIL_READ_BYTES", 10)
    child_fh = open_log_file(4723)  # same flags as the spawned process gets
    try:
        child_fh.write(b"x" * 100)
        child_fh.flush()
        assert truncate_if_oversized(appium_log_path(4723)) is True
        child_fh.write(b"AFTER")
        child_fh.flush()
    finally:
        child_fh.close()
    data = appium_log_path(4723).read_bytes()
    assert data == b"x" * 10 + b"AFTER"


def test_truncate_oversized_logs_scans_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(log_files, "MAX_LOG_BYTES", 10)
    monkeypatch.setattr(log_files, "TAIL_READ_BYTES", 5)
    big = appium_log_path(4723)
    big.parent.mkdir(parents=True)
    big.write_text("x" * 50)
    small = appium_log_path(4724)
    small.write_text("ok")
    truncate_oversized_logs()
    assert big.stat().st_size == 5
    assert small.read_text() == "ok"


def test_truncate_oversized_logs_noop_without_dir() -> None:
    truncate_oversized_logs()  # must not raise or create the dir
    assert not appium_log_dir().exists()


def test_sweep_removes_log_files() -> None:
    path = appium_log_path(4723)
    path.parent.mkdir(parents=True)
    path.write_text("orphan\n")
    sweep_log_dir()
    assert not path.exists()
    assert appium_log_dir().is_dir()


def test_sweep_noop_without_dir() -> None:
    sweep_log_dir()  # must not raise or create the dir
    assert not appium_log_dir().exists()


async def test_subprocess_output_lands_in_file() -> None:
    """End-to-end fd redirect: a real child writes stdout+stderr into the log file with zero reader tasks."""
    log_file = open_log_file(4723)
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            "import sys; print('out line'); print('err line', file=sys.stderr)",
            stdout=log_file,
            stderr=log_file,
        )
        await proc.wait()
    finally:
        log_file.close()
    lines = tail_lines(appium_log_path(4723), 100)
    assert "out line" in lines
    assert "err line" in lines
