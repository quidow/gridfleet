from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from sqlalchemy import select

from app.devices.models import DeviceIntent
from app.devices.services.intent_types import NODE_PROCESS
from tests.helpers import create_device

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.hosts.models import Host


@pytest.mark.db
async def test_device_intent_persists_precondition(db_session: AsyncSession, db_host: Host) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="prec-persist")
    intent = DeviceIntent(
        device_id=device.id,
        source="cooldown:node:test",
        axis=NODE_PROCESS,
        payload={"action": "stop"},
        precondition={"kind": "run_active", "run_id": "00000000-0000-0000-0000-000000000000"},
    )
    db_session.add(intent)
    await db_session.commit()

    fetched = (await db_session.execute(select(DeviceIntent).where(DeviceIntent.device_id == device.id))).scalar_one()
    assert fetched.precondition == {
        "kind": "run_active",
        "run_id": "00000000-0000-0000-0000-000000000000",
    }


@pytest.mark.db
async def test_device_intent_precondition_defaults_to_none(db_session: AsyncSession, db_host: Host) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="prec-default")
    intent = DeviceIntent(
        device_id=device.id,
        source="baseline",
        axis=NODE_PROCESS,
        payload={"action": "start"},
    )
    db_session.add(intent)
    await db_session.commit()

    fetched = (await db_session.execute(select(DeviceIntent).where(DeviceIntent.device_id == device.id))).scalar_one()
    assert fetched.precondition is None
