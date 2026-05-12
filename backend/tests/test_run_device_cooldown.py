"""Tests for the run device cooldown endpoint."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import AsyncClient  # noqa: TC002
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: TC002

from app.models.appium_node import AppiumDesiredState, AppiumNode
from app.models.device import Device, DeviceHold, DeviceOperationalState
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


async def test_cooldown_preserves_desired_grid_run_id(
    client: AsyncClient, db_session: AsyncSession, default_host_id: str
) -> None:
    device = await _create_available_device(db_session, default_host_id, "cooldown-grid")
    run = await _create_run(client)
    run_id = uuid.UUID(run["id"])

    # Set up an AppiumNode with the run's grid_run_id
    node = AppiumNode(
        device_id=device.id,
        port=4723,
        grid_url="http://grid:4444",
        pid=1234,
        active_connection_target=device.connection_target,
        desired_grid_run_id=run_id,
        grid_run_id=run_id,
    )
    db_session.add(node)
    await db_session.commit()

    resp = await client.post(
        f"/api/runs/{run_id}/devices/{device.id}/cooldown",
        json={"reason": "flaky", "ttl_seconds": 60},
    )
    assert resp.status_code == 200

    await db_session.refresh(node)
    # desired_grid_run_id must stay set so the device does not fall into the
    # free Grid pool during cooldown.  The reservation excluded flag is the
    # signal, not the node tag.
    assert node.desired_grid_run_id == run_id
    # The node must be stopped so Grid cannot route new sessions during TTL.
    assert node.desired_state == AppiumDesiredState.stopped


async def test_cooldown_does_not_mutate_operational_state(
    client: AsyncClient, db_session: AsyncSession, default_host_id: str
) -> None:
    device = await _create_available_device(db_session, default_host_id, "cooldown-state")
    run = await _create_run(client)
    run_id = run["id"]

    # Simulate an active session by flipping to busy after reservation.
    device.operational_state = DeviceOperationalState.busy
    await db_session.commit()

    resp = await client.post(
        f"/api/runs/{run_id}/devices/{device.id}/cooldown",
        json={"reason": "flaky", "ttl_seconds": 60},
    )
    assert resp.status_code == 200

    await db_session.refresh(device)
    assert device.operational_state == DeviceOperationalState.busy


async def test_reserved_device_info_reflects_expired_cooldown(db_session: AsyncSession, default_host_id: str) -> None:
    """to_reserved_device_info should report excluded=false once excluded_until passes."""
    from app.models.test_run import TestRun

    device = await create_device_record(
        db_session,
        host_id=default_host_id,
        identity_value="expired-cooldown",
        name="Expired Cooldown",
        operational_state="available",
    )
    run = TestRun(
        name="expired-run",
        state=RunState.active,
        requirements=[{"platform_id": "android_mobile", "count": 1}],
        ttl_minutes=60,
        heartbeat_timeout_sec=120,
    )
    db_session.add(run)
    await db_session.flush()

    reservation = DeviceReservation(
        run_id=run.id,
        device_id=device.id,
        identity_value=device.identity_value,
        connection_target=device.connection_target,
        pack_id=device.pack_id,
        platform_id=device.platform_id,
        os_version=device.os_version,
        excluded=True,
        exclusion_reason="flaky",
        excluded_at=datetime.now(UTC) - timedelta(seconds=120),
        excluded_until=datetime.now(UTC) - timedelta(seconds=60),
        cooldown_count=1,
    )
    db_session.add(reservation)
    await db_session.commit()

    info = reservation.to_reserved_device_info()
    assert info["excluded"] is False
    assert info["cooldown_remaining_sec"] == 0
    assert info["cooldown_count"] == 1


async def test_cooldown_stops_appium_node(client: AsyncClient, db_session: AsyncSession, default_host_id: str) -> None:
    device = await _create_available_device(db_session, default_host_id, "cooldown-stop")
    run = await _create_run(client)
    run_id = uuid.UUID(run["id"])

    node = AppiumNode(
        device_id=device.id,
        port=4723,
        grid_url="http://grid:4444",
        pid=1234,
        active_connection_target=device.connection_target,
        desired_grid_run_id=run_id,
        grid_run_id=run_id,
        desired_state=AppiumDesiredState.running,
    )
    db_session.add(node)
    await db_session.commit()

    resp = await client.post(
        f"/api/runs/{run_id}/devices/{device.id}/cooldown",
        json={"reason": "flaky", "ttl_seconds": 60},
    )
    assert resp.status_code == 200

    await db_session.refresh(node)
    assert node.desired_state == AppiumDesiredState.stopped


async def test_expired_cooldown_restores_and_restarts_node(db_session: AsyncSession, default_host_id: str) -> None:
    from app.models.test_run import TestRun
    from app.services.device_connectivity import _check_expired_cooldowns

    device = await create_device_record(
        db_session,
        host_id=default_host_id,
        identity_value="expired-restore",
        name="Expired Restore",
        operational_state="available",
    )
    run = TestRun(
        name="expired-run",
        state=RunState.active,
        requirements=[{"platform_id": "android_mobile", "count": 1}],
        ttl_minutes=60,
        heartbeat_timeout_sec=120,
    )
    db_session.add(run)
    await db_session.flush()

    node = AppiumNode(
        device_id=device.id,
        port=4723,
        grid_url="http://grid:4444",
        pid=1234,
        active_connection_target=device.connection_target,
        desired_state=AppiumDesiredState.stopped,
    )
    db_session.add(node)

    reservation = DeviceReservation(
        run_id=run.id,
        device_id=device.id,
        identity_value=device.identity_value,
        connection_target=device.connection_target,
        pack_id=device.pack_id,
        platform_id=device.platform_id,
        os_version=device.os_version,
        excluded=True,
        exclusion_reason="flaky",
        excluded_at=datetime.now(UTC) - timedelta(seconds=120),
        excluded_until=datetime.now(UTC) - timedelta(seconds=1),
        cooldown_count=1,
    )
    db_session.add(reservation)
    await db_session.commit()

    await _check_expired_cooldowns(db_session)

    await db_session.refresh(reservation)
    assert reservation.excluded is False
    assert reservation.exclusion_reason is None
    assert reservation.excluded_until is None

    await db_session.refresh(node)
    assert node.desired_state == AppiumDesiredState.running
