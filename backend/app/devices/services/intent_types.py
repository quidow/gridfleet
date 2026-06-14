from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, TypedDict

if TYPE_CHECKING:
    from datetime import datetime
    from uuid import UUID


def verification_intent_source(device_id: UUID) -> str:
    """Return the ``source`` key used for verification intents on *device_id*."""
    return f"verification:{device_id}"


def failure_stop_sources(device_id: UUID) -> list[str]:
    """Failure-driven node stop sources for *device_id*.

    These carry ``PRIORITY_HEALTH_FAILURE``/``PRIORITY_CONNECTIVITY_LOST`` (60/50),
    which outrank the ``PRIORITY_AUTO_RECOVERY`` (20) start intents used by operator
    start-node and verification. Both explicit re-qualification paths revoke these
    before starting a node so a leftover stop cannot silently block the start.
    """
    return [
        f"health_failure:node:{device_id}",
        f"health_failure:recovery:{device_id}",
        f"connectivity:{device_id}",
    ]


IntentAxis = Literal["node_process", "grid_routing", "reservation", "recovery"]

NODE_PROCESS: IntentAxis = "node_process"
GRID_ROUTING: IntentAxis = "grid_routing"
RESERVATION: IntentAxis = "reservation"
RECOVERY: IntentAxis = "recovery"

PRIORITY_OPERATOR_STOP = 100
PRIORITY_FORCED_RELEASE = 95
PRIORITY_DEVICE_DELETE = 90
PRIORITY_MAINTENANCE = 80
PRIORITY_COOLDOWN = 70
PRIORITY_HEALTH_FAILURE = 60
PRIORITY_CONNECTIVITY_LOST = 50
PRIORITY_RUN_ROUTING = 40
PRIORITY_AUTO_RECOVERY = 20
PRIORITY_IDLE = 10


class RunActivePrecondition(TypedDict):
    kind: Literal["run_active"]
    run_id: str


class ReservationActivePrecondition(TypedDict):
    kind: Literal["reservation_active"]
    run_id: str
    device_id: str


class NodeRunningPrecondition(TypedDict):
    kind: Literal["node_running"]
    device_id: str
    expected: bool


class MaintenanceActivePrecondition(TypedDict):
    kind: Literal["maintenance_active"]
    device_id: str


Precondition = (
    RunActivePrecondition | ReservationActivePrecondition | NodeRunningPrecondition | MaintenanceActivePrecondition
)


@dataclass(frozen=True)
class IntentRegistration:
    source: str
    axis: IntentAxis
    payload: dict[str, Any]
    run_id: UUID | None = None
    expires_at: datetime | None = None
    precondition: Precondition | None = None
