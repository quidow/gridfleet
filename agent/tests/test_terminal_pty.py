import asyncio
import fcntl
import struct
import sys
import termios

import pytest

from agent_app.terminal_pty import PtyShell


@pytest.mark.skipif(sys.platform == "win32", reason="PTY unsupported on Windows")
@pytest.mark.asyncio
async def test_pty_shell_echoes_input_and_exits() -> None:
    outputs: list[bytes] = []

    def on_output(data: bytes) -> None:
        outputs.append(data)

    shell = PtyShell(program="/bin/sh", cols=80, rows=24)
    await shell.start(on_output=on_output)
    assert shell.pid and shell.pid > 0

    await shell.write(b"echo hello\n")
    await shell.write(b"exit\n")

    exit_code = await asyncio.wait_for(shell.wait(), timeout=5.0)
    joined = b"".join(outputs)
    assert b"hello" in joined
    assert exit_code == 0
    assert shell.closed


@pytest.mark.skipif(sys.platform == "win32", reason="PTY unsupported on Windows")
@pytest.mark.asyncio
async def test_pty_shell_resize_updates_winsize() -> None:
    shell = PtyShell(program="/bin/sh", cols=80, rows=24)
    await shell.start(on_output=lambda _data: None)
    try:
        shell.resize(cols=132, rows=40)
        packed = fcntl.ioctl(shell._master_fd, termios.TIOCGWINSZ, b"\0" * 8)
        rows, cols, _, _ = struct.unpack("HHHH", packed)
        assert rows == 40
        assert cols == 132
    finally:
        await shell.close(reason="test_teardown")


@pytest.mark.skipif(sys.platform == "win32", reason="PTY unsupported on Windows")
@pytest.mark.asyncio
async def test_pty_shell_close_concurrent_with_wait() -> None:
    shell = PtyShell(program="/bin/sh", cols=80, rows=24)
    await shell.start(on_output=lambda _data: None)
    await shell.write(b"exit\n")
    wait_task = asyncio.create_task(shell.wait())
    close_task = asyncio.create_task(shell.close(reason="concurrent_close"))
    exit_code, _ = await asyncio.gather(wait_task, close_task)
    assert shell.closed
    assert isinstance(exit_code, int)
