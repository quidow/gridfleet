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


@pytest.mark.asyncio
async def test_health_check_identity_mismatch_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """A different device answering at the stored address is a definitive failure."""

    class _Ctx:
        device_identity_value = "192.168.1.50"
        allow_boot = False
        expected_identity_value = "SER123"

    async def fake_reachable(host: str, port: int, *, timeout: float) -> bool:
        return True

    async def fake_device_info(ip_address: str) -> dict[str, str]:
        assert ip_address == "192.168.1.50"
        return {"serial-number": "STRANGER999"}

    monkeypatch.setattr("adapter.tcp_reachable", fake_reachable)
    monkeypatch.setattr("adapter.normalize.fetch_device_info", fake_device_info)

    result = await Adapter().health_check(_Ctx())

    identity = next(check for check in result if check.check_id == "identity")
    assert identity.ok is False
    assert "STRANGER999" in identity.detail


@pytest.mark.asyncio
async def test_health_check_identity_match_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Ctx:
        device_identity_value = "192.168.1.50"
        allow_boot = False
        expected_identity_value = "SER123"

    async def fake_reachable(host: str, port: int, *, timeout: float) -> bool:
        return True

    async def fake_device_info(ip_address: str) -> dict[str, str]:
        return {"serial-number": "SER123"}

    monkeypatch.setattr("adapter.tcp_reachable", fake_reachable)
    monkeypatch.setattr("adapter.normalize.fetch_device_info", fake_device_info)

    result = await Adapter().health_check(_Ctx())

    identity = next(check for check in result if check.check_id == "identity")
    assert identity.ok is True


@pytest.mark.asyncio
async def test_health_check_identity_inconclusive_query_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    """A transient ECP failure must not flap health — the identity check is omitted."""

    class _Ctx:
        device_identity_value = "192.168.1.50"
        allow_boot = False
        expected_identity_value = "SER123"

    async def fake_reachable(host: str, port: int, *, timeout: float) -> bool:
        return True

    async def failing_device_info(ip_address: str) -> dict[str, str]:
        raise TimeoutError("ECP busy")

    monkeypatch.setattr("adapter.tcp_reachable", fake_reachable)
    monkeypatch.setattr("adapter.normalize.fetch_device_info", failing_device_info)

    result = await Adapter().health_check(_Ctx())

    assert [check.check_id for check in result] == ["ping", "ecp"]
    assert all(check.ok for check in result)


@pytest.mark.asyncio
async def test_health_check_without_expected_identity_skips_verification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Old agents (no expected_identity_value on ctx) keep today's behavior."""

    class _Ctx:
        device_identity_value = "192.168.1.50"
        allow_boot = False

    async def fake_reachable(host: str, port: int, *, timeout: float) -> bool:
        return True

    async def explode(ip_address: str) -> dict[str, str]:
        raise AssertionError("device-info must not be queried without an expected identity")

    monkeypatch.setattr("adapter.tcp_reachable", fake_reachable)
    monkeypatch.setattr("adapter.normalize.fetch_device_info", explode)

    result = await Adapter().health_check(_Ctx())

    assert [check.check_id for check in result] == ["ping", "ecp"]


@pytest.mark.asyncio
async def test_health_check_unreachable_skips_identity_query(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Ctx:
        device_identity_value = "192.168.1.50"
        allow_boot = False
        expected_identity_value = "SER123"

    async def fake_reachable(host: str, port: int, *, timeout: float) -> bool:
        return False

    async def fake_sleep(_delay: float) -> None:
        return None

    async def explode(ip_address: str) -> dict[str, str]:
        raise AssertionError("device-info must not be queried when the device is unreachable")

    monkeypatch.setattr("adapter.tcp_reachable", fake_reachable)
    monkeypatch.setattr("adapter.asyncio.sleep", fake_sleep)
    monkeypatch.setattr("adapter.normalize.fetch_device_info", explode)

    result = await Adapter().health_check(_Ctx())

    assert [check.check_id for check in result] == ["ping", "ecp"]
