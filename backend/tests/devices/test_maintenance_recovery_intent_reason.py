"""Lock the reason text of the maintenance recovery intent.

Regression guard for a string-drift bug: the recovery intent payload's
``reason`` is propagated end-to-end and ends up persisted in
``lifecycle_policy_state.recovery_suppressed_reason``. The exit-maintenance
cleanup helper (``clear_maintenance_recovery_suppression``) only clears that
field when the value equals ``MAINTENANCE_HOLD_SUPPRESSION_REASON``. Any
divergence between the intent payload and the constant freezes the device's
node ``effective_state`` at ``"blocked"`` after an operator exit and lights
up unrelated UI panels.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from app.core.timeutil import now_utc
from app.devices.services import state_write_guard
from app.devices.services.intent_synthesis import synthesize_fact_intents
from app.devices.services.intent_types import RECOVERY
from app.devices.services.lifecycle_policy_state import MAINTENANCE_HOLD_SUPPRESSION_REASON, set_maintenance_reason
from tests.helpers import create_device

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.hosts.models import Host


@pytest.mark.db
async def test_maintenance_recovery_intent_uses_clear_constant(db_session: AsyncSession, db_host: Host) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="maint-reason")
    with state_write_guard.bypass():
        set_maintenance_reason(device, "Operator entered maintenance")
    await db_session.flush()
    intents = await synthesize_fact_intents(db_session, device, None, [], now_utc())
    recovery = next(intent for intent in intents if intent.axis == RECOVERY)
    assert recovery.payload["reason"] == MAINTENANCE_HOLD_SUPPRESSION_REASON
