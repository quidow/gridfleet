from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from app.models.appium_node import AppiumDesiredState
from app.models.device_intent import DeviceIntent
from app.services.intent_evaluator import (
    evaluate_grid_routing,
    evaluate_node_process,
    evaluate_recovery,
    evaluate_reservation,
    map_node_process_decision,
)
from app.services.intent_types import GRID_ROUTING, NODE_PROCESS, RECOVERY, RESERVATION

if TYPE_CHECKING:
    import pytest


def _intent(
    *,
    source: str,
    axis: str,
    payload: dict[str, Any],
    run_id: uuid.UUID | None = None,
    expires_at: datetime | None = None,
) -> DeviceIntent:
    return DeviceIntent(
        device_id=uuid.uuid4(),
        source=source,
        axis=axis,
        run_id=run_id,
        payload=payload,
        expires_at=expires_at,
    )


def test_empty_node_process_intents_return_stopped() -> None:
    decision = evaluate_node_process([], datetime.now(UTC))

    assert decision.desired_state == "stopped"
    assert decision.stop_mode is None
    assert decision.reason == "no active node_process intent"


def test_idle_start_returns_running() -> None:
    decision = evaluate_node_process(
        [
            _intent(
                source="baseline:idle",
                axis=NODE_PROCESS,
                payload={"action": "start", "priority": 10, "desired_port": 4723},
            )
        ],
        datetime.now(UTC),
    )

    assert decision.desired_state == "running"
    assert decision.desired_port == 4723
    assert map_node_process_decision(decision) == (AppiumDesiredState.running, True, False)


def test_cooldown_defer_stop_returns_running_blocked() -> None:
    decision = evaluate_node_process(
        [
            _intent(
                source="cooldown:node:run",
                axis=NODE_PROCESS,
                payload={"action": "stop", "priority": 70, "stop_mode": "defer"},
            )
        ],
        datetime.now(UTC),
    )

    assert decision.desired_state == "running_blocked"
    assert decision.stop_mode == "defer"
    assert map_node_process_decision(decision) == (AppiumDesiredState.running, False, True)


def test_maintenance_graceful_stop_returns_stopping_graceful() -> None:
    decision = evaluate_node_process(
        [
            _intent(
                source="maintenance:device",
                axis=NODE_PROCESS,
                payload={"action": "stop", "priority": 80, "stop_mode": "graceful"},
            )
        ],
        datetime.now(UTC),
    )

    assert decision.desired_state == "stopping_graceful"
    assert decision.stop_mode == "graceful"
    assert map_node_process_decision(decision) == (AppiumDesiredState.stopped, False, True)


def test_forced_release_hard_stop_beats_cooldown_and_active_session() -> None:
    decision = evaluate_node_process(
        [
            _intent(
                source="active_session:session",
                axis=NODE_PROCESS,
                payload={"action": "start", "priority": 30},
            ),
            _intent(
                source="cooldown:node:run",
                axis=NODE_PROCESS,
                payload={"action": "stop", "priority": 70, "stop_mode": "defer"},
            ),
            _intent(
                source="forced_release:run",
                axis=NODE_PROCESS,
                payload={"action": "stop", "priority": 95, "stop_mode": "hard"},
            ),
        ],
        datetime.now(UTC),
    )

    assert decision.desired_state == "stopped"
    assert decision.stop_mode == "hard"


def test_equal_start_stop_priority_returns_stopped_and_logs_conflict(caplog: pytest.LogCaptureFixture) -> None:
    now = datetime.now(UTC)
    caplog.set_level(logging.WARNING)

    decision = evaluate_node_process(
        [
            _intent(source="start", axis=NODE_PROCESS, payload={"action": "start", "priority": 50}),
            _intent(source="stop", axis=NODE_PROCESS, payload={"action": "stop", "priority": 50}),
        ],
        now,
    )

    assert decision.desired_state == "stopped"
    assert "device_intent_same_priority_conflict" in caplog.text


def test_grid_routing_uses_intent_run_id_column() -> None:
    run_id = uuid.uuid4()
    wrong_payload_run_id = uuid.uuid4()
    decision = evaluate_grid_routing(
        [
            _intent(
                source="run:target",
                axis=GRID_ROUTING,
                run_id=run_id,
                payload={
                    "accepting_new_sessions": True,
                    "priority": 40,
                    "run_id": str(wrong_payload_run_id),
                },
            )
        ],
        datetime.now(UTC),
    )

    assert decision.run_id == run_id
    assert decision.accepting_new_sessions is True


def test_reservation_returns_highest_run_id_excluded_until_and_cooldown_count() -> None:
    now = datetime.now(UTC)
    run_id = uuid.uuid4()
    expires_at = now + timedelta(minutes=5)

    decision = evaluate_reservation(
        [
            _intent(
                source="cooldown:reservation:run",
                axis=RESERVATION,
                run_id=run_id,
                expires_at=expires_at,
                payload={
                    "excluded": True,
                    "priority": 70,
                    "exclusion_reason": "Device in cooldown",
                    "cooldown_count": 3,
                },
            )
        ],
        now,
    )

    assert decision.excluded is True
    assert decision.run_id == run_id
    assert decision.expires_at == expires_at
    assert decision.exclusion_reason == "Device in cooldown"
    assert decision.cooldown_count == 3


def test_recovery_uses_highest_priority_intent() -> None:
    decision = evaluate_recovery(
        [
            _intent(
                source="operator:allow",
                axis=RECOVERY,
                payload={"allowed": True, "priority": 100, "reason": "operator override"},
            ),
            _intent(
                source="maintenance:device",
                axis=RECOVERY,
                payload={"allowed": False, "priority": 80, "reason": "maintenance"},
            ),
        ],
        datetime.now(UTC),
    )

    assert decision.allowed is True
    assert decision.reason == "operator override"
    assert decision.source == "operator:allow"


def test_recovery_same_priority_deny_wins() -> None:
    decision = evaluate_recovery(
        [
            _intent(
                source="operator:allow",
                axis=RECOVERY,
                payload={"allowed": True, "priority": 80, "reason": "operator override"},
            ),
            _intent(
                source="maintenance:device",
                axis=RECOVERY,
                payload={"allowed": False, "priority": 80, "reason": "maintenance"},
            ),
        ],
        datetime.now(UTC),
    )

    assert decision.allowed is False
    assert decision.reason == "maintenance"
    assert decision.source == "maintenance:device"


def test_expired_intents_are_ignored() -> None:
    now = datetime.now(UTC)
    decision = evaluate_grid_routing(
        [
            _intent(
                source="run:expired",
                axis=GRID_ROUTING,
                run_id=uuid.uuid4(),
                expires_at=now - timedelta(seconds=1),
                payload={"accepting_new_sessions": False, "priority": 100},
            )
        ],
        now,
    )

    assert decision.run_id is None
    assert decision.accepting_new_sessions is True
