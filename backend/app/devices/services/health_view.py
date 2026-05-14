"""Pure projections of device health columns into the public summary.

Split out from ``device_health`` so that callers which need to *read* the
combined health view (e.g. ``device_state.ready_operational_state``) can
import it without pulling in the state writers, which themselves depend on
``device_state``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from datetime import datetime

    from app.devices.models import Device
    from app.models.appium_node import AppiumNode


def node_running_signal(node: AppiumNode) -> bool:
    if node.health_running is not None:
        return node.health_running
    return node.pid is not None and node.active_connection_target is not None


def node_summary_label(node: AppiumNode) -> str:
    if node.health_state:
        return node.health_state
    return "running" if node_running_signal(node) else "stopped"


def _summary_parts(device: Device) -> list[str]:
    parts: list[str] = []
    if device.device_checks_summary:
        parts.append(device.device_checks_summary)
    node = device.appium_node
    if node is not None:
        parts.append(f"Node: {node_summary_label(node)}")
    if device.session_viability_status == "failed":
        err = device.session_viability_error
        parts.append(f"Session: failed ({err})" if err else "Session: failed")
    elif device.session_viability_status == "passed":
        parts.append("Session: passed")
    return parts


def build_public_summary(device: Device) -> dict[str, Any]:
    node = device.appium_node
    healthy: bool | None = True
    has_signal = False

    if isinstance(device.device_checks_healthy, bool):
        healthy = healthy and device.device_checks_healthy
        has_signal = True

    if node is not None:
        healthy = healthy and node_running_signal(node)
        has_signal = True

    if device.session_viability_status in {"passed", "failed"}:
        healthy = healthy and device.session_viability_status == "passed"
        has_signal = True

    parts = _summary_parts(device)
    summary_text = " | ".join(parts) if parts else ("Healthy" if healthy and has_signal else "Unknown")

    timestamps: list[datetime] = []
    if device.device_checks_checked_at is not None:
        timestamps.append(device.device_checks_checked_at)
    if device.session_viability_checked_at is not None:
        timestamps.append(device.session_viability_checked_at)
    if node is not None and node.last_health_checked_at is not None:
        timestamps.append(node.last_health_checked_at)
    last_checked = max(timestamps) if timestamps else None

    return {
        "healthy": healthy if has_signal else None,
        "summary": summary_text,
        "last_checked_at": last_checked.isoformat() if last_checked is not None else None,
    }


def device_allows_allocation(device: Device) -> bool:
    return build_public_summary(device).get("healthy") is not False
