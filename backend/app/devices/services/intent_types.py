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

    The ``health_failure:node`` stop command outranks the node-start commands
    (operator start-node, verification) in the decision ladder. Both explicit
    re-qualification paths revoke it before starting a node so a leftover stop
    cannot silently block the start. (``connectivity:`` is not here — it is a fact
    read from ``device_checks_healthy`` and suppressed by an active start command,
    so there is nothing stored to revoke.)
    """
    return [
        f"health_failure:node:{device_id}",
    ]


IntentAxis = Literal["node_process", "grid_routing", "recovery"]

NODE_PROCESS: IntentAxis = "node_process"
GRID_ROUTING: IntentAxis = "grid_routing"
RECOVERY: IntentAxis = "recovery"


@dataclass(frozen=True)
class IntentRegistration:
    source: str
    axis: IntentAxis
    payload: dict[str, Any]
    run_id: UUID | None = None
    expires_at: datetime | None = None
