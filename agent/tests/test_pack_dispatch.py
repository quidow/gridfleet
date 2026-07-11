"""Tests for the pack worker dispatch helper behavior."""

from __future__ import annotations

import pytest

from agent_app.pack.adapter_types import HealthCheckResult, NormalizedDevice
from agent_app.pack.contexts import HealthCtx
from tests.pack.adapter_test_helpers import adapter_health_check, adapter_normalize_device


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
        adapter_registry=registry,  # type: ignore[arg-type]
        pack_id="pkg",
        pack_release="1.0.0",
        ctx=HealthCtx(
            device_identity_value="abc",
            allow_boot=False,
            platform_id="p",
            device_type="real_device",
            connection_type="usb",
            ip_address="10.0.0.7",
            ip_ping_timeout_sec=1.5,
            ip_ping_count=2,
        ),
    )
    assert payload == {
        "healthy": True,
        "checks": [{"check_id": "x", "ok": True, "message": "", "debounce": False}],
    }
    assert adapter.last_ctx.ip_address == "10.0.0.7"
    assert adapter.last_ctx.ip_ping_timeout_sec == 1.5
    assert adapter.last_ctx.ip_ping_count == 2


@pytest.mark.asyncio
async def test_adapter_health_check_threads_expected_identity() -> None:
    adapter = _StubAdapter()
    registry = _StubRegistry(adapter)
    await adapter_health_check(
        adapter_registry=registry,  # type: ignore[arg-type]
        pack_id="pkg",
        pack_release="1.0.0",
        ctx=HealthCtx(
            device_identity_value="10.0.0.5",
            allow_boot=False,
            expected_identity_value="SER123",
        ),
    )
    assert getattr(adapter.last_ctx, "expected_identity_value", None) == "SER123"


@pytest.mark.asyncio
async def test_adapter_health_check_expected_identity_defaults_to_none() -> None:
    adapter = _StubAdapter()
    registry = _StubRegistry(adapter)
    await adapter_health_check(
        adapter_registry=registry,  # type: ignore[arg-type]
        pack_id="pkg",
        pack_release="1.0.0",
        ctx=HealthCtx(device_identity_value="10.0.0.5", allow_boot=False),
    )
    assert getattr(adapter.last_ctx, "expected_identity_value", "missing") is None


def test_adapter_health_payload_carries_debounce_flag() -> None:
    from agent_app.pack.router import _adapter_health_payload

    payload = _adapter_health_payload(
        [
            HealthCheckResult(check_id="ping", ok=False, debounce=True),
            HealthCheckResult(check_id="adb_connected", ok=True),
        ]
    )
    assert payload["checks"][0]["debounce"] is True
    assert payload["checks"][1]["debounce"] is False


class _NormalizeStubAdapter:
    pack_id = "pkg"
    pack_release = "1.0.0"

    async def normalize_device(self, ctx: object) -> NormalizedDevice:
        return NormalizedDevice(
            identity_scheme="serial",
            identity_scope="global",
            identity_value="SER1",
            connection_target="10.0.0.9:5555",
            ip_address="10.0.0.9",
            device_type="real_device",
            connection_type="wifi",
            os_version="17.5",
            field_errors=[],
            os_version_display="17.5.1",
        )


@pytest.mark.asyncio
async def test_adapter_normalize_device_includes_os_version_display() -> None:
    adapter = _NormalizeStubAdapter()
    registry = _StubRegistry(adapter)  # type: ignore[arg-type]
    payload = await adapter_normalize_device(
        adapter_registry=registry,  # type: ignore[arg-type]
        pack_id="pkg",
        pack_release="1.0.0",
        host_id="h1",
        platform_id="android",
        raw_input={"connection_target": "10.0.0.9:5555"},
    )
    assert payload is not None
    assert payload["os_version_display"] == "17.5.1"
