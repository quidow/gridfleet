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


@pytest.mark.asyncio
async def test_health_check_retries_once_on_transient_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """A single transient TCP probe failure must not flip ``ping``/``ecp`` to
    unhealthy — the adapter retries once with a short backoff."""

    class _Ctx:
        device_identity_value = "192.168.1.51"
        allow_boot = False

    attempts = {"count": 0}

    async def fake_reachable(host: str, port: int, *, timeout: float) -> bool:
        attempts["count"] += 1
        return attempts["count"] > 1

    async def fake_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr("adapter.tcp_reachable", fake_reachable)
    monkeypatch.setattr("adapter.asyncio.sleep", fake_sleep)

    result = await Adapter().health_check(_Ctx())

    assert attempts["count"] == 2
    assert all(check.ok for check in result)


@pytest.mark.asyncio
async def test_health_check_fails_when_both_attempts_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two consecutive TCP probe failures correctly mark the checks unhealthy."""

    class _Ctx:
        device_identity_value = "192.168.1.52"
        allow_boot = False

    attempts = {"count": 0}

    async def fake_reachable(host: str, port: int, *, timeout: float) -> bool:
        attempts["count"] += 1
        return False

    async def fake_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr("adapter.tcp_reachable", fake_reachable)
    monkeypatch.setattr("adapter.asyncio.sleep", fake_sleep)

    result = await Adapter().health_check(_Ctx())

    assert attempts["count"] == 2
    assert not any(check.ok for check in result)
    assert all(check.detail == "Roku ECP port 8060 unreachable" for check in result)
