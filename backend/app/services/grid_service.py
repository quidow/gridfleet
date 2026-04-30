import logging
from typing import Any

import httpx

from app.services.settings_service import settings_service

logger = logging.getLogger(__name__)


async def get_grid_status() -> dict[str, Any]:
    """Fetch Selenium Grid /status and return parsed JSON."""
    url = f"{settings_service.get('grid.hub_url')}/status"
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, timeout=5)
            resp.raise_for_status()
            result: dict[str, Any] = resp.json()
            return result
    except httpx.HTTPError as e:
        logger.warning("Failed to reach Grid hub at %s: %s", url, e)
        return {"ready": False, "error": str(e)}


def available_node_device_ids(grid_data: dict[str, Any]) -> set[str] | None:
    """Return device IDs advertised by UP Grid nodes, or None when Grid status is unavailable."""
    value = grid_data.get("value")
    if not isinstance(value, dict):
        return None

    nodes = value.get("nodes")
    if not isinstance(nodes, list):
        return None

    device_ids: set[str] = set()
    for node in nodes:
        if not isinstance(node, dict):
            continue
        availability = str(node.get("availability") or "UP").upper()
        if availability != "UP":
            continue
        slots = node.get("slots")
        if not isinstance(slots, list):
            continue
        for slot in slots:
            if not isinstance(slot, dict):
                continue
            stereotype = slot.get("stereotype")
            if not isinstance(stereotype, dict):
                continue
            device_id = stereotype.get("appium:gridfleet:deviceId") or stereotype.get("gridfleet:deviceId")
            if isinstance(device_id, str) and device_id:
                device_ids.add(device_id)
    return device_ids
