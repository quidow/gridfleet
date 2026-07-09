"""Pure projections of device health columns into the public verdicts.

Two distinct concerns live here, deliberately separated:

- ``build_public_summary`` — the display/API projection (three verdicts +
  ``overall``). Consumed by the presenter and the devices API.
- ``merged_liveness`` / ``device_allows_allocation`` — the allocation-gating
  predicate with the historical truth table (a present-but-not-running node
  blocks allocation). Consumed by the state derivation
  (``gather_device_state_facts``), the run
  allocator, grid allocation, and deferred-stop recovery. NOT part of the
  public API shape.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from app.appium_nodes.services.effective_state import compute_effective_state
from app.core.timeutil import now_utc
from app.hosts import service_hardware_telemetry as hardware_telemetry

if TYPE_CHECKING:
    from datetime import datetime

    from app.appium_nodes.models import AppiumNode
    from app.devices.models import Device

HealthVerdictStatus = Literal["ok", "warn", "failed", "unknown"]

_NODE_STATE_TO_STATUS: dict[str, HealthVerdictStatus] = {
    "running": "ok",
    "starting": "warn",
    "stopping": "warn",
    "restarting": "warn",
    "error": "failed",
    "blocked": "failed",
    "stopped": "unknown",
}


def node_running_signal(node: AppiumNode) -> bool:
    if node.health_running is not None:
        return node.health_running
    return node.pid is not None and node.active_connection_target is not None


def _verdict(status: HealthVerdictStatus, detail: str | None, checked_at: datetime | None) -> dict[str, Any]:
    return {
        "status": status,
        "detail": detail,
        "checked_at": checked_at.isoformat() if checked_at is not None else None,
    }


def _device_verdict(device: Device) -> dict[str, Any]:
    hardware = hardware_telemetry.current_hardware_health_status(device)
    checked_at = device.device_checks_checked_at
    if device.device_checks_healthy is False:
        return _verdict("failed", device.device_checks_summary or "Device checks failed", checked_at)
    if hardware.value == "critical":
        return _verdict("failed", "Hardware critical", checked_at)
    if hardware.value == "warning":
        return _verdict("warn", "Hardware warning", checked_at)
    if device.device_checks_healthy is True:
        return _verdict("ok", device.device_checks_summary, checked_at)
    return _verdict("unknown", "not checked", None)


def _node_verdict(device: Device) -> dict[str, Any]:
    node = device.appium_node
    if node is None:
        return _verdict("unknown", "no node", None)
    effective = compute_effective_state(
        pid=node.pid,
        desired_state=node.desired_state.value,
        health_running=node.health_running,
        health_state=node.health_state,
        transition_token=node.transition_token,
        transition_deadline=node.transition_deadline,
        lifecycle_policy_state=device.lifecycle_policy_state,
        review_required=device.review_required,
        now=now_utc(),
    )
    detail = node.health_state if node.health_state and node.health_state != effective else effective
    return _verdict(_NODE_STATE_TO_STATUS[effective], detail, node.last_health_checked_at)


def _viability_verdict(device: Device) -> dict[str, Any]:
    checked_at = device.session_viability_checked_at
    if device.session_viability_status == "failed":
        return _verdict("failed", device.session_viability_error or "session probe failed", checked_at)
    if device.session_viability_status == "passed":
        return _verdict("ok", "passed", checked_at)
    return _verdict("unknown", "not run", None)


def _overall(statuses: list[HealthVerdictStatus]) -> HealthVerdictStatus:
    if "failed" in statuses:
        return "failed"
    if "warn" in statuses:
        return "warn"
    if all(status == "unknown" for status in statuses):
        return "unknown"
    return "ok"


def build_public_summary(device: Device) -> dict[str, Any]:
    device_v = _device_verdict(device)
    node_v = _node_verdict(device)
    viability_v = _viability_verdict(device)
    return {
        "device": device_v,
        "node": node_v,
        "viability": viability_v,
        "overall": _overall([device_v["status"], node_v["status"], viability_v["status"]]),
    }


def merged_liveness(device: Device) -> bool | None:
    """Historical merged-health truth table. Allocation gating only — not display.

    ``False`` ⇔ any present signal fails, where a present-but-not-running node
    counts as failing. ``None`` ⇔ no signals present.
    """
    healthy = True
    has_signal = False
    if isinstance(device.device_checks_healthy, bool):
        healthy = healthy and device.device_checks_healthy
        has_signal = True
    node = device.appium_node
    if node is not None:
        healthy = healthy and node_running_signal(node)
        has_signal = True
    if device.session_viability_status in {"passed", "failed"}:
        healthy = healthy and device.session_viability_status == "passed"
        has_signal = True
    return healthy if has_signal else None


def device_allows_allocation(device: Device) -> bool:
    return merged_liveness(device) is not False
