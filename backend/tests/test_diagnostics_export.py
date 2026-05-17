"""Backend tests for the device diagnostic export feature."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.devices.models import DeviceDiagnosticSnapshot
from app.hosts.models import Host
from tests.helpers import create_device

pytestmark = pytest.mark.usefixtures("seeded_driver_packs")


@pytest.mark.db
async def test_diagnostic_snapshot_persists_with_payload(
    db_session: AsyncSession,
    db_host: Host,
    seeded_driver_packs: None,
) -> None:
    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="snapshot-model-device",
        identity_value="model-test",
    )
    row = DeviceDiagnosticSnapshot(
        device_id=device.id,
        trigger="operator",
        reason="manual",
        payload={"schema_version": 1, "device": {"id": str(device.id)}},
    )
    db_session.add(row)
    await db_session.commit()

    result = await db_session.execute(
        select(DeviceDiagnosticSnapshot).where(DeviceDiagnosticSnapshot.device_id == device.id)
    )
    persisted = result.scalar_one()
    assert persisted.trigger == "operator"
    assert persisted.reason == "manual"
    assert persisted.payload["schema_version"] == 1
    assert persisted.captured_at is not None
    assert isinstance(persisted.id, uuid.UUID)
    assert isinstance(persisted.captured_at, datetime)
    assert persisted.captured_at.tzinfo is not None
