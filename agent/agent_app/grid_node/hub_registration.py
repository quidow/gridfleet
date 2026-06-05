"""Desired/observed reconciliation of this relay's hub registration.

Single-owner rule: ``HubRegistrationReconciler.converge`` is the ONLY place
that publishes hub lifecycle events (NODE_ADDED / NODE_DRAIN /
NODE_DRAIN_COMPLETE / NODE_REMOVED). Every other component (HTTP reconfigure
handlers, drain/stop flows, the heartbeat) only sets desired state and asks
for a converge pass. This is what makes the F-G2 wedge class impossible: the
2026-06-05 TR10 finding was the heartbeat's drain self-stop killing the event
bus mid-reregistration (RuntimeError("event bus is not started") from the
final NODE_ADDED), leaving a permanent DRAINING husk on the hub.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

from agent_app.grid_node import hub_status_cache

logger = logging.getLogger(__name__)


class EventPublisher(Protocol):
    async def publish(self, event: dict[str, object]) -> None:
        raise NotImplementedError


@dataclass(frozen=True)
class HubObserved:
    present: bool
    availability: str | None  # "UP" / "DRAINING"; None when absent
    run_id: str | None  # first slot's stereotype "gridfleet:run_id"; None when absent/missing


async def observe_hub_node(hub_status_url: str, node_id: str, *, fresh: bool = False) -> HubObserved | None:
    """Structured view of this node as the hub sees it; ``None`` = unknown.

    ``None`` (no URL configured, hub unreachable, unparseable) must never
    cause churn — callers keep their last known state. Absence is only
    definitive on a cache-bypassing fetch (fresh-node race, same rule the
    presence probe used).
    """
    if not hub_status_url:
        return None
    nodes = await hub_status_cache.get_hub_nodes(hub_status_url, fresh=fresh)
    if nodes is None:
        return None
    for node in nodes:
        if node.get("id") != node_id:
            continue
        availability = node.get("availability")
        run_id: str | None = None
        slots = node.get("slots") or []
        if slots and isinstance(slots[0], dict):
            stereotype = slots[0].get("stereotype") or {}
            if isinstance(stereotype, dict):
                raw = stereotype.get("gridfleet:run_id")
                run_id = raw if isinstance(raw, str) else None
        return HubObserved(
            present=True,
            availability=availability if isinstance(availability, str) else None,
            run_id=run_id,
        )
    if not fresh:
        return await observe_hub_node(hub_status_url, node_id, fresh=True)
    return HubObserved(present=False, availability=None, run_id=None)
