"""Shared utilities available to adapter packages."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import os
import re
import shutil
import sys

logger = logging.getLogger(__name__)


async def run_cmd(cmd: list[str], *, timeout: float = 30.0) -> str:
    """Run a subprocess and return stripped stdout. Returns an empty string on failure."""
    proc: asyncio.subprocess.Process | None = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return (stdout or b"").decode().strip()
    except FileNotFoundError:
        logger.warning("command not found: %s", cmd[0])
        return ""
    except TimeoutError:
        logger.warning("command timed out: %s", " ".join(cmd))
        if proc is not None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            with contextlib.suppress(Exception):
                await proc.wait()
        return ""


async def tcp_reachable(host: str, port: int, *, timeout: float = 5.0) -> bool:
    """Return True if a TCP connection to host:port succeeds."""
    try:
        _reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port),
            timeout=timeout,
        )
        writer.close()
        await writer.wait_closed()
        return True
    except (OSError, TimeoutError):
        return False


def find_tool(name: str, extra_paths: list[str] | None = None) -> str:
    """Locate a tool binary. Returns the name itself as fallback."""
    found = shutil.which(name)
    if found:
        return found
    for path in extra_paths or []:
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return name


_RECEIVED_RE = re.compile(r"(\d+)\s+(?:packets\s+)?received")


_PING_INTER_PROBE_INTERVAL_SEC = 1.0


async def icmp_reachable(host: str, *, timeout: float = 2.0, count: int = 1) -> bool:
    """Return True iff the host responds to at least one ICMP echo within the timeout.

    Uses the system ``ping`` binary. Returns False if the binary is missing,
    if the subprocess fails, if ``timeout`` is non-finite or non-positive, or
    if the parsed response shows zero packets received.
    """

    if not math.isfinite(timeout) or timeout <= 0 or count < 1:
        return False

    if sys.platform == "darwin":
        wait_arg = str(int(max(timeout, 0.001) * 1000))
    else:
        wait_arg = str(max(1, math.ceil(timeout)))

    # ``ping`` sends probes at ~1s intervals by default, so the subprocess must
    # outlive ``(count - 1) * interval + timeout`` plus a small buffer. Earlier
    # bound (``timeout * count + 1``) killed multi-probe runs early on slow
    # links.
    subprocess_timeout = (count - 1) * _PING_INTER_PROBE_INTERVAL_SEC + timeout + 2.0

    cmd = ["ping", "-c", str(count), "-W", wait_arg, host]
    output = await run_cmd(cmd, timeout=subprocess_timeout)
    if not output:
        return False
    match = _RECEIVED_RE.search(output)
    if not match:
        return False
    return int(match.group(1)) >= 1
