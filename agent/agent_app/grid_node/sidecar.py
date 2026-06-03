"""Fast-lane relay sidecar process management.

The sidecar is a small Rust binary (``gridfleet-relay-proxy``, shipped by
the ``gridfleet-agent-relay`` PyPI package) that owns the node's
hub-advertised port: it streams ``/session/{id}/...`` WebDriver commands
straight to Appium and forwards everything else to the Python relay's
loopback control port. See ``docs/reference/architecture.md``.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import logging
import shutil
from dataclasses import dataclass
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from agent_app.config import GridNodeSettings

logger = logging.getLogger(__name__)

RELAY_BINARY_NAME = "gridfleet-relay-proxy"
_STDERR_TAIL_LINES = 50


class RelayBinaryNotFoundError(RuntimeError):
    """AGENT_RELAY_FAST_LANE=on but no relay binary is installed."""


class SidecarExitedError(RuntimeError):
    """The sidecar process died; the node service must be restarted."""


def resolve_relay_binary(settings: GridNodeSettings) -> str | None:
    """Map the fast-lane mode setting to a binary path (None = fallback)."""
    if settings.relay_fast_lane == "off":
        return None
    binary = settings.relay_binary or shutil.which(RELAY_BINARY_NAME)
    if binary is None and settings.relay_fast_lane == "on":
        raise RelayBinaryNotFoundError(
            "AGENT_RELAY_FAST_LANE=on but no gridfleet-relay-proxy binary was found; "
            "install the gridfleet-agent-relay package or set AGENT_RELAY_BINARY"
        )
    return binary


def admin_host(bind_host: str) -> str:
    """Host the agent uses to reach the sidecar's loopback-guarded admin API."""
    return "127.0.0.1" if bind_host in {"0.0.0.0", "::", ""} else bind_host


def build_sidecar_command(
    *,
    binary: str,
    bind_host: str,
    listen_port: int,
    appium_upstream: str,
    control_port: int,
    proxy_timeout_sec: float,
) -> list[str]:
    """CLI contract frozen with relay-proxy/src/main.rs::Args."""
    return [
        binary,
        "--listen",
        f"{bind_host}:{listen_port}",
        "--appium",
        appium_upstream,
        "--control",
        f"http://127.0.0.1:{control_port}",
        "--proxy-timeout",
        str(proxy_timeout_sec),
    ]


@dataclass(frozen=True)
class RelayActivity:
    start_token: str
    idle_sec_by_session: dict[str, float]


class RelaySidecar:
    """Owns one relay sidecar subprocess for one grid node."""

    def __init__(
        self,
        *,
        command: list[str],
        admin_base_url: str,
        startup_timeout_sec: float = 10.0,
    ) -> None:
        self._command = command
        self._admin_base_url = admin_base_url.rstrip("/")
        self._startup_timeout_sec = startup_timeout_sec
        self._process: asyncio.subprocess.Process | None = None
        self._stderr_tail: collections.deque[str] = collections.deque(maxlen=_STDERR_TAIL_LINES)
        self._stderr_task: asyncio.Task[None] | None = None
        self.start_token: str | None = None

    def is_running(self) -> bool:
        return self._process is not None and self._process.returncode is None

    async def start(self) -> None:
        if self.is_running():
            return
        self._stderr_tail.clear()
        self._process = await asyncio.create_subprocess_exec(
            *self._command,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        if self._process.stderr is not None:
            self._stderr_task = asyncio.create_task(self._drain_stderr(self._process.stderr))
        deadline = asyncio.get_running_loop().time() + self._startup_timeout_sec
        async with httpx.AsyncClient(timeout=2.0) as client:
            while True:
                if self._process.returncode is not None:
                    raise SidecarExitedError(
                        f"relay sidecar exited during startup (code={self._process.returncode}): "
                        f"{' | '.join(self._stderr_tail)}"
                    )
                with contextlib.suppress(httpx.HTTPError, ValueError):
                    response = await client.get(f"{self._admin_base_url}/__gridfleet/healthz")
                    if response.status_code == 200:
                        token = response.json().get("start_token")
                        self.start_token = token if isinstance(token, str) else None
                        return
                if asyncio.get_running_loop().time() >= deadline:
                    await self.stop()
                    raise TimeoutError(f"relay sidecar did not become healthy within {self._startup_timeout_sec}s")
                await asyncio.sleep(0.05)

    async def stop(self) -> None:
        process = self._process
        self._process = None
        if self._stderr_task is not None:
            self._stderr_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._stderr_task
            self._stderr_task = None
        if process is None or process.returncode is not None:
            return
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=5.0)
        except TimeoutError:
            process.kill()
            with contextlib.suppress(Exception):
                await process.wait()

    async def fetch_activity(self) -> RelayActivity | None:
        """Per-session idle seconds; None when the sidecar is unreachable."""
        try:
            async with httpx.AsyncClient(timeout=2.0) as client:
                response = await client.get(f"{self._admin_base_url}/__gridfleet/activity")
        except httpx.HTTPError:
            return None
        if response.status_code != 200:
            return None
        try:
            payload = response.json()
        except ValueError:
            return None
        token = payload.get("start_token") if isinstance(payload, dict) else None
        sessions = payload.get("sessions") if isinstance(payload, dict) else None
        if not isinstance(token, str) or not isinstance(sessions, dict):
            return None
        idle: dict[str, float] = {}
        for session_id, info in sessions.items():
            if isinstance(info, dict) and isinstance(info.get("idle_sec"), int | float):
                idle[str(session_id)] = float(info["idle_sec"])
        return RelayActivity(start_token=token, idle_sec_by_session=idle)

    async def _drain_stderr(self, stream: asyncio.StreamReader) -> None:
        # The binary logs errors only; keep a small tail for diagnostics and
        # never let an unread PIPE buffer block the child.
        while True:
            line = await stream.readline()
            if not line:
                return
            text = line.decode(errors="replace").rstrip("\n")
            self._stderr_tail.append(text)
            logger.warning("relay_sidecar_stderr", extra={"line": text})
