from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.grid import service as grid_service
from app.models.device import (
    ConnectionType,
    Device,
    DeviceOperationalState,
    DeviceType,
)
from app.models.device_reservation import DeviceReservation
from app.models.host import Host
from app.models.session import Session, SessionStatus
from app.models.test_run import RunState, TestRun
from app.services import run_service
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
        operational_state=DeviceOperationalState.busy,
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


async def test_release_devices_defers_lifecycle_cleanup_until_after_commit(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_release_devices`` must NOT invoke
    ``complete_deferred_stop_if_session_ended`` while the run-state transaction
    is open.

    Calling the lifecycle helper inline would surface partial commits on the
    caller's transaction (the helper commits internally via
    ``handle_node_crash``). Audit P1 — collect device IDs during
    ``_release_devices`` and run cleanup only after the run-state commit.
    """
    device = Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value="policy-release-commit-boundary",
        connection_target="policy-release-commit-boundary",
        name="Release Commit Boundary",
        os_version="14",
        host_id=db_host.id,
        operational_state=DeviceOperationalState.busy,
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )
    db_session.add(device)
    run = TestRun(
        id=uuid4(),
        name="run-release-commit-boundary",
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
        session_id="sess-release-commit-boundary",
        device_id=device.id,
        run_id=run.id,
        status=SessionStatus.running,
    )
    db_session.add(session)
    await db_session.commit()

    call_log: list[str] = []

    real_release = run_service._release_devices
    real_helper = run_service.lifecycle_policy.complete_deferred_stop_if_session_ended

    async def _spy_release(*args: object, **kwargs: object) -> list:
        # Marker is recorded AFTER awaiting the real implementation so the
        # ordering assertion proves the helper ran strictly after
        # ``_release_devices`` returned (i.e. after the run-state commit
        # window closed). Logging before the await would also pass for a
        # regression where the helper runs inside ``_release_devices``.
        result = await real_release(*args, **kwargs)
        call_log.append("release_done")
        return result

    async def _spy_helper(*args: object, **kwargs: object) -> object:
        call_log.append("helper")
        return await real_helper(*args, **kwargs)

    async def _fake_terminate(_session_id: str) -> bool:
        return True

    monkeypatch.setattr(run_service, "_release_devices", _spy_release)
    monkeypatch.setattr(
        run_service.lifecycle_policy,
        "complete_deferred_stop_if_session_ended",
        _spy_helper,
    )
    monkeypatch.setattr(grid_service, "terminate_grid_session", _fake_terminate)

    await run_service.force_release(db_session, run.id)

    # _release_devices must complete strictly before the lifecycle helper is
    # invoked on any device — otherwise the helper's internal commits could
    # leak under the run-state transaction.
    assert "release_done" in call_log
    assert "helper" in call_log
    assert call_log.index("release_done") < call_log.index("helper"), call_log
