from __future__ import annotations

import pytest

from agent_app.pack.adapter_utils import find_tool, run_cmd, tcp_reachable


@pytest.mark.asyncio
async def test_run_cmd_echo() -> None:
    result = await run_cmd(["echo", "hello"])
    assert result == "hello"


@pytest.mark.asyncio
async def test_run_cmd_not_found() -> None:
    result = await run_cmd(["nonexistent_binary_xyz_123"])
    assert result == ""


@pytest.mark.asyncio
async def test_tcp_reachable_closed_port() -> None:
    reachable = await tcp_reachable("127.0.0.1", 1, timeout=1.0)
    assert reachable is False


def test_find_tool_on_path() -> None:
    result = find_tool("python3")
    assert "python" in result


def test_find_tool_not_found() -> None:
    result = find_tool("nonexistent_tool_xyz_123")
    assert result == "nonexistent_tool_xyz_123"
