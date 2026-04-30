from __future__ import annotations

import inspect

import pytest
from adapter import Adapter

REQUIRED_METHODS = {
    "discover",
    "doctor",
    "health_check",
    "lifecycle_action",
    "pre_session",
    "post_session",
    "normalize_device",
    "telemetry",
    "feature_action",
    "sidecar_lifecycle",
}


def test_adapter_protocol() -> None:
    methods = {name for name, _ in inspect.getmembers(Adapter, predicate=inspect.isfunction)}
    assert REQUIRED_METHODS.issubset(methods)


@pytest.mark.asyncio
async def test_discover_returns_empty() -> None:
    class _Ctx:
        host_id = "h1"
        platform_id = "roku_real"

    adapter = Adapter()
    assert await adapter.discover(_Ctx()) == []


@pytest.mark.asyncio
async def test_health_check_uses_manifest_check_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Ctx:
        device_identity_value = "192.168.1.50"
        allow_boot = False

    async def fake_reachable(host: str, port: int, *, timeout: float) -> bool:
        assert host == "192.168.1.50"
        assert port == 8060
        assert timeout == 5.0
        return True

    monkeypatch.setattr("adapter.tcp_reachable", fake_reachable)

    result = await Adapter().health_check(_Ctx())

    assert [check.check_id for check in result] == ["ping", "ecp"]
    assert all(check.ok for check in result)
