from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from app.devices.models import DeviceTestDataAuditLog
from tests.helpers import create_device_record

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.hosts.models import Host


pytestmark = pytest.mark.db


async def test_device_test_data_audit_log_persists(db_session: AsyncSession, db_host: Host) -> None:
    device = await create_device_record(
        db_session,
        host_id=db_host.id,
        identity_value="udid-test-1",
        name="dev-1",
    )
    await db_session.flush()

    entry = DeviceTestDataAuditLog(
        device_id=device.id,
        previous_test_data={"old": True},
        new_test_data={"new": True},
        changed_by="op@example.com",
    )
    db_session.add(entry)
    await db_session.commit()

    fetched = await db_session.get(DeviceTestDataAuditLog, entry.id)
    assert fetched is not None
    assert fetched.new_test_data == {"new": True}
