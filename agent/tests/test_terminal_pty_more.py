from __future__ import annotations

import sys

import pytest

from agent_app.terminal_pty import PtyShell


@pytest.mark.skipif(sys.platform == "win32", reason="PTY unsupported on Windows")
@pytest.mark.asyncio
async def test_pty_write_after_close_drops_data() -> None:
    shell = PtyShell(program="/bin/sh", cols=80, rows=24)
    await shell.start(on_output=lambda _data: None)
    await shell.close(reason="test")
    await shell.write(b"hello")  # should not raise
    assert shell.closed is True


@pytest.mark.skipif(sys.platform == "win32", reason="PTY unsupported on Windows")
@pytest.mark.asyncio
async def test_pty_resize_before_started_does_nothing() -> None:
    shell = PtyShell(program="/bin/sh", cols=80, rows=24)
    shell.resize(cols=132, rows=40)
    assert shell._master_fd is None


@pytest.mark.skipif(sys.platform == "win32", reason="PTY unsupported on Windows")
@pytest.mark.asyncio
async def test_pty_resize_after_closed_does_nothing() -> None:
    shell = PtyShell(program="/bin/sh", cols=80, rows=24)
    await shell.start(on_output=lambda _data: None)
    await shell.close(reason="test")
    shell.resize(cols=132, rows=40)


@pytest.mark.skipif(sys.platform == "win32", reason="PTY unsupported on Windows")
@pytest.mark.asyncio
async def test_pty_close_before_start_does_nothing() -> None:
    shell = PtyShell(program="/bin/sh", cols=80, rows=24)
    await shell.close(reason="early close")
    assert shell.closed is False


@pytest.mark.skipif(sys.platform == "win32", reason="PTY unsupported on Windows")
@pytest.mark.asyncio
async def test_pty_on_readable_with_none_master_fd_ignores() -> None:
    shell = PtyShell(program="/bin/sh", cols=80, rows=24)
    shell._master_fd = None
    shell._on_readable()  # should not raise


@pytest.mark.skipif(sys.platform == "win32", reason="PTY unsupported on Windows")
@pytest.mark.asyncio
async def test_pty_wait_without_process_returns_negative_one() -> None:
    shell = PtyShell(program="/bin/sh", cols=80, rows=24)
    rc = await shell.wait()
    assert rc == -1


@pytest.mark.skipif(sys.platform == "win32", reason="PTY unsupported on Windows")
@pytest.mark.asyncio
async def test_pty_write_would_block_warns(monkeypatch: pytest.MonkeyPatch) -> None:
    shell = PtyShell(program="/bin/sh", cols=80, rows=24)
    await shell.start(on_output=lambda _data: None)
    import os

    real_write = os.write
    call_count = 0

    def fake_write(fd: int, data: bytes) -> int:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise BlockingIOError()
        return real_write(fd, data)

    monkeypatch.setattr(os, "write", fake_write)
    import logging

    records: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    cap = _Capture()
    logging.getLogger("agent_app.terminal_pty").addHandler(cap)
    try:
        await shell.write(b"x")
    finally:
        logging.getLogger("agent_app.terminal_pty").removeHandler(cap)
    await shell.close(reason="test")
    assert any("would block" in r.getMessage() for r in records)
