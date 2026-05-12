from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from datetime import datetime
    from uuid import UUID

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
PRIORITY_ACTIVE_SESSION = 30
PRIORITY_AUTO_RECOVERY = 20
PRIORITY_IDLE = 10


@dataclass(frozen=True)
class IntentRegistration:
    source: str
    axis: IntentAxis
    payload: dict[str, Any]
    run_id: UUID | None = None
    expires_at: datetime | None = None
