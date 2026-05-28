from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from app.core.protocols import SettingsReader

logger = logging.getLogger(__name__)


class GridService:
    """Selenium Grid hub client.

    A single ``httpx.AsyncClient`` is reused across all calls on a given
    instance (leader loops poll the hub every few seconds —
    ``session_sync_loop``, ``node_health_loop``,
    ``fleet_capacity_collector_loop``). Instantiating per call leaks ~0.8 MB
    of native state on macOS that ``aclose()`` does not release.

    The composition root constructs one ``GridService`` for the process
    lifetime so every consumer shares the same client.
    """

    def __init__(self, *, settings: SettingsReader) -> None:
        self._settings = settings
        self._client: httpx.AsyncClient | None = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient()
        return self._client

    async def close(self) -> None:
        """Close the shared client. Call from app shutdown."""
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None

    async def get_status(self) -> dict[str, Any]:
        """Fetch Selenium Grid /status and return parsed JSON."""
        url = f"{self._settings.get('grid.hub_url')}/status"
        try:
            resp = await self._get_client().get(url, timeout=5)
            resp.raise_for_status()
            result: dict[str, Any] = resp.json()
            return result
        except httpx.HTTPError as e:
            logger.warning("Failed to reach Grid hub at %s: %s", url, e)
            return {"ready": False, "error": "grid_unreachable"}

    async def terminate_session(self, session_id: str) -> bool:
        """Delete a WebDriver session through the Selenium Grid hub.

        Selenium Grid exposes the normal WebDriver endpoint at DELETE /session/{id}.
        A 404 means the session is already gone and is safe to treat as success.
        """
        url = f"{self._settings.get('grid.hub_url')}/session/{session_id}"
        try:
            resp = await self._get_client().delete(url, timeout=10)
            if resp.status_code == 404:
                return True
            resp.raise_for_status()
            return True
        except httpx.HTTPError as exc:
            logger.warning("Failed to terminate Grid session %s at %s: %s", session_id, url, exc)
            return False

    @staticmethod
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
