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

import uuid

from app.devices.services.intent_types import RECOVERY
from app.devices.services.lifecycle_policy_state import MAINTENANCE_HOLD_SUPPRESSION_REASON
from app.devices.services.maintenance import _maintenance_intents


def test_maintenance_recovery_intent_uses_clear_constant() -> None:
    intents = _maintenance_intents(uuid.uuid4())
    recovery = next(intent for intent in intents if intent.axis == RECOVERY)
    assert recovery.payload["reason"] == MAINTENANCE_HOLD_SUPPRESSION_REASON
