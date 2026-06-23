from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

from app.devices.models import DeviceTestDataAuditLog
from app.devices.services import data_cleanup
from tests.fakes import FakeSettingsReader
from tests.helpers import create_device_record

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.hosts.models import Host

pytestmark = pytest.mark.db


async def test_cleanup_deletes_old_test_data_audit_rows(db_session: AsyncSession, db_host: Host) -> None:
    device = await create_device_record(
        db_session,
        host_id=db_host.id,
        identity_value="udid-cleanup-1",
        name="dev-cleanup-1",
    )
    old = DeviceTestDataAuditLog(
        device_id=device.id,
        previous_test_data={},
        new_test_data={"v": 1},
        changed_by="op",
        changed_at=datetime.now(UTC) - timedelta(days=30),
    )
    db_session.add(old)
    await db_session.commit()

    await data_cleanup.DataCleanupService(
        publisher=AsyncMock(), settings=FakeSettingsReader({"retention.audit_log_days": 7})
    ).cleanup_old_data(db_session)

    refreshed = await db_session.get(DeviceTestDataAuditLog, old.id)
    assert refreshed is None
