"""Lock the reason text of the maintenance recovery block.

Regression guard for a string-drift bug: the recovery availability projection
reports a maintenance-held device as blocked with the exact
``MAINTENANCE_HOLD_SUPPRESSION_REASON`` constant. Any divergence between the
decider's reason and the constant would light up unrelated UI panels.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from app.appium_nodes.models import AppiumDesiredState, AppiumNode
from app.devices import locking as device_locking
from app.devices.services.lifecycle_policy_state import MAINTENANCE_HOLD_SUPPRESSION_REASON, set_maintenance_reason
from app.devices.services.recovery_projection import RecoveryBlockKind, recovery_availability
from tests.helpers import create_device

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.hosts.models import Host


@pytest.mark.db
async def test_maintenance_recovery_uses_clear_constant(db_session: AsyncSession, db_host: Host) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="maint-reason")
    node = AppiumNode(device_id=device.id, port=4723, desired_state=AppiumDesiredState.stopped)
    db_session.add(node)
    await db_session.commit()

    locked = await device_locking.lock_device(db_session, device.id)
    set_maintenance_reason(locked, "Operator entered maintenance")
    await db_session.commit()

    availability = await recovery_availability(db_session, locked)
    assert availability.allowed is False
    assert availability.kind is RecoveryBlockKind.maintenance
    assert availability.reason == MAINTENANCE_HOLD_SUPPRESSION_REASON
