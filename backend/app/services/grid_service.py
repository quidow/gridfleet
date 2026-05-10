import logging
from typing import Any

import httpx

from app.services.settings_service import settings_service

logger = logging.getLogger(__name__)

# A single httpx.AsyncClient is reused across all calls (leader loops poll the
# hub every few seconds — `session_sync_loop`, `node_health_loop`,
# `fleet_capacity_collector_loop`). Instantiating per call leaks ~0.8 MB of
# native state on macOS that aclose() does not release.
_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient()
    return _client


async def close() -> None:
    """Close the shared client. Call from app shutdown."""
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None


async def get_grid_status() -> dict[str, Any]:
    """Fetch Selenium Grid /status and return parsed JSON."""
    url = f"{settings_service.get('grid.hub_url')}/status"
    try:
        resp = await _get_client().get(url, timeout=5)
        resp.raise_for_status()
        result: dict[str, Any] = resp.json()
        return result
    except httpx.HTTPError as e:
        logger.warning("Failed to reach Grid hub at %s: %s", url, e)
        return {"ready": False, "error": "grid_unreachable"}


async def terminate_grid_session(session_id: str) -> bool:
    """Delete a WebDriver session through the Selenium Grid hub.

    Selenium Grid exposes the normal WebDriver endpoint at DELETE /session/{id}.
    A 404 means the session is already gone and is safe to treat as success.
    """
    url = f"{settings_service.get('grid.hub_url')}/session/{session_id}"
    try:
        resp = await _get_client().delete(url, timeout=10)
        if resp.status_code == 404:
            return True
        resp.raise_for_status()
        return True
    except httpx.HTTPError as exc:
        logger.warning("Failed to terminate Grid session %s at %s: %s", session_id, url, exc)
        return False


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
