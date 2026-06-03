from __future__ import annotations

import socket
import sys
from pathlib import Path

import pytest

from agent_app.config import GridNodeSettings
from agent_app.grid_node.sidecar import (
    RelayBinaryNotFoundError,
    RelaySidecar,
    SidecarExitedError,
    admin_host,
    build_sidecar_command,
    resolve_relay_binary,
)

FAKE_SIDECAR = str(Path(__file__).parent / "fake_relay_sidecar.py")


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _sidecar(port: int, token: str = "tok-1", *extra: str) -> RelaySidecar:
    return RelaySidecar(
        command=[sys.executable, FAKE_SIDECAR, str(port), token, *extra],
        admin_base_url=f"http://127.0.0.1:{port}",
        startup_timeout_sec=5.0,
    )


async def test_start_waits_for_healthz_and_captures_token() -> None:
    sidecar = _sidecar(_free_port(), "tok-abc")
    try:
        await sidecar.start()
        assert sidecar.is_running()
        assert sidecar.start_token == "tok-abc"
    finally:
        await sidecar.stop()
    assert not sidecar.is_running()


async def test_fetch_activity_parses_sessions() -> None:
    sidecar = _sidecar(_free_port())
    try:
        await sidecar.start()
        activity = await sidecar.fetch_activity()
        assert activity is not None
        assert activity.start_token == "tok-1"
        assert activity.idle_sec_by_session == {"sess-1": 2.5}
    finally:
        await sidecar.stop()


async def test_fetch_activity_returns_none_when_unreachable() -> None:
    sidecar = _sidecar(_free_port())
    # Never started: connection refused -> None, not an exception.
    assert await sidecar.fetch_activity() is None


async def test_start_raises_when_process_exits_immediately() -> None:
    sidecar = _sidecar(_free_port(), "tok-1", "--exit-immediately")
    with pytest.raises(SidecarExitedError, match="exiting immediately"):
        await sidecar.start()


def test_resolve_relay_binary_modes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake = tmp_path / "gridfleet-relay-proxy"
    fake.write_text("#!/bin/sh\n")

    assert resolve_relay_binary(GridNodeSettings(relay_fast_lane="off")) is None
    assert resolve_relay_binary(GridNodeSettings(relay_fast_lane="auto", relay_binary=str(fake))) == str(fake)
    monkeypatch.setattr("shutil.which", lambda _name: None)
    assert resolve_relay_binary(GridNodeSettings(relay_fast_lane="auto")) is None
    with pytest.raises(RelayBinaryNotFoundError):
        resolve_relay_binary(GridNodeSettings(relay_fast_lane="on"))


def test_build_sidecar_command_contract() -> None:
    command = build_sidecar_command(
        binary="/usr/local/bin/gridfleet-relay-proxy",
        bind_host="0.0.0.0",
        listen_port=5555,
        appium_upstream="http://127.0.0.1:4723",
        control_port=7900,
        proxy_timeout_sec=60.0,
    )
    assert command == [
        "/usr/local/bin/gridfleet-relay-proxy",
        "--listen",
        "0.0.0.0:5555",
        "--appium",
        "http://127.0.0.1:4723",
        "--control",
        "http://127.0.0.1:7900",
        "--proxy-timeout",
        "60.0",
    ]


def test_admin_host_prefers_loopback_for_wildcard_binds() -> None:
    assert admin_host("0.0.0.0") == "127.0.0.1"
    assert admin_host("::") == "127.0.0.1"
    assert admin_host("192.168.1.10") == "192.168.1.10"
