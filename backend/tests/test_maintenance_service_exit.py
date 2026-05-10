"""D3: exit_maintenance must enqueue a recovery job."""

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.device import DeviceHold, DeviceOperationalState
from app.models.host import Host
from app.models.job import Job
from app.services import device_locking, maintenance_service
from app.services.job_kind_constants import JOB_KIND_DEVICE_RECOVERY
from tests.helpers import create_device

pytestmark = pytest.mark.asyncio


async def test_exit_maintenance_enqueues_recovery_job(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="exit-enqueues-job",
        hold=DeviceHold.maintenance,
        operational_state=DeviceOperationalState.offline,
    )

    locked = await device_locking.lock_device(db_session, device.id)
    await maintenance_service.exit_maintenance(db_session, locked)

    rows = (await db_session.execute(select(Job).where(Job.kind == JOB_KIND_DEVICE_RECOVERY))).scalars().all()
    assert len(rows) == 1
    payload = rows[0].payload
    assert payload["device_id"] == str(device.id)
    assert payload["source"] == "exit_maintenance"
