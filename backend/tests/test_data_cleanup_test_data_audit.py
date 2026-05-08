from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from app.models import DeviceTestDataAuditLog
from app.services import data_cleanup
from app.services.settings_service import settings_service
from tests.helpers import create_device_record

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.host import Host

pytestmark = pytest.mark.db


async def test_cleanup_deletes_old_test_data_audit_rows(db_session: AsyncSession, db_host: Host) -> None:
    device = await create_device_record(
        db_session,
        host_id=db_host.id,
        identity_value="udid-cleanup-1",
        name="dev-cleanup-1",
    )
    settings_service._cache["retention.audit_log_days"] = 7

    old = DeviceTestDataAuditLog(
        device_id=device.id,
        previous_test_data={},
        new_test_data={"v": 1},
        changed_by="op",
        changed_at=datetime.now(UTC) - timedelta(days=30),
    )
    db_session.add(old)
    await db_session.commit()

    await data_cleanup._cleanup_old_data(db_session)

    refreshed = await db_session.get(DeviceTestDataAuditLog, old.id)
    assert refreshed is None
