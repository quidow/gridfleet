from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import uuid
    from datetime import datetime

    from app.devices.services.decision import DecisionFacts
    from app.devices.services.state import DeviceStateFacts
    from app.lifecycle.services.remediation_log import LadderState


@dataclass(frozen=True, slots=True)
class IntentSnapshot:
    id: uuid.UUID
    device_id: uuid.UUID
    source: str
    kind: str
    run_id: uuid.UUID | None
    payload: dict[str, Any]
    expires_at: datetime | None


@dataclass(frozen=True, slots=True)
class ReservationDecisionSnapshot:
    run_id: uuid.UUID
    exclusion_kind: str | None
    exclusion_reason: str | None
    excluded_until: datetime | None


@dataclass(frozen=True, slots=True)
class DeviceDecisionSnapshot:
    intents: tuple[IntentSnapshot, ...]
    has_live_session: bool
    ladder: LadderState
    decision_facts: DecisionFacts
    state_facts: DeviceStateFacts
    host_ip: str | None
    host_agent_port: int | None
    node_observed_pack_release: str | None
    node_port: int | None
