from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from sqlalchemy.exc import IntegrityError

from app.devices.models import DeviceRemediationLogEntry
from tests.helpers import create_device_record

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.hosts.models import Host


pytestmark = pytest.mark.db


async def test_remediation_log_entry_has_uuidv7_default_and_kind_constraint(
    db_session: AsyncSession, db_host: Host
) -> None:
    device = await create_device_record(
        db_session,
        host_id=db_host.id,
        identity_value="remediation-log-model-device",
        name="remediation-log-model-device",
    )
    entry = DeviceRemediationLogEntry(
        device_id=device.id,
        kind="attempt",
        source="node_health",
        action="recovery_failed",
        at=datetime.now(UTC),
    )
    db_session.add(entry)
    await db_session.flush()
    assert entry.id is not None

    invalid = DeviceRemediationLogEntry(
        device_id=device.id,
        kind="bogus",
        source="node_health",
        action="recovery_failed",
        at=datetime.now(UTC),
    )
    db_session.add(invalid)
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()
