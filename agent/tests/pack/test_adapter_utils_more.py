from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from agent_app.pack.adapter_utils import find_tool, icmp_reachable, run_cmd, tcp_reachable


@pytest.mark.asyncio
async def test_run_cmd_timeout_returns_empty() -> None:
    result = await run_cmd(["sleep", "5"], timeout=0.01)
    assert result == ""


@pytest.mark.asyncio
async def test_tcp_reachable_open_port() -> None:
    # Open a server on a random port and verify tcp_reachable returns True
    async def close_client(_reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(close_client, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1] if server.sockets else 0
    try:
        reachable = await tcp_reachable("127.0.0.1", port, timeout=2.0)
        assert reachable is True
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_run_cmd_filenotfound_returns_empty() -> None:
    result = await run_cmd(["/does/not/exist"], timeout=1.0)
    assert result == ""


def test_find_tool_extra_paths_hit() -> None:

    with patch("os.path.isfile", return_value=True), patch("os.access", return_value=True):
        result = find_tool("missing", extra_paths=["/tmp/missing"])
    assert result == "/tmp/missing"


def test_find_tool_extra_paths_miss() -> None:
    result = find_tool("notfound_xyz", extra_paths=["/tmp/notfound_xyz"])
    assert result == "notfound_xyz"


@pytest.mark.asyncio
async def test_icmp_reachable_bad_params_returns_false() -> None:
    assert await icmp_reachable("127.0.0.1", timeout=0) is False
    assert await icmp_reachable("127.0.0.1", timeout=-1) is False
    assert await icmp_reachable("127.0.0.1", timeout=2, count=0) is False


@pytest.mark.asyncio
async def test_icmp_reachable_parses_received_packets() -> None:

    # Patch run_cmd to return a ping-like output with 1 packet received
    with patch("agent_app.pack.adapter_utils.run_cmd", new_callable=AsyncMock, return_value="1 packets received"):
        reachable = await icmp_reachable("127.0.0.1", timeout=2.0, count=1)
        assert reachable is True
    with patch("agent_app.pack.adapter_utils.run_cmd", new_callable=AsyncMock, return_value="0 packets received"):
        reachable = await icmp_reachable("127.0.0.1", timeout=2.0, count=1)
        assert reachable is False
    with patch("agent_app.pack.adapter_utils.run_cmd", new_callable=AsyncMock, return_value=""):
        reachable = await icmp_reachable("127.0.0.1", timeout=2.0, count=1)
        assert reachable is False


@pytest.mark.asyncio
async def test_icmp_reachable_unparseable_output_returns_false() -> None:
    with patch("agent_app.pack.adapter_utils.run_cmd", new_callable=AsyncMock, return_value="unexpected output"):
        reachable = await icmp_reachable("127.0.0.1", timeout=2.0, count=1)
        assert reachable is False


@pytest.mark.asyncio
async def test_icmp_reachable_nonfinite_timeout() -> None:
    assert await icmp_reachable("127.0.0.1", timeout=float("inf")) is False
    assert await icmp_reachable("127.0.0.1", timeout=float("nan")) is False
