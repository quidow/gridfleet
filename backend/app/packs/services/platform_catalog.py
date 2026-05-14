from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.models.device import Device


_VIRTUAL_DEVICE_TYPES = frozenset({"emulator", "simulator"})


def device_is_virtual(device: Device) -> bool:
    dt = getattr(device, "device_type", None)
    if dt is None:
        return False
    value = dt.value if hasattr(dt, "value") else str(dt)
    return value in _VIRTUAL_DEVICE_TYPES


def platform_has_lifecycle_action(lifecycle_actions: list[dict[str, Any]], action_id: str) -> bool:
    return any(a.get("id") == action_id for a in lifecycle_actions)
