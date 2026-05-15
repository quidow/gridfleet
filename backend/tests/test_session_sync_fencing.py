"""session_sync must not create or end Session rows after losing leadership."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import func, select

from app.core.leader.advisory import LeadershipLost
from app.devices.models import ConnectionType, Device, DeviceOperationalState, DeviceType
from app.hosts.models import Host, HostStatus, OSType
from app.sessions.models import Session, SessionStatus
from app.sessions.service_sync import _sync_sessions

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


@pytest.mark.db
@pytest.mark.asyncio
async def test_sync_sessions_aborts_after_grid_call_when_leadership_lost(
    db_session: AsyncSession,
) -> None:
    fake_grid = {"value": {"ready": True, "nodes": []}}
    initial_count = (await db_session.execute(select(func.count()).select_from(Session))).scalar_one()

    with (
        patch(
            "app.sessions.service_sync.grid_service.get_grid_status",
            new_callable=AsyncMock,
            return_value=fake_grid,
        ),
        patch(
            "app.sessions.service_sync.assert_current_leader",
            side_effect=LeadershipLost("test"),
        ),
        pytest.raises(LeadershipLost),
    ):
        await _sync_sessions(db_session)

    final_count = (await db_session.execute(select(func.count()).select_from(Session))).scalar_one()
    assert final_count == initial_count


@pytest.mark.db
@pytest.mark.asyncio
async def test_sync_sessions_does_not_end_running_session_when_leadership_lost(
    db_session: AsyncSession,
) -> None:
    host = Host(
        id=uuid.uuid4(),
        hostname="sync-fence-h",
        ip="10.0.0.99",
        agent_port=5100,
        status=HostStatus.online,
        os_type=OSType.linux,
    )
    db_session.add(host)
    await db_session.flush()
    device = Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value="sync-fence-001",
        connection_target="sync-fence-001",
        name="Sync Fence Device",
        os_version="14",
        host_id=host.id,
        operational_state=DeviceOperationalState.busy,
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )
    db_session.add(device)
    await db_session.flush()
    session = Session(
        session_id="run-sess-fence",
        device_id=device.id,
        status=SessionStatus.running,
    )
    db_session.add(session)
    await db_session.commit()

    fake_grid = {"value": {"ready": True, "nodes": []}}

    with (
        patch(
            "app.sessions.service_sync.grid_service.get_grid_status",
            new_callable=AsyncMock,
            return_value=fake_grid,
        ),
        patch(
            "app.sessions.service_sync.assert_current_leader",
            side_effect=LeadershipLost("test"),
        ),
        pytest.raises(LeadershipLost),
    ):
        await _sync_sessions(db_session)

    await db_session.refresh(session, attribute_names=["status", "ended_at"])
    assert session.status == SessionStatus.running
    assert session.ended_at is None
