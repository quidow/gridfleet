"""Canonical parser for the pushed device_health section (contract v7).

One shared representation used by the inline Phase-3 fold, the StatusFoldLoop
device fold, and the synchronous emulator_state fold, so all three agree on
membership, presence, and the v7-vs-legacy shape decision.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class DeviceHealthItem:
    device_id: uuid.UUID
    probe_status: str  # "observed" | "error"
    presence: str  # "present" | "absent" | "unknown"
    health: dict[str, Any] | None
    lifecycle_state: dict[str, Any]


@dataclass(frozen=True, slots=True)
class PushedDeviceHealth:
    is_v7: bool
    complete_gather: bool
    by_device_id: dict[uuid.UUID, DeviceHealthItem]


def parse_device_health_items(section: dict[str, Any]) -> PushedDeviceHealth:
    raw = section.get("devices")
    if not isinstance(raw, list):
        # Legacy (pre-v7) section: a dict keyed by connection_target. The fold
        # falls back to presence/lifecycle dials for this host.
        return PushedDeviceHealth(is_v7=False, complete_gather=False, by_device_id={})
    by_id: dict[uuid.UUID, DeviceHealthItem] = {}
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            device_id = uuid.UUID(str(item.get("device_id")))
        except ValueError, TypeError:
            continue
        health = item.get("health")
        lifecycle = item.get("lifecycle_state")
        by_id[device_id] = DeviceHealthItem(
            device_id=device_id,
            probe_status=str(item.get("probe_status") or "error"),
            presence=str(item.get("presence") or "unknown"),
            health=health if isinstance(health, dict) else None,
            lifecycle_state=lifecycle if isinstance(lifecycle, dict) else {},
        )
    return PushedDeviceHealth(is_v7=True, complete_gather=bool(section.get("complete_gather")), by_device_id=by_id)
