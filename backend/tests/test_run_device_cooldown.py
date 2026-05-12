"""Tests for the run device cooldown endpoint."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from httpx import AsyncClient  # noqa: TC002
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: TC002

from app.models.device import Device, DeviceHold
from app.models.device_reservation import DeviceReservation
from app.models.test_run import RunState
from app.services.settings_service import settings_service
from tests.helpers import create_device_record
from tests.pack.factories import seed_test_packs


@pytest.fixture(autouse=True)
async def _seed_packs(db_session: AsyncSession) -> None:
    await seed_test_packs(db_session)
    await db_session.commit()


async def _create_available_device(
    db_session: AsyncSession,
    host_id: str,
    identity_value: str,
) -> Device:
    return await create_device_record(
        db_session,
        host_id=host_id,
        identity_value=identity_value,
        connection_target=identity_value,
        name=f"Device {identity_value}",
        operational_state="available",
    )


async def _create_run(client: AsyncClient, **overrides: object) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "name": "Test Run",
        "requirements": [{"pack_id": "appium-uiautomator2", "platform_id": "android_mobile", "count": 1}],
        **overrides,
    }
    resp = await client.post("/api/runs", json=payload)
    assert resp.status_code == 201
    return dict(resp.json())


async def test_cooldown_device_success(client: AsyncClient, db_session: AsyncSession, default_host_id: str) -> None:
    device = await _create_available_device(db_session, default_host_id, "cooldown-001")
    run = await _create_run(client)
    run_id = run["id"]
    device_id = str(device.id)

    resp = await client.post(
        f"/api/runs/{run_id}/devices/{device_id}/cooldown",
        json={"reason": "flaky connection", "ttl_seconds": 120},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "cooldown_set"
    assert data["cooldown_count"] == 1
    assert "excluded_until" in data

    # Verify DB state
    await db_session.refresh(device)
    entry = (
        await db_session.execute(
            select(DeviceReservation).where(
                DeviceReservation.run_id == uuid.UUID(run_id),
                DeviceReservation.device_id == device.id,
            )
        )
    ).scalar_one()
    assert entry.excluded is True
    assert entry.cooldown_count == 1
    assert entry.exclusion_reason == "flaky connection"
    assert entry.excluded_until is not None


async def test_cooldown_device_not_found_run(client: AsyncClient) -> None:
    resp = await client.post(
        f"/api/runs/{uuid.uuid4()}/devices/{uuid.uuid4()}/cooldown",
        json={"reason": "flaky", "ttl_seconds": 60},
    )
    assert resp.status_code == 404


async def test_cooldown_device_not_reserved(
    client: AsyncClient, db_session: AsyncSession, default_host_id: str
) -> None:
    await _create_available_device(db_session, default_host_id, "cooldown-nr")
    run = await _create_run(client)
    # Try to cooldown a different device
    resp = await client.post(
        f"/api/runs/{run['id']}/devices/{uuid.uuid4()}/cooldown",
        json={"reason": "flaky", "ttl_seconds": 60},
    )
    assert resp.status_code == 404


async def test_cooldown_device_ttl_too_high(
    client: AsyncClient, db_session: AsyncSession, default_host_id: str
) -> None:
    device = await _create_available_device(db_session, default_host_id, "cooldown-ttl")
    run = await _create_run(client)
    max_ttl = int(settings_service.get("general.device_cooldown_max_sec"))
    resp = await client.post(
        f"/api/runs/{run['id']}/devices/{device.id}/cooldown",
        json={"reason": "flaky", "ttl_seconds": max_ttl + 1},
    )
    assert resp.status_code == 422


async def test_cooldown_device_terminal_run(
    client: AsyncClient, db_session: AsyncSession, default_host_id: str
) -> None:
    device = await _create_available_device(db_session, default_host_id, "cooldown-term")
    run = await _create_run(client)
    run_id = uuid.UUID(run["id"])

    # Complete the run
    from app.services import run_service as rs

    run_obj = await rs.get_run(db_session, run_id)
    assert run_obj is not None
    run_obj.state = RunState.completed
    await db_session.commit()

    resp = await client.post(
        f"/api/runs/{run_id}/devices/{device.id}/cooldown",
        json={"reason": "flaky", "ttl_seconds": 60},
    )
    assert resp.status_code == 409


async def test_cooldown_device_escalation(
    client: AsyncClient, db_session: AsyncSession, default_host_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(settings_service._cache, "general.device_cooldown_escalation_threshold", 2)
    device = await _create_available_device(db_session, default_host_id, "cooldown-esc")
    run = await _create_run(client)
    run_id = run["id"]
    device_id = str(device.id)

    # First cooldown
    resp = await client.post(
        f"/api/runs/{run_id}/devices/{device_id}/cooldown",
        json={"reason": "flaky", "ttl_seconds": 60},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "cooldown_set"

    # Second cooldown triggers escalation
    resp = await client.post(
        f"/api/runs/{run_id}/devices/{device_id}/cooldown",
        json={"reason": "flaky again", "ttl_seconds": 60},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "maintenance_escalated"
    assert data["cooldown_count"] == 2
    assert data["threshold"] == 2

    await db_session.refresh(device)
    assert device.hold == DeviceHold.maintenance


async def test_cooldown_device_increments_count(
    client: AsyncClient, db_session: AsyncSession, default_host_id: str
) -> None:
    device = await _create_available_device(db_session, default_host_id, "cooldown-inc")
    run = await _create_run(client)
    run_id = run["id"]
    device_id = str(device.id)

    for i in range(1, 4):
        resp = await client.post(
            f"/api/runs/{run_id}/devices/{device_id}/cooldown",
            json={"reason": f"flaky {i}", "ttl_seconds": 60},
        )
        assert resp.status_code == 200
        assert resp.json()["cooldown_count"] == i

    entry = (
        await db_session.execute(
            select(DeviceReservation).where(
                DeviceReservation.run_id == uuid.UUID(run_id),
                DeviceReservation.device_id == device.id,
            )
        )
    ).scalar_one()
    assert entry.cooldown_count == 3
