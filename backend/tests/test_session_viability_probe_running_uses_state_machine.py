"""Regression: probe-running busy mark must be a SESSION_STARTED state-machine
transition, not a direct ``set_operational_state(busy)`` write. State-machine
routing fires EventLogHook (DeviceEvent row), IncidentHook, and RunExclusionHook
side effects; the previous direct write skipped them.
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.appium_nodes.models import AppiumDesiredState, AppiumNode
from app.devices.models import ConnectionType, Device, DeviceOperationalState, DeviceType
from app.devices.models.event import DeviceEvent, DeviceEventType
from app.devices.services import state_write_guard
from app.devices.services.capability import DeviceCapabilityService
from app.hosts.models import Host
from app.sessions import service_viability as session_viability
from app.sessions.service_viability import SessionViabilityService
from tests.fakes import FakeSettingsReader

pytestmark = pytest.mark.usefixtures("seeded_driver_packs")


async def test_probe_running_busy_mark_writes_device_event_row(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    """The busy-mark transition at probe start must go through DeviceStateMachine
    so EventLogHook writes a DeviceEvent row with event_type=session_started.

    Before conversion: ``set_operational_state(busy)`` was called directly —
    it emitted a bus event but did NOT add a DeviceEvent ORM row.
    After conversion: ``_MACHINE.transition(SESSION_STARTED)`` routes through
    EventLogHook which adds the DeviceEvent row in the same transaction.
    """
    with state_write_guard.bypass():
        device = Device(
            pack_id="appium-uiautomator2",
            platform_id="android_mobile",
            identity_scheme="android_serial",
            identity_scope="host",
            identity_value="probe-sm-hook-001",
            connection_target="probe-sm-hook-001",
            name="Probe SM Hook Device",
            os_version="14",
            host_id=db_host.id,
            operational_state=DeviceOperationalState.available,
            verified_at=datetime.now(UTC),
            device_type=DeviceType.real_device,
            connection_type=ConnectionType.usb,
        )
    db_session.add(device)
    await db_session.flush()

    with state_write_guard.bypass():
        node = AppiumNode(
            device_id=device.id,
            port=4751,
            grid_url="http://hub:4444",
            desired_state=AppiumDesiredState.running,
            desired_port=4751,
            pid=5000,
            active_connection_target="probe-sm-hook-001",
        )
    db_session.add(node)
    await db_session.commit()

    loaded_device = await db_session.get(Device, device.id)
    assert loaded_device is not None
    loaded_node = await db_session.get(AppiumNode, node.id)
    assert loaded_node is not None
    loaded_device.appium_node = loaded_node

    svc = SessionViabilityService(
        publisher=Mock(),
        settings=FakeSettingsReader({}),
        session_factory=AsyncMock(),
        capability=DeviceCapabilityService(),
        health=AsyncMock(),
    )
    with (
        patch(
            "app.devices.services.capability.DeviceCapabilityService.get_device_capabilities",
            new_callable=AsyncMock,
            return_value={"platformName": "Android"},
        ),
        patch.object(
            svc,
            "probe_session_via_grid",
            new_callable=AsyncMock,
            return_value=(True, None),
        ),
    ):
        result = await svc.run_session_viability_probe(
            db_session,
            loaded_device,
            checked_by=session_viability.SessionViabilityCheckedBy.manual,
        )

    assert result["status"] == "passed"

    # The distinguishing artifact: EventLogHook writes a DeviceEvent ORM row
    # only when the transition goes through DeviceStateMachine. A direct
    # set_operational_state call emits a bus event but does NOT add this row.
    rows = (
        (
            await db_session.execute(
                select(DeviceEvent).where(
                    DeviceEvent.device_id == loaded_device.id,
                    DeviceEvent.event_type == DeviceEventType.session_started,
                )
            )
        )
        .scalars()
        .all()
    )

    assert len(rows) >= 1, (
        "Expected at least one DeviceEvent row with event_type=session_started; "
        "got none. This means the busy-mark did not go through DeviceStateMachine."
    )

    # The first session_started row must record the available→busy transition.
    busy_mark_row = rows[0]
    assert busy_mark_row.details is not None
    assert busy_mark_row.details.get("from") == "available/None", (
        f"Expected 'from' to be 'available/None', got {busy_mark_row.details.get('from')!r}"
    )
    assert busy_mark_row.details.get("to") == "busy/None", (
        f"Expected 'to' to be 'busy/None', got {busy_mark_row.details.get('to')!r}"
    )
