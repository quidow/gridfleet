"""Column-backed device health service."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy.exc import NoResultFound

from app.appium_nodes.services import locking as appium_node_locking
from app.devices import locking as device_locking
from app.devices.models import Device, DeviceOperationalState
from app.devices.services.health_view import (
    build_public_summary,
    device_allows_allocation,
    node_running_signal,
    node_summary_label,
)
from app.devices.services.readiness import is_ready_for_use_async
from app.devices.services.state import set_operational_state
from app.events import queue_event_for_session

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

__all__ = [
    "apply_node_state_transition",
    "build_public_summary",
    "device_allows_allocation",
    "update_device_checks",
    "update_emulator_state",
    "update_session_viability",
]


def _now() -> datetime:
    return datetime.now(UTC)


async def _lock(db: AsyncSession, device: Device) -> Device | None:
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
    if locked.operational_state != DeviceOperationalState.available:
        return

    await set_operational_state(
        locked,
        DeviceOperationalState.offline,
        reason=reason,
    )


async def _restore_available_for_healthy_signal(
    db: AsyncSession,
    locked: Device,
) -> None:
    if locked.operational_state != DeviceOperationalState.offline:
        return
    if not locked.auto_manage:
        return
    node = locked.appium_node
    if node is None or not node_running_signal(node):
        return
    if not await is_ready_for_use_async(db, locked):
        return
    if not device_allows_allocation(locked):
        return

    await set_operational_state(
        locked,
        DeviceOperationalState.available,
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
    health_running: bool | None = None,
    health_state: str | None = None,
    mark_offline: bool = True,
    reason: str | None = None,
) -> None:
    locked = await _lock(db, device)
    if locked is None:
        return
    locked_node = await appium_node_locking.lock_appium_node_for_device(db, locked.id)
    if locked_node is None:
        return

    locked.appium_node = locked_node
    previous = build_public_summary(locked)

    locked_node.health_running = health_running
    locked_node.health_state = health_state
    locked_node.last_health_checked_at = _now()

    if mark_offline:
        await _mark_offline_for_failed_signal(
            locked,
            failed=not node_running_signal(locked_node),
            reason=reason or f"Node: {node_summary_label(locked_node)}",
        )
    await _restore_available_for_healthy_signal(db, locked)
    _maybe_emit_health_changed(db, locked, previous)


async def update_emulator_state(db: AsyncSession, device: Device, state: str | None) -> None:
    locked = await _lock(db, device)
    if locked is None:
        return
    locked.emulator_state = state
