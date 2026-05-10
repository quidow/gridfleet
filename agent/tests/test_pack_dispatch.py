"""Tests for agent_app.pack.dispatch helper functions."""

from __future__ import annotations

import pytest

from agent_app.pack.adapter_types import HealthCheckResult
from agent_app.pack.dispatch import adapter_health_check


class _StubAdapter:
    pack_id = "pkg"
    pack_release = "1.0.0"

    def __init__(self) -> None:
        self.last_ctx: object = None

    async def health_check(self, ctx: object) -> list[HealthCheckResult]:
        self.last_ctx = ctx
        return [HealthCheckResult(check_id="x", ok=True, detail="")]


class _StubRegistry:
    def __init__(self, adapter: _StubAdapter) -> None:
        self._adapter = adapter

    def get(self, pack_id: str, pack_release: str) -> _StubAdapter:
        return self._adapter


@pytest.mark.asyncio
async def test_adapter_health_check_threads_ip_ping_fields() -> None:
    adapter = _StubAdapter()
    registry = _StubRegistry(adapter)
    payload = await adapter_health_check(
        adapter_registry=registry,
        pack_id="pkg",
        pack_release="1.0.0",
        identity_value="abc",
        allow_boot=False,
        platform_id="p",
        device_type="real_device",
        connection_type="usb",
        ip_address="10.0.0.7",
        ip_ping_timeout_sec=1.5,
        ip_ping_count=2,
    )
    assert payload == {
        "healthy": True,
        "checks": [{"check_id": "x", "ok": True, "message": ""}],
    }
    assert adapter.last_ctx.ip_address == "10.0.0.7"
    assert adapter.last_ctx.ip_ping_timeout_sec == 1.5
    assert adapter.last_ctx.ip_ping_count == 2
