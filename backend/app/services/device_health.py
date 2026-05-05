"""Column-backed device health service."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy.exc import NoResultFound

from app.models.appium_node import AppiumNode, NodeState
from app.models.device import Device, DeviceAvailabilityStatus
from app.observability import get_logger
from app.services.event_bus import queue_event_for_session

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = get_logger(__name__)


def _now() -> datetime:
    return datetime.now(UTC)


def _node_running_signal(node: AppiumNode) -> bool:
    if node.health_running is not None:
        return node.health_running
    return node.state == NodeState.running


def _node_summary_label(node: AppiumNode) -> str:
    return node.health_state or node.state.value


def _summary_parts(device: Device) -> list[str]:
    parts: list[str] = []
    if device.device_checks_summary:
        parts.append(device.device_checks_summary)
    node = device.appium_node
    if node is not None:
        parts.append(f"Node: {_node_summary_label(node)}")
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
        healthy = healthy and _node_running_signal(node)
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


async def _lock(db: AsyncSession, device: Device) -> Device | None:
    from app.services import device_locking

    try:
        return await device_locking.lock_device(db, device.id)
    except NoResultFound:
        return None


def _maybe_emit_health_changed(
    db: AsyncSession,
    device: Device,
    previous: dict[str, Any],
) -> None:
    nxt = build_public_summary(device)
    if previous.get("healthy") == nxt.get("healthy"):
        return
    queue_event_for_session(
        db,
        "device.health_changed",
        {
            "device_id": str(device.id),
            "healthy": nxt.get("healthy"),
            "summary": nxt.get("summary"),
        },
    )


async def _mark_offline_for_failed_signal(
    locked: Device,
    *,
    failed: bool,
    reason: str,
) -> None:
    if not failed:
        return
    if locked.availability_status != DeviceAvailabilityStatus.available:
        return
    from app.services.device_availability import set_device_availability_status

    await set_device_availability_status(
        locked,
        DeviceAvailabilityStatus.offline,
        reason=reason,
    )


async def _restore_available_for_healthy_signal(
    db: AsyncSession,
    locked: Device,
) -> None:
    if locked.availability_status != DeviceAvailabilityStatus.offline:
        return
    if not locked.auto_manage:
        return
    from app.services.device_availability import set_device_availability_status
    from app.services.device_readiness import is_ready_for_use_async

    node = locked.appium_node
    if node is None or node.state != NodeState.running:
        return
    if not await is_ready_for_use_async(db, locked):
        return
    if not device_allows_allocation(locked):
        return

    await set_device_availability_status(
        locked,
        DeviceAvailabilityStatus.available,
        reason="Health checks recovered",
    )


async def update_device_checks(
    db: AsyncSession,
    device: Device,
    *,
    healthy: bool,
    summary: str,
) -> None:
    locked = await _lock(db, device)
    if locked is None:
        return
    previous = build_public_summary(locked)
    locked.device_checks_healthy = healthy
    locked.device_checks_summary = summary
    locked.device_checks_checked_at = _now()
    await _mark_offline_for_failed_signal(locked, failed=not healthy, reason=summary)
    await _restore_available_for_healthy_signal(db, locked)
    _maybe_emit_health_changed(db, locked, previous)


async def update_session_viability(
    db: AsyncSession,
    device: Device,
    *,
    status: str | None,
    error: str | None,
) -> None:
    locked = await _lock(db, device)
    if locked is None:
        return
    previous = build_public_summary(locked)
    locked.session_viability_status = status
    locked.session_viability_error = error
    locked.session_viability_checked_at = _now()
    await _mark_offline_for_failed_signal(
        locked,
        failed=status == "failed",
        reason=error or "Session viability failed",
    )
    await _restore_available_for_healthy_signal(db, locked)
    _maybe_emit_health_changed(db, locked, previous)


async def apply_node_state_transition(
    db: AsyncSession,
    device: Device,
    *,
    new_state: NodeState | None = None,
    health_running: bool | None = None,
    health_state: str | None = None,
    mark_offline: bool = True,
    reason: str | None = None,
) -> None:
    from app.services import appium_node_locking

    locked = await _lock(db, device)
    if locked is None:
        return
    locked_node = await appium_node_locking.lock_appium_node_for_device(db, locked.id)
    if locked_node is None:
        return

    locked.appium_node = locked_node
    previous = build_public_summary(locked)

    if new_state is not None:
        locked_node.state = new_state
    locked_node.health_running = health_running
    locked_node.health_state = health_state
    locked_node.last_health_checked_at = _now()

    if mark_offline:
        await _mark_offline_for_failed_signal(
            locked,
            failed=not _node_running_signal(locked_node),
            reason=reason or f"Node: {_node_summary_label(locked_node)}",
        )
    await _restore_available_for_healthy_signal(db, locked)
    _maybe_emit_health_changed(db, locked, previous)


async def update_emulator_state(db: AsyncSession, device: Device, state: str | None) -> None:
    locked = await _lock(db, device)
    if locked is None:
        return
    locked.emulator_state = state
