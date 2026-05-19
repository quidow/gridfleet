"""High-level operator-driven Appium node lifecycle helpers.

All operator-initiated node lifecycle writes (start / stop / restart) must flow
through this module so that the ``device_intents`` table is the single source of
truth for desired ``appium_nodes`` state. Direct ``write_desired_state`` calls
from operator code are forbidden — they leave stale intent payloads that the
intent reconciler keeps re-asserting onto the AppiumNode row.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from app.devices.services.intent_types import (
    GRID_ROUTING,
    NODE_PROCESS,
    PRIORITY_AUTO_RECOVERY,
    PRIORITY_OPERATOR_STOP,
    IntentRegistration,
    NodeRunningPrecondition,
)
from app.settings import settings_service

if TYPE_CHECKING:
    from app.devices.models import Device


def operator_start_source(device_id: uuid.UUID) -> str:
    return f"operator:start:{device_id}"


def operator_stop_sources(device_id: uuid.UUID) -> list[str]:
    return [f"operator:stop:node:{device_id}", f"operator:stop:grid:{device_id}"]


def operator_start_precondition(device_id: uuid.UUID) -> NodeRunningPrecondition:
    """Precondition retiring an operator:start intent once the node is observed running.

    ``expected: False`` means "satisfied while the node is NOT running". The
    intent represents an operator's desire to start the node, so once the node
    reaches ``observed_running == True`` the precondition flips and the
    reconciler sweep deletes the row.
    """
    return {
        "kind": "node_running",
        "device_id": str(device_id),
        "expected": False,
    }


def operator_start_intent(device: Device, desired_port: int) -> IntentRegistration:
    return IntentRegistration(
        source=operator_start_source(device.id),
        axis=NODE_PROCESS,
        payload={"action": "start", "priority": PRIORITY_AUTO_RECOVERY, "desired_port": desired_port},
        precondition=operator_start_precondition(device.id),
    )


def operator_restart_intent(device: Device, desired_port: int) -> IntentRegistration:
    window_sec = int(settings_service.get("appium_reconciler.restart_window_sec"))
    deadline = datetime.now(UTC) + timedelta(seconds=window_sec)
    return IntentRegistration(
        source=operator_start_source(device.id),
        axis=NODE_PROCESS,
        payload={
            "action": "start",
            "priority": PRIORITY_AUTO_RECOVERY,
            "desired_port": desired_port,
            "transition_token": str(uuid.uuid4()),
            "transition_deadline": deadline.isoformat(),
        },
        precondition=operator_start_precondition(device.id),
        expires_at=deadline,
    )


def operator_stop_intents(device_id: uuid.UUID) -> list[IntentRegistration]:
    return [
        IntentRegistration(
            source=f"operator:stop:node:{device_id}",
            axis=NODE_PROCESS,
            payload={"action": "stop", "priority": PRIORITY_OPERATOR_STOP, "stop_mode": "hard"},
        ),
        IntentRegistration(
            source=f"operator:stop:grid:{device_id}",
            axis=GRID_ROUTING,
            payload={"accepting_new_sessions": False, "priority": PRIORITY_OPERATOR_STOP},
        ),
    ]
