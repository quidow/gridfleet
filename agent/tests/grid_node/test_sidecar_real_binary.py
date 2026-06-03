"""Integration: RelaySidecar driving the real Rust binary.

Skipped unless the binary exists — built via `cargo build` in relay-proxy/
or installed from the gridfleet-agent-relay package. CI for the agent does
not build it; the relay-proxy workflow covers the Rust side. This test is
the cross-language contract check for local/full-stack runs.
"""

from __future__ import annotations

import asyncio
import shutil
import socket
from pathlib import Path

import httpx
import pytest

from agent_app.grid_node.sidecar import RelaySidecar, build_sidecar_command

_REPO_ROOT = Path(__file__).resolve().parents[3]


def _find_binary() -> str | None:
    for candidate in (
        shutil.which("gridfleet-relay-proxy"),
        _REPO_ROOT / "relay-proxy" / "target" / "debug" / "gridfleet-relay-proxy",
        _REPO_ROOT / "relay-proxy" / "target" / "release" / "gridfleet-relay-proxy",
    ):
        if candidate and Path(candidate).exists():
            return str(candidate)
    return None


BINARY = _find_binary()
pytestmark = pytest.mark.skipif(BINARY is None, reason="gridfleet-relay-proxy binary not built/installed")


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def _stub_upstream(name: str, port: int) -> asyncio.AbstractServer:
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readline()
        while (await reader.readline()) not in (b"\r\n", b"\n", b""):
            pass
        body = f'{{"upstream": "{name}"}}'.encode()
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
            + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
            + body
        )
        await writer.drain()
        writer.close()

    return await asyncio.start_server(handle, "127.0.0.1", port)


async def test_real_binary_routes_and_reports_activity() -> None:
    assert BINARY is not None
    appium_port, control_port, listen_port = _free_port(), _free_port(), _free_port()
    appium = await _stub_upstream("appium", appium_port)
    control = await _stub_upstream("control", control_port)
    sidecar = RelaySidecar(
        command=build_sidecar_command(
            binary=BINARY,
            bind_host="127.0.0.1",
            listen_port=listen_port,
            appium_upstream=f"http://127.0.0.1:{appium_port}",
            control_port=control_port,
            proxy_timeout_sec=5.0,
        ),
        admin_base_url=f"http://127.0.0.1:{listen_port}",
    )
    try:
        await sidecar.start()
        assert sidecar.start_token
        async with httpx.AsyncClient(timeout=5.0) as client:
            fast = await client.get(f"http://127.0.0.1:{listen_port}/session/abc/element")
            assert fast.json()["upstream"] == "appium"
            slow = await client.get(f"http://127.0.0.1:{listen_port}/status")
            assert slow.json()["upstream"] == "control"
        activity = await sidecar.fetch_activity()
        assert activity is not None
        assert "abc" in activity.idle_sec_by_session
        assert activity.start_token == sidecar.start_token
    finally:
        await sidecar.stop()
        appium.close()
        control.close()
