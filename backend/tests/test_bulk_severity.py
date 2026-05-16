"""Tests that bulk operation events carry the correct severity."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from app.devices.services.bulk import _bulk_severity

# ---------------------------------------------------------------------------
# Unit tests for _bulk_severity helper
# ---------------------------------------------------------------------------


def test_bulk_severity_all_succeed_is_success() -> None:
    assert _bulk_severity(total=5, succeeded=5, failed=0) == "success"


def test_bulk_severity_all_fail_is_critical() -> None:
    assert _bulk_severity(total=5, succeeded=0, failed=5) == "critical"


def test_bulk_severity_partial_fail_is_warning() -> None:
    assert _bulk_severity(total=5, succeeded=3, failed=2) == "warning"


def test_bulk_severity_single_success_no_failures() -> None:
    assert _bulk_severity(total=1, succeeded=1, failed=0) == "success"


def test_bulk_severity_single_failure_no_successes() -> None:
    assert _bulk_severity(total=1, succeeded=0, failed=1) == "critical"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_session_factory(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch _session_factory_from_db to return a working async context manager."""
    mock_session = AsyncMock()
    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_ctx.__aexit__ = AsyncMock(return_value=None)
    mock_factory = MagicMock(return_value=mock_ctx)
    monkeypatch.setattr("app.devices.services.bulk._session_factory_from_db", lambda _db: mock_factory)

    import app.devices.locking as locking_mod

    monkeypatch.setattr(locking_mod, "lock_device", AsyncMock(return_value=MagicMock()))


# ---------------------------------------------------------------------------
# Integration tests: _run_per_device_node_action passes correct severity
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bulk_completed_succeeds_emits_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When all devices succeed, bulk.operation_completed is emitted with severity='success'."""
    published: list[dict[str, Any]] = []

    async def _fake_publish(name: str, data: dict[str, Any], severity: str | None = None) -> None:
        published.append({"name": name, "data": data, "severity": severity})

    monkeypatch.setattr("app.devices.services.bulk.event_bus.publish", _fake_publish)

    async def _fake_load_existing(_db: object, device_ids: list) -> list:
        return list(device_ids)

    monkeypatch.setattr("app.devices.services.bulk._load_existing_device_ids", _fake_load_existing)
    _make_mock_session_factory(monkeypatch)

    from app.devices.services import bulk as bulk_svc

    async def _ok_action(_db: object, _device: object, _caller: str) -> object:
        return object()

    await bulk_svc._run_per_device_node_action(
        db=MagicMock(),
        device_ids=[uuid4()],
        operation="start_nodes",
        action_fn=_ok_action,
        caller="test",
    )

    bulk_events = [p for p in published if p["name"] == "bulk.operation_completed"]
    assert len(bulk_events) == 1
    assert bulk_events[0]["severity"] == "success"
    assert bulk_events[0]["data"]["succeeded"] == 1
    assert bulk_events[0]["data"]["failed"] == 0


@pytest.mark.asyncio
async def test_bulk_completed_all_fail_emits_critical(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When all devices fail, bulk.operation_completed is emitted with severity='critical'."""
    published: list[dict[str, Any]] = []

    async def _fake_publish(name: str, data: dict[str, Any], severity: str | None = None) -> None:
        published.append({"name": name, "data": data, "severity": severity})

    monkeypatch.setattr("app.devices.services.bulk.event_bus.publish", _fake_publish)

    async def _fake_load_existing(_db: object, device_ids: list) -> list:
        return list(device_ids)

    monkeypatch.setattr("app.devices.services.bulk._load_existing_device_ids", _fake_load_existing)
    _make_mock_session_factory(monkeypatch)

    from app.devices.services import bulk as bulk_svc

    async def _fail_action(_db: object, _device: object, _caller: str) -> object:
        raise RuntimeError("simulated failure")

    await bulk_svc._run_per_device_node_action(
        db=MagicMock(),
        device_ids=[uuid4()],
        operation="start_nodes",
        action_fn=_fail_action,
        caller="test",
    )

    bulk_events = [p for p in published if p["name"] == "bulk.operation_completed"]
    assert len(bulk_events) == 1
    assert bulk_events[0]["severity"] == "critical"
    assert bulk_events[0]["data"]["succeeded"] == 0
    assert bulk_events[0]["data"]["failed"] == 1


@pytest.mark.asyncio
async def test_bulk_completed_partial_emits_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When some devices succeed and some fail, severity is 'warning'."""
    published: list[dict[str, Any]] = []

    async def _fake_publish(name: str, data: dict[str, Any], severity: str | None = None) -> None:
        published.append({"name": name, "data": data, "severity": severity})

    monkeypatch.setattr("app.devices.services.bulk.event_bus.publish", _fake_publish)

    async def _fake_load_existing(_db: object, device_ids: list) -> list:
        return list(device_ids)

    monkeypatch.setattr("app.devices.services.bulk._load_existing_device_ids", _fake_load_existing)
    _make_mock_session_factory(monkeypatch)

    from app.devices.services import bulk as bulk_svc

    call_count = [0]

    async def _counting_action(_db: object, _device: object, _caller: str) -> object:
        call_count[0] += 1
        if call_count[0] == 1:
            return object()  # first device succeeds
        raise RuntimeError("second device fails")

    await bulk_svc._run_per_device_node_action(
        db=MagicMock(),
        device_ids=[uuid4(), uuid4()],
        operation="start_nodes",
        action_fn=_counting_action,
        caller="test",
    )

    bulk_events = [p for p in published if p["name"] == "bulk.operation_completed"]
    assert len(bulk_events) == 1
    assert bulk_events[0]["severity"] == "warning"
    assert bulk_events[0]["data"]["succeeded"] == 1
    assert bulk_events[0]["data"]["failed"] == 1
