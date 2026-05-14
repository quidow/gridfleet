from __future__ import annotations

import asyncio
import contextlib
import fcntl
import logging
import os
import pty
import signal
import struct
import termios
from collections.abc import Callable

logger = logging.getLogger(__name__)

OnOutput = Callable[[bytes], None]


class PtyShell:
    def __init__(self, *, program: str, cols: int, rows: int) -> None:
        self._program = program
        self._cols = cols
        self._rows = rows
        self._master_fd: int | None = None
        self._process: asyncio.subprocess.Process | None = None
        self._on_output: OnOutput | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self.closed = False
        self._cleanup_lock = asyncio.Lock()

    @property
    def pid(self) -> int | None:
        return self._process.pid if self._process else None

    async def start(self, *, on_output: OnOutput) -> None:
        self._on_output = on_output
        self._loop = asyncio.get_running_loop()
        master_fd, slave_fd = pty.openpty()
        self._master_fd = master_fd
        self._set_winsize(self._cols, self._rows)
        os.set_blocking(master_fd, False)
        env = os.environ.copy()
        env.setdefault("TERM", "xterm-256color")
        self._process = await asyncio.create_subprocess_exec(
            self._program,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            start_new_session=True,
            env=env,
        )
        os.close(slave_fd)
        self._loop.add_reader(master_fd, self._on_readable)

    def _on_readable(self) -> None:
        if self._master_fd is None:
            return
        try:
            data = os.read(self._master_fd, 4096)
        except OSError:
            data = b""
        if not data:
            self._stop_reader()
            return
        if self._on_output is not None:
            try:
                self._on_output(data)
            except Exception:
                logger.exception("terminal output handler failed")

    def _stop_reader(self) -> None:
        if self._master_fd is not None and self._loop is not None:
            with contextlib.suppress(ValueError, OSError):
                self._loop.remove_reader(self._master_fd)

    def _cleanup_fd(self) -> None:
        self._stop_reader()
        if self._master_fd is not None:
            with contextlib.suppress(OSError):
                os.close(self._master_fd)
            self._master_fd = None

    async def write(self, data: bytes) -> None:
        if self._master_fd is None or self.closed:
            return
        try:
            os.write(self._master_fd, data)
        except BlockingIOError:
            logger.warning("PTY write would block; dropping %d bytes", len(data))

    def resize(self, *, cols: int, rows: int) -> None:
        if self._master_fd is None or self.closed:
            return
        self._cols = cols
        self._rows = rows
        self._set_winsize(cols, rows)

    def _set_winsize(self, cols: int, rows: int) -> None:
        assert self._master_fd is not None
        fcntl.ioctl(self._master_fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

    async def wait(self) -> int:
        if self._process is None:
            return -1
        rc = await self._process.wait()
        async with self._cleanup_lock:
            self.closed = True
            self._cleanup_fd()
        return rc if rc is not None else -1

    async def close(self, *, reason: str) -> None:
        async with self._cleanup_lock:
            if self.closed or self._process is None:
                return
            self.closed = True  # set before awaiting to prevent re-entry
        logger.info("closing PTY shell reason=%s pid=%s", reason, self._process.pid)
        try:
            with contextlib.suppress(ProcessLookupError):
                self._process.send_signal(signal.SIGTERM)
            try:
                await asyncio.wait_for(self._process.wait(), timeout=2.0)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    self._process.send_signal(signal.SIGKILL)
                await self._process.wait()
        finally:
            async with self._cleanup_lock:
                self._cleanup_fd()
