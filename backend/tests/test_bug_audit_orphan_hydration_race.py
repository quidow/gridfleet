"""Bug 2: Orphan session hydration writes device_id to a Session ended concurrently.

See ``docs/superpowers/specs/2026-05-20-backend-bug-audit.md#bug-2``.

``_hydrate_orphan_session_row`` reads the Session row at
``service_sync.py:185-191`` without ``with_for_update``. Between that
unlocked read and ``session.device_id = locked_device.id`` at line
203, a concurrent end-of-session can flip ``status=passed`` /
``ended_at=NOW``. Hydration still binds the device and fires
``SESSION_STARTED``, leaving the device ``busy`` for a dead session.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

from app.devices.models import Device, DeviceOperationalState
from app.sessions.models import Session, SessionStatus
from app.sessions.service_sync import SessionSyncService
from tests.fakes import FakeSettingsReader, make_fake_grid
from tests.helpers import create_device, create_host
from tests.helpers import test_event_bus as event_bus

if TYPE_CHECKING:
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@pytest.mark.db
@pytest.mark.asyncio
async def test_orphan_hydration_writes_device_to_ended_session(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    client: AsyncClient,
) -> None:
    host = await create_host(client)
    device = await create_device(
        db_session,
        host_id=uuid.UUID(host["id"]),
        name="orphan-target",
        operational_state=DeviceOperationalState.available,
        verified=True,
    )

    sid = f"orphan-{uuid.uuid4().hex[:8]}"
    orphan = Session(
        session_id=sid,
        device_id=None,
        status=SessionStatus.running,
        ended_at=None,
    )
    db_session.add(orphan)
    await db_session.commit()
    orphan_id = orphan.id
    device_id = device.id

    # Simulate the race by committing the end-of-session on a side-channel
    # *before* hydration runs its Session SELECT. A fixed hydration that
    # takes ``SELECT … FOR UPDATE`` on the Session row will see the row as
    # ``status=passed, ended_at IS NOT NULL`` and the FOR UPDATE predicate
    # ``ended_at IS NULL`` will exclude it — hydration bails without
    # binding the device. The previous (buggy) hydration read the row
    # without a lock and would still bind the device.
    async with db_session_maker() as side:
        row = await side.get(Session, orphan_id)
        assert row is not None
        row.status = SessionStatus.passed
        row.ended_at = datetime.now(UTC)
        await side.commit()

    info = {"device_id": str(device_id), "connection_target": device.connection_target}
    svc = SessionSyncService(
        publisher=event_bus, settings=FakeSettingsReader({}), grid=make_fake_grid(), lifecycle=MagicMock()
    )
    await svc._hydrate_orphan_session_row(db_session, sid, info)

    await db_session.commit()

    # Re-read the device on a fresh session.
    async with db_session_maker() as side:
        refreshed = await side.get(Device, device_id)
        assert refreshed is not None
        # Fixed behavior: hydration sees the ended session and skips the
        # SESSION_STARTED transition.
        # Current behavior (bug): device is flipped to busy for a session
        # that already ended.
        assert refreshed.operational_state != DeviceOperationalState.busy, (
            "Device transitioned to busy for a Session row that ended during hydration"
        )
