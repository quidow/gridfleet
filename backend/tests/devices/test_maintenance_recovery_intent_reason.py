"""Lock the reason text of the maintenance recovery suppression.

Regression guard for a string-drift bug: the recovery deny reason is propagated
end-to-end and ends up persisted in
``lifecycle_policy_state.recovery_suppressed_reason`` (surfaced as
``Device.recovery_blocked_reason``). The exit-maintenance cleanup helper
(``clear_maintenance_recovery_suppression``) only clears that field when the
value equals ``MAINTENANCE_HOLD_SUPPRESSION_REASON``. Any divergence between the
decider's reason and the constant freezes the device's node ``effective_state``
at ``"blocked"`` after an operator exit and lights up unrelated UI panels.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from sqlalchemy import select

from app.appium_nodes.models import AppiumDesiredState, AppiumNode
from app.devices import locking as device_locking
from app.devices.models import Device
from app.devices.services.intent_reconciler import reconcile_device
from app.devices.services.lifecycle_policy_state import MAINTENANCE_HOLD_SUPPRESSION_REASON, set_maintenance_reason
from tests.helpers import create_device
from tests.helpers import test_event_bus as event_bus

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

    await reconcile_device(db_session, device.id, publisher=event_bus)
    await db_session.commit()

    device = (await db_session.execute(select(Device).where(Device.id == device.id))).scalar_one()
    assert device.recovery_allowed is False
    assert device.recovery_blocked_reason == MAINTENANCE_HOLD_SUPPRESSION_REASON
