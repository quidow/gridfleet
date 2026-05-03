from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.device import (
    ConnectionType,
    Device,
    DeviceAvailabilityStatus,
    DeviceType,
)
from app.models.device_reservation import DeviceReservation
from app.models.host import Host
from app.models.session import Session, SessionStatus
from app.models.test_run import RunState, TestRun
from app.services import grid_service, run_service
from app.services.lifecycle_policy import handle_health_failure


async def test_force_release_clears_stop_pending(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value="policy-stuck-stop-3",
        connection_target="policy-stuck-stop-3",
        name="Stuck Deferred Stop Device 3",
        os_version="14",
        host_id=db_host.id,
        availability_status=DeviceAvailabilityStatus.busy,
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )
    db_session.add(device)
    run = TestRun(
        id=uuid4(),
        name="run-stuck-stop-3",
        state=RunState.active,
        requirements=[],
        ttl_minutes=10,
        heartbeat_timeout_sec=300,
        last_heartbeat=datetime.now(UTC),
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
    )
    db_session.add(reservation)
    session = Session(
        session_id="sess-stuck-stop-3",
        device_id=device.id,
        run_id=run.id,
        status=SessionStatus.running,
    )
    db_session.add(session)
    await db_session.commit()

    result = await handle_health_failure(db_session, device, source="device_checks", reason="ADB not responsive")
    assert result == "deferred"

    async def _fake_terminate(_session_id: str) -> bool:
        return True

    monkeypatch.setattr(grid_service, "terminate_grid_session", _fake_terminate)

    await run_service.force_release(db_session, run.id)

    reloaded = await db_session.get(Device, device.id)
    assert reloaded is not None
    assert reloaded.lifecycle_policy_state is not None
    assert reloaded.lifecycle_policy_state["stop_pending"] is False
