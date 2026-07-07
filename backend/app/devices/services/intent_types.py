from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from datetime import datetime
    from uuid import UUID


def verification_intent_source(device_id: UUID) -> str:
    """Return the ``source`` key used for verification intents on *device_id*."""
    return f"verification:{device_id}"


def failure_stop_sources(device_id: UUID) -> list[str]:
    """Failure-driven node stop sources for *device_id*.

    These carry ``PRIORITY_HEALTH_FAILURE`` (60), which outranks the
    ``PRIORITY_AUTO_RECOVERY`` (20) start intents used by operator start-node and
    verification. Both explicit re-qualification paths revoke these before starting a
    node so a leftover stop cannot silently block the start. (``connectivity:`` is no
    longer here — it is synthesized from ``device_checks_healthy`` and suppressed by an
    active start command, so there is nothing stored to revoke.)
    """
    return [
        f"health_failure:node:{device_id}",
        f"health_failure:recovery:{device_id}",
    ]


IntentAxis = Literal["node_process", "grid_routing", "recovery"]

NODE_PROCESS: IntentAxis = "node_process"
GRID_ROUTING: IntentAxis = "grid_routing"
RECOVERY: IntentAxis = "recovery"

PRIORITY_OPERATOR_STOP = 100
PRIORITY_FORCED_RELEASE = 95
PRIORITY_MAINTENANCE = 80
PRIORITY_COOLDOWN = 70
PRIORITY_HEALTH_FAILURE = 60
PRIORITY_CONNECTIVITY_LOST = 50
PRIORITY_RUN_ROUTING = 40
PRIORITY_AUTO_RECOVERY = 20
PRIORITY_IDLE = 10


@dataclass(frozen=True)
class IntentRegistration:
    source: str
    axis: IntentAxis
    payload: dict[str, Any]
    run_id: UUID | None = None
    expires_at: datetime | None = None
