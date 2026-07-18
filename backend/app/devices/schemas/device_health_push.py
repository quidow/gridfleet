"""Canonical parser for the pushed device_health section (contract v7).

One shared representation used by the inline Phase-3 fold and the StatusFoldLoop
device fold, so both agree on membership, presence, and the v7-vs-legacy shape
decision.
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


@dataclass(frozen=True, slots=True)
class PushedDeviceHealth:
    is_v7: bool
    complete_gather: bool
    by_device_id: dict[uuid.UUID, DeviceHealthItem]


def parse_device_health_items(section: dict[str, Any]) -> PushedDeviceHealth:
    raw = section.get("devices")
    if not isinstance(raw, list):
        # Legacy (pre-v7) section: a dict keyed by connection_target. The fold
        # falls back to the legacy presence dials for this host.
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
        raw_probe_status = item.get("probe_status")
        probe_status = raw_probe_status if raw_probe_status in {"observed", "error"} else "error"
        raw_presence = item.get("presence")
        presence = raw_presence if raw_presence in {"present", "absent", "unknown"} else "unknown"
        by_id[device_id] = DeviceHealthItem(
            device_id=device_id,
            probe_status=probe_status,
            presence=presence,
            health=health if isinstance(health, dict) else None,
        )
    return PushedDeviceHealth(
        is_v7=True,
        complete_gather=section.get("complete_gather") is True,
        by_device_id=by_id,
    )
