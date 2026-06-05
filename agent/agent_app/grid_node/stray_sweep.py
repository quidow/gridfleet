"""Remove hub registrations that point at this host but have no live relay.

A relay that dies without its NODE_REMOVED landing (agent crash, kill -9,
or the pre-fix F-G2 wedge) leaves a husk on the hub — DRAINING or even UP —
that nothing else cleans: the hub does not purge silent nodes and a fresh
relay can only converge its OWN node-id. Port-bumped restarts strand the old
port's id forever (the TR10 probe saw two such husks). Swept on relay start.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from agent_app.grid_node import hub_status_cache
from agent_app.grid_node.protocol import EventType, event_envelope

if TYPE_CHECKING:
    from collections.abc import Callable

    from agent_app.grid_node.hub_registration import EventPublisher

logger = logging.getLogger(__name__)


async def sweep_stray_registrations(
    *,
    hub_status_url: str,
    bus: EventPublisher,
    own_uri: Callable[[str], bool],
    live_node_ids: set[str],
) -> int:
    """Publish NODE_REMOVED for every hub node on our URIs with no live relay.

    Best-effort: unreachable hub or publish failures log and return — the
    sweep reruns on the next relay start. Never raises.
    """
    if not hub_status_url:
        return 0
    nodes = await hub_status_cache.get_hub_nodes(hub_status_url, fresh=True)
    if nodes is None:
        return 0
    removed = 0
    for node in nodes:
        node_id = node.get("id")
        uri = node.get("uri")
        if not isinstance(node_id, str) or not isinstance(uri, str):
            continue
        if node_id in live_node_ids or not own_uri(uri):
            continue
        # Selenium NodeRemovedEvent expects a NodeStatus payload — rebuild it
        # from the hub's own /status entry for the husk.
        payload = {
            "nodeId": node_id,
            "externalUri": uri,
            "version": node.get("version", ""),
            "osInfo": node.get("osInfo", {}),
            "maxSessions": node.get("maxSessions", 1),
            "sessionTimeout": node.get("sessionTimeout", 300000),
            "slots": node.get("slots", []),
            "availability": node.get("availability", "DRAINING"),
            "heartbeatPeriod": node.get("heartbeatPeriod", 5000),
        }
        try:
            await bus.publish(event_envelope(EventType.NODE_REMOVED, payload))
        except Exception:  # best-effort cleanup must never break a node start
            logger.warning("grid_node_stray_sweep_publish_failed", extra={"node_id": node_id})
            continue
        logger.warning("grid_node_stray_registration_removed", extra={"node_id": node_id, "uri": uri})
        removed += 1
    return removed
