"""Detect installed tools and supported platforms on this host."""

from __future__ import annotations

import asyncio
import logging
import time
from copy import deepcopy
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from agent_app.pack.adapter_registry import AdapterRegistry

logger = logging.getLogger(__name__)

_CAPABILITIES_REFRESH_INTERVAL_SEC = 600
ORCHESTRATION_CONTRACT_VERSION = 2
_DEFAULT_CAPABILITIES: dict[str, Any] = {
    "platforms": [],
    "tools": {},
    "missing_prerequisites": [],
    "orchestration_contract_version": ORCHESTRATION_CONTRACT_VERSION,
}
_capabilities_snapshot: dict[str, Any] | None = None
_capabilities_snapshot_at: float | None = None
_capabilities_lock = asyncio.Lock()
_adapter_registry: AdapterRegistry | None = None


def set_adapter_registry(registry: AdapterRegistry | None) -> None:
    global _adapter_registry
    _adapter_registry = registry


def _collect_adapter_tool_versions() -> dict[str, str]:
    """Gather tool versions from all loaded adapters."""
    if _adapter_registry is None:
        return {}
    tools: dict[str, str] = {}
    for pack_id in _adapter_registry.pack_ids():
        adapter = _adapter_registry.get_current(pack_id)
        if adapter is not None and hasattr(adapter, "tool_versions"):
            for name, version in adapter.tool_versions().items():
                if version is not None and name not in tools:
                    tools[name] = version
    return tools


async def detect_capabilities() -> dict[str, Any]:
    """Detect installed tools and infer supported platforms."""
    tools = await asyncio.to_thread(_collect_adapter_tool_versions)
    platforms: list[str] = []
    missing_prerequisites: list[str] = []

    return {
        "platforms": platforms,
        "tools": tools,
        "missing_prerequisites": missing_prerequisites,
        "orchestration_contract_version": ORCHESTRATION_CONTRACT_VERSION,
    }


def get_capabilities_snapshot() -> dict[str, Any]:
    """Return the last detected capabilities without running probes."""
    snapshot = deepcopy(_capabilities_snapshot or _DEFAULT_CAPABILITIES)
    snapshot["orchestration_contract_version"] = ORCHESTRATION_CONTRACT_VERSION
    return snapshot


def clear_capabilities_snapshot() -> None:
    """Clear the cached capabilities snapshot."""
    global _capabilities_snapshot, _capabilities_snapshot_at
    _capabilities_snapshot = None
    _capabilities_snapshot_at = None


def _snapshot_is_stale() -> bool:
    if _capabilities_snapshot_at is None:
        return True
    return time.monotonic() - _capabilities_snapshot_at >= _CAPABILITIES_REFRESH_INTERVAL_SEC


async def refresh_capabilities_snapshot() -> dict[str, Any]:
    """Refresh and return the cached capabilities snapshot."""
    global _capabilities_snapshot, _capabilities_snapshot_at
    async with _capabilities_lock:
        snapshot = await detect_capabilities()
        snapshot["orchestration_contract_version"] = ORCHESTRATION_CONTRACT_VERSION
        _capabilities_snapshot = deepcopy(snapshot)
        _capabilities_snapshot_at = time.monotonic()
        return deepcopy(snapshot)


async def get_or_refresh_capabilities_snapshot(*, force: bool = False) -> dict[str, Any]:
    """Return cached capabilities, refreshing only when missing/stale or forced."""
    if force or _capabilities_snapshot is None or _snapshot_is_stale():
        return await refresh_capabilities_snapshot()
    return get_capabilities_snapshot()


async def capabilities_refresh_loop(
    interval_sec: int = _CAPABILITIES_REFRESH_INTERVAL_SEC,
    *,
    refresh_immediately: bool = True,
) -> None:
    """Periodically refresh the capability snapshot outside the health request path."""
    if not refresh_immediately:
        await asyncio.sleep(interval_sec)
    while True:
        try:
            await refresh_capabilities_snapshot()
        except Exception:
            logger.exception("Capability snapshot refresh failed")
        await asyncio.sleep(interval_sec)
