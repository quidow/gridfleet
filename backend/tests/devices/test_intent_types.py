from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from app.devices.services.intent_types import (
    NODE_PROCESS,
    IntentRegistration,
)


def test_intent_registration_carries_optional_precondition() -> None:
    run_id = uuid4()
    reg = IntentRegistration(
        source=f"cooldown:node:{run_id}",
        axis=NODE_PROCESS,
        payload={"action": "stop"},
        run_id=run_id,
        expires_at=datetime.now(UTC),
        precondition={"kind": "run_active", "run_id": str(run_id)},
    )
    assert reg.precondition == {"kind": "run_active", "run_id": str(run_id)}


def test_intent_registration_precondition_defaults_to_none() -> None:
    reg = IntentRegistration(source="baseline:idle", axis=NODE_PROCESS, payload={})
    assert reg.precondition is None
