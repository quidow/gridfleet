from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from app.devices.services.lifecycle_policy_state import (
    clear_recovery_generation,
    in_maintenance,
    recovery_generation,
    set_maintenance_reason,
    set_recovery_generation,
)
from tests.helpers import seed_host_and_device

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


async def test_recovery_generation_round_trip_preserves_maintenance(
    db_session: AsyncSession,
) -> None:
    _host, device = await seed_host_and_device(db_session, identity="recovery-gen-rt")
    await db_session.commit()
    generation = uuid.uuid4()
    set_maintenance_reason(device, "operator")
    set_recovery_generation(device, generation)
    assert recovery_generation(device) == generation
    assert in_maintenance(device) is True
    assert clear_recovery_generation(device, expected=uuid.uuid4()) is False
    assert recovery_generation(device) == generation
    assert in_maintenance(device) is True
    assert clear_recovery_generation(device, expected=generation) is True
    assert in_maintenance(device) is True
