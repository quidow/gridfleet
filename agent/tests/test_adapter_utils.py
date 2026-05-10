"""Tests for agent_app.pack.adapter_utils helpers."""

from __future__ import annotations

import sys

import pytest

from agent_app.pack import adapter_utils


@pytest.mark.asyncio
async def test_icmp_reachable_success(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[list[str]] = []

    async def fake_run_cmd(cmd: list[str], *, timeout: float) -> str:
        captured.append(cmd)
        return "1 packets transmitted, 1 received"

    monkeypatch.setattr(adapter_utils, "run_cmd", fake_run_cmd)
    monkeypatch.setattr(sys, "platform", "linux")
    ok = await adapter_utils.icmp_reachable("10.0.0.7", timeout=2.0, count=1)
    assert ok is True
    assert captured[0][0].endswith("ping")
    assert "-c" in captured[0]
    assert "1" in captured[0]
    assert "-W" in captured[0]
    assert "2" in captured[0]
    assert "10.0.0.7" in captured[0]


@pytest.mark.asyncio
async def test_icmp_reachable_macos_uses_milliseconds(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[list[str]] = []

    async def fake_run_cmd(cmd: list[str], *, timeout: float) -> str:
        captured.append(cmd)
        return "1 packets received"

    monkeypatch.setattr(adapter_utils, "run_cmd", fake_run_cmd)
    monkeypatch.setattr(sys, "platform", "darwin")
    await adapter_utils.icmp_reachable("10.0.0.7", timeout=1.5, count=1)
    assert "-W" in captured[0]
    assert "1500" in captured[0]


@pytest.mark.asyncio
async def test_icmp_reachable_failure_on_empty_output(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_run_cmd(cmd: list[str], *, timeout: float) -> str:
        return ""

    monkeypatch.setattr(adapter_utils, "run_cmd", fake_run_cmd)
    monkeypatch.setattr(sys, "platform", "linux")
    ok = await adapter_utils.icmp_reachable("10.0.0.7", timeout=2.0, count=1)
    assert ok is False


@pytest.mark.asyncio
async def test_icmp_reachable_failure_when_no_received(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_run_cmd(cmd: list[str], *, timeout: float) -> str:
        return "1 packets transmitted, 0 received"

    monkeypatch.setattr(adapter_utils, "run_cmd", fake_run_cmd)
    monkeypatch.setattr(sys, "platform", "linux")
    ok = await adapter_utils.icmp_reachable("10.0.0.7", timeout=2.0, count=1)
    assert ok is False
