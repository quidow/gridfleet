"""Reclaim a desired Appium port from an unmanaged Appium this host started.

A leaked Appium child — one whose supervising task was cancelled after the
spawn, or one outliving a previous agent process — keeps its port bound, and
every later start on that port fails with ``PortOccupiedError`` forever with
nothing able to clear it.

Reclaim is deliberately narrow. Only a process recognisable as *this agent's*
Appium is terminated: an ``appium server --port <port>`` command line, running
from a path under ``AGENT_RUNTIME_ROOT`` (or with ``APPIUM_HOME`` under it).
Anything else is left alone and the caller keeps raising — taking a port from
an unrelated process on a shared host is not the agent's business.

No socket-table lookup: ``psutil.net_connections`` needs root on macOS. The
caller's HTTP/bind probe already proved the port is held; this module only
answers *by whom*.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Protocol

import psutil  # type: ignore[import-untyped]

logger = logging.getLogger(__name__)

TERMINATE_GRACE_SEC = 5.0
KILL_GRACE_SEC = 2.0
POLL_INTERVAL_SEC = 0.2


class ProcessHandle(Protocol):
    """The slice of ``psutil.Process`` this module uses (psutil ships no stubs)."""

    pid: int

    def terminate(self) -> None: ...

    def kill(self) -> None: ...

    def is_running(self) -> bool: ...

    def status(self) -> str: ...


def serves_port(cmdline: list[str], port: int) -> bool:
    """True when *cmdline* is an ``appium server --port <port>`` invocation.

    Matches the agent's own spawn shape (see ``_start_appium_server``), which
    survives the shebang rewrite that puts the node interpreter at argv[0].
    """
    if "server" not in cmdline:
        return False
    try:
        port_index = cmdline.index("--port")
    except ValueError:
        return False
    return port_index + 1 < len(cmdline) and cmdline[port_index + 1] == str(port)


def runs_from_runtime_root(cmdline: list[str], runtime_root: str) -> bool:
    """True when any argument is a path under the agent's runtime root."""
    return any(arg.startswith(runtime_root) for arg in cmdline)


def _appium_home_under_root(proc: object, runtime_root: str) -> bool:
    """Fallback ownership signal; unreadable environments simply say 'not ours'."""
    try:
        environ = proc.environ()  # type: ignore[attr-defined]
    except psutil.Error, OSError:
        return False
    return str(environ.get("APPIUM_HOME") or "").startswith(runtime_root)


def find_agent_owned_appium(*, port: int, runtime_root: str, exclude_pids: set[int]) -> ProcessHandle | None:
    """The unmanaged Appium this host started on *port*, or None.

    ``exclude_pids`` is every Appium the manager still tracks: a tracked
    process is by definition not unmanaged, and must never be reclaimed.
    """
    for proc in psutil.process_iter(["pid", "cmdline"]):
        try:
            pid = proc.info["pid"]
            cmdline = proc.info["cmdline"] or []
        except psutil.Error, KeyError:
            continue
        if pid in exclude_pids or not serves_port(cmdline, port):
            continue
        if runs_from_runtime_root(cmdline, runtime_root) or _appium_home_under_root(proc, runtime_root):
            return proc  # type: ignore[no-any-return]
    return None


def _is_gone(proc: ProcessHandle) -> bool:
    """A zombie has released its sockets — for port reclaim it counts as gone."""
    try:
        return not proc.is_running() or proc.status() == psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return True
    except psutil.Error:
        return False


async def _wait_gone(proc: ProcessHandle, timeout: float) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if _is_gone(proc):
            return True
        await asyncio.sleep(POLL_INTERVAL_SEC)
    return _is_gone(proc)


async def terminate_process(proc: ProcessHandle) -> None:
    """SIGTERM, then SIGKILL if it outlives the grace window."""
    with contextlib.suppress(psutil.Error, OSError):
        proc.terminate()
    if await _wait_gone(proc, TERMINATE_GRACE_SEC):
        return
    logger.warning("Appium pid %d ignored SIGTERM during port reclaim; killing", proc.pid)
    with contextlib.suppress(psutil.Error, OSError):
        proc.kill()
    await _wait_gone(proc, KILL_GRACE_SEC)
