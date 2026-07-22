from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from app.devices.services.decision import parse_command
from app.devices.services.decision_snapshot import IntentSnapshot
from app.devices.services.intent_types import CommandKind


def test_parse_command_accepts_immutable_intent_snapshot() -> None:
    now = datetime.now(UTC)
    intent = IntentSnapshot(
        id=uuid.uuid4(),
        device_id=uuid.uuid4(),
        source="operator:start:test",
        kind=CommandKind.operator_start.value,
        run_id=None,
        payload={"restart_requested_at": now.isoformat(), "reason": "operator"},
        expires_at=now + timedelta(minutes=1),
    )

    command = parse_command(intent, now)

    assert command is not None
    assert command.kind is CommandKind.operator_start
    assert command.source == intent.source
    assert command.restart_requested_at == now
    assert command.reason_detail == "operator"
