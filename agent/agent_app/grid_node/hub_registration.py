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

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol

from agent_app.grid_node import hub_status_cache
from agent_app.grid_node.protocol import EventType, event_envelope

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

DesiredMode = Literal["registered", "draining", "absent"]


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


class HubRegistrationReconciler:
    """Converges the hub's view of this node onto the desired mode.

    Injection points (callables) keep this unit free of NodeState /
    GridNodeService imports and make every matrix cell unit-testable.
    """

    _REMOVE_CONFIRM_ATTEMPTS = 3

    def __init__(
        self,
        *,
        node_id: str,
        bus: EventPublisher,
        node_payload: Callable[[], dict[str, object]],
        local_run_id: Callable[[], str],
        observe: Callable[[bool], Awaitable[HubObserved | None]],
        has_busy_slots: Callable[[], bool],
        drain_grace_sec: Callable[[], float],
    ) -> None:
        self._node_id = node_id
        self._bus = bus
        self._node_payload = node_payload
        self._local_run_id = local_run_id
        self._observe = observe
        self._has_busy_slots = has_busy_slots
        self._drain_grace_sec = drain_grace_sec
        self._lock = asyncio.Lock()
        self._desired: DesiredMode = "registered"
        # Same semantics as the old _registered_with_hub: start True so an
        # unknown hub degrades to "process up == up" instead of pinning a
        # healthy node at "registering" forever.
        self._registered_with_hub = True
        self._announced = False

    @property
    def desired(self) -> DesiredMode:
        return self._desired

    def set_desired(self, mode: DesiredMode) -> None:
        self._desired = mode

    def mark_announced(self) -> None:
        self._announced = True

    def is_registered_with_hub(self) -> bool:
        return self._registered_with_hub

    async def try_converge(self) -> None:
        """Heartbeat entry point: skip when a converge pass is already running
        (an HTTP-handler-initiated pass holds the lock) instead of queueing —
        queued heartbeat passes would re-run the same matrix cell."""
        if self._lock.locked():
            return
        await self.converge()

    async def converge(self) -> None:
        async with self._lock:
            desired = self._desired
            if desired == "absent":
                await self._converge_absent()
                return
            observed = await self._observe(False)
            if observed is None:
                # Hub unreachable / probe disabled: never churn — except the
                # very first announce, which must keep the old blind-ADDED
                # startup behavior (the hub may simply not be probed yet).
                if desired == "registered" and not self._announced:
                    await self._publish_added()
                return
            if desired == "registered":
                await self._converge_registered(observed)
            else:
                await self._converge_draining(observed)

    async def _converge_registered(self, observed: HubObserved) -> None:
        if not observed.present:
            if self._announced:
                logger.warning("grid_node_reregistering_lost_node", extra={"node_id": self._node_id})
            await self._publish_added()
            return
        if observed.availability == "DRAINING" or observed.run_id != self._local_run_id():
            # Stale registration: a DRAINING husk from a prior incarnation
            # (F-G2), a drain we have since left, or an old stereotype.
            # Selenium fixes stereotypes at registration, so converging is
            # always remove + re-add. Drain first only if the hub still
            # routes here (availability UP) so in-flight sessions finish.
            if observed.availability != "DRAINING":
                await self._bus.publish(event_envelope(EventType.NODE_DRAIN, self._node_id))
                await self._wait_for_busy_slots()
                await self._bus.publish(event_envelope(EventType.NODE_DRAIN_COMPLETE, self._node_id))
            await self._remove_and_confirm()
            await self._publish_added()
            return
        # Hub confirmed it has us: a later unknown observation (hub blip) must
        # not trigger the blind first-announce ADDED.
        self._announced = True
        self._registered_with_hub = True

    async def _converge_draining(self, observed: HubObserved) -> None:
        if observed.present and observed.availability != "DRAINING":
            await self._bus.publish(event_envelope(EventType.NODE_DRAIN, self._node_id))
        # present+DRAINING: heartbeat NODE_STATUS keeps it rendered; absent:
        # an unregistered node cannot be routed to, which is all drain
        # guarantees — re-ADDED happens when desired returns to registered.

    async def _converge_absent(self) -> None:
        # Parity with the old stop(): REMOVED only, now confirmed via probe.
        await self._remove_and_confirm()
        self._registered_with_hub = False

    async def _remove_and_confirm(self) -> None:
        for attempt in range(self._REMOVE_CONFIRM_ATTEMPTS):
            await self._bus.publish(event_envelope(EventType.NODE_REMOVED, self._node_payload()))
            observed = await self._observe(True)
            if observed is None or not observed.present:
                return
            logger.warning(
                "grid_node_remove_not_confirmed",
                extra={"node_id": self._node_id, "attempt": attempt + 1},
            )

    async def _wait_for_busy_slots(self) -> None:
        deadline = asyncio.get_running_loop().time() + self._drain_grace_sec()
        while self._has_busy_slots():
            if asyncio.get_running_loop().time() >= deadline:
                logger.warning("grid_node_drain_timeout", extra={"node_id": self._node_id})
                break
            await asyncio.sleep(0.05)

    async def _publish_added(self) -> None:
        # ZMQ slow-joiner settle is owned by service.start(); a converge-time
        # re-add reuses the long-lived PUB socket, no settle needed.
        await self._bus.publish(event_envelope(EventType.NODE_ADDED, self._node_id))
        await self._bus.publish(event_envelope(EventType.NODE_STATUS, self._node_payload()))
        self._announced = True
        self._registered_with_hub = True
