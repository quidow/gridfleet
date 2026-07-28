"""Pure function tests for decision snapshot building blocks.

Split out of ``test_decision_snapshot.py``: that module's ``pytestmark`` puts
every test behind Postgres and the ``seeded_driver_packs`` fixture. These tests
need neither — they build an ``IntentSnapshot`` by hand and assert on pure
functions, or compile SQLAlchemy statements and check their generated SQL without
execution.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy.dialects import postgresql

from app.devices.services.decision import parse_command
from app.devices.services.decision_snapshot import IntentSnapshot, _ladder_entries_stmt
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


def test_the_ladder_statement_locates_the_reset_once() -> None:
    """One reset lookup per statement, not two structurally identical ones."""
    compiled = str(_ladder_entries_stmt(uuid.uuid4()).compile(dialect=postgresql.dialect()))
    assert compiled.count("LIMIT") == 1, f"the reset is located more than once:\n{compiled}"
