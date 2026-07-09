"""Detect installed tools and supported platforms on this host."""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from copy import deepcopy
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from agent_app.pack.adapter_registry import AdapterRegistry

logger = logging.getLogger(__name__)

_CAPABILITIES_REFRESH_INTERVAL_SEC = 600
ORCHESTRATION_CONTRACT_VERSION = 4
_DEFAULT_CAPABILITIES: dict[str, Any] = {
    "platforms": [],
    "tools": {},
    "missing_prerequisites": [],
    "orchestration_contract_version": ORCHESTRATION_CONTRACT_VERSION,
}


def default_capabilities() -> dict[str, Any]:
    """Capabilities payload used before the cache has run any detection."""
    return deepcopy(_DEFAULT_CAPABILITIES)


class CapabilitiesCache:
    """Owns the cached host-capabilities snapshot and the adapter registry it reads from."""

    def __init__(self, *, adapter_registry: AdapterRegistry | None) -> None:
        self._adapter_registry = adapter_registry
        self._snapshot: dict[str, Any] | None = None
        self._snapshot_at: float | None = None
        self._lock = asyncio.Lock()

    async def _collect_adapter_tool_versions(self) -> dict[str, str]:
        """Gather tool versions from all loaded adapters."""
        if self._adapter_registry is None:
            return {}
        tools: dict[str, str] = {}
        for pack_id in self._adapter_registry.pack_ids():
            adapter = self._adapter_registry.get_current(pack_id)
            if adapter is not None and hasattr(adapter, "tool_versions"):
                result = adapter.tool_versions()
                if inspect.isawaitable(result):
                    result = await result
                for name, version in result.items():
                    if version is not None and name not in tools:
                        tools[name] = version
        return tools

    async def detect(self) -> dict[str, Any]:
        """Detect installed tools and infer supported platforms."""
        tools = await self._collect_adapter_tool_versions()
        return {
            "platforms": [],
            "tools": tools,
            "missing_prerequisites": [],
            "orchestration_contract_version": ORCHESTRATION_CONTRACT_VERSION,
        }

    def get(self) -> dict[str, Any]:
        """Return the last detected capabilities without running probes."""
        snapshot = deepcopy(self._snapshot or _DEFAULT_CAPABILITIES)
        snapshot["orchestration_contract_version"] = ORCHESTRATION_CONTRACT_VERSION
        return snapshot

    def _is_stale(self) -> bool:
        if self._snapshot_at is None:
            return True
        return time.monotonic() - self._snapshot_at >= _CAPABILITIES_REFRESH_INTERVAL_SEC

    async def refresh(self) -> dict[str, Any]:
        """Refresh and return the cached capabilities snapshot."""
        async with self._lock:
            snapshot = await self.detect()
            self._snapshot = deepcopy(snapshot)
            self._snapshot_at = time.monotonic()
            return deepcopy(snapshot)

    async def get_or_refresh(self, *, force: bool = False) -> dict[str, Any]:
        """Return cached capabilities, refreshing only when missing/stale or forced."""
        if force or self._snapshot is None or self._is_stale():
            return await self.refresh()
        return self.get()

    async def run_refresh_loop(
        self,
        interval_sec: int = _CAPABILITIES_REFRESH_INTERVAL_SEC,
        *,
        refresh_immediately: bool = True,
    ) -> None:
        """Periodically refresh the capability snapshot outside the health request path."""
        if not refresh_immediately:
            await asyncio.sleep(interval_sec)
        while True:
            try:
                await self.refresh()
            except Exception:
                logger.exception("Capability snapshot refresh failed")
            await asyncio.sleep(interval_sec)
