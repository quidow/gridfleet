from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.devices.models import ConnectionType, Device, DeviceOperationalState, DeviceReservation, DeviceType
from app.devices.services import state_write_guard
from app.grid.service import GridService
from app.hosts.models import Host
from app.lifecycle.services.actions import LifecyclePolicyActionsService
from app.lifecycle.services.incidents import LifecycleIncidentService
from app.lifecycle.services.policy import LifecyclePolicyService
from app.runs.models import RunState, TestRun
from app.runs.service_lifecycle import RunLifecycleService
from app.runs.service_lifecycle_release import RunReleaseService
from app.runs.service_reservation import RunReservationService
from app.sessions.models import Session, SessionStatus
from tests.fakes import FakeSettingsReader, make_fake_grid
from tests.helpers import test_event_bus as event_bus

_settings = FakeSettingsReader({})
_grid = GridService(settings=_settings)
_release_svc = RunReleaseService(
    publisher=event_bus,
    settings=_settings,
    grid=_grid,
    deferred_stop=AsyncMock(),
)
_lifecycle_svc = RunLifecycleService(publisher=event_bus, settings=_settings, grid=_grid, release=_release_svc)


async def test_force_release_clears_stop_pending(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with state_write_guard.bypass():
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

    real_deferred_stop = LifecyclePolicyService(
        publisher=event_bus,
        settings=_settings,
        actions=LifecyclePolicyActionsService(
            publisher=event_bus, reservation=RunReservationService(), incidents=LifecycleIncidentService()
        ),
        incidents=LifecycleIncidentService(),
        viability=Mock(),
        node_manager=AsyncMock(),
    )
    result = await real_deferred_stop.handle_health_failure(
        db_session, device, source="device_checks", reason="ADB not responsive"
    )
    assert result == "deferred"

    fake_grid = make_fake_grid()
    test_release = RunReleaseService(
        publisher=event_bus,
        settings=_settings,
        grid=fake_grid,
        deferred_stop=real_deferred_stop,
    )
    test_lifecycle = RunLifecycleService(publisher=event_bus, settings=_settings, grid=fake_grid, release=test_release)
    await test_lifecycle.force_release(db_session, run.id)

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
    with state_write_guard.bypass():
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

    class SpyReleaseService(RunReleaseService):
        async def release_devices(self, *args: object, **kwargs: object) -> list:
            result = await super().release_devices(*args, **kwargs)  # type: ignore[misc]
            call_log.append("release_done")
            return result  # type: ignore[return-value]

    async def _spy_deferred_stop(db: object, device: object) -> object:
        call_log.append("helper")

    spy_deferred_stop = AsyncMock()
    spy_deferred_stop.complete_deferred_stop_if_session_ended = _spy_deferred_stop

    fake_grid_2 = make_fake_grid()
    spy_release = SpyReleaseService(
        publisher=event_bus,
        settings=_settings,
        grid=fake_grid_2,
        deferred_stop=spy_deferred_stop,
    )
    spy_lifecycle = RunLifecycleService(publisher=event_bus, settings=_settings, grid=fake_grid_2, release=spy_release)

    await spy_lifecycle.force_release(db_session, run.id)

    # release_devices must complete strictly before the lifecycle helper is
    # invoked on any device — otherwise the helper's internal commits could
    # leak under the run-state transaction.
    assert "release_done" in call_log
    assert "helper" in call_log
    assert call_log.index("release_done") < call_log.index("helper"), call_log
