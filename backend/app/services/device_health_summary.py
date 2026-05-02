from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy.exc import NoResultFound

from app.models.device import Device, DeviceAvailabilityStatus
from app.services import control_plane_state_store

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

HEALTH_SUMMARY_NAMESPACE = "device.health_summary"


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _summary_parts(snapshot: dict[str, Any]) -> list[str]:
    parts: list[str] = []

    device_checks_summary = snapshot.get("device_checks_summary")
    if isinstance(device_checks_summary, str) and device_checks_summary:
        parts.append(device_checks_summary)

    node_state = snapshot.get("node_state")
    if isinstance(node_state, str) and node_state:
        parts.append(f"Node: {node_state}")

    viability_status = snapshot.get("session_viability_status")
    if viability_status == "failed":
        error = snapshot.get("session_viability_error")
        parts.append(f"Session: failed{f' ({error})' if isinstance(error, str) and error else ''}")
    elif viability_status == "passed":
        parts.append("Session: passed")

    return parts


def build_public_health_summary(snapshot: dict[str, Any] | None) -> dict[str, Any]:
    if not snapshot:
        return {"healthy": None, "summary": "Unknown", "last_checked_at": None}

    device_checks_healthy = snapshot.get("device_checks_healthy")
    node_running = snapshot.get("node_running")
    viability_status = snapshot.get("session_viability_status")

    healthy = True
    has_signal = False

    if isinstance(device_checks_healthy, bool):
        healthy = healthy and device_checks_healthy
        has_signal = True
    if isinstance(node_running, bool):
        healthy = healthy and node_running
        has_signal = True
    if viability_status in {"passed", "failed"}:
        healthy = healthy and viability_status == "passed"
        has_signal = True

    parts = _summary_parts(snapshot)
    summary = " | ".join(parts) if parts else ("Healthy" if healthy and has_signal else "Unknown")
    return {
        "healthy": healthy if has_signal else None,
        "summary": summary,
        "last_checked_at": snapshot.get("last_checked_at"),
    }


async def get_health_snapshot(db: AsyncSession, device_id: str) -> dict[str, Any] | None:
    value = await control_plane_state_store.get_value(db, HEALTH_SUMMARY_NAMESPACE, device_id)
    return value if isinstance(value, dict) else None


async def get_health_summary_map(db: AsyncSession, device_ids: list[str]) -> dict[str, dict[str, Any]]:
    values = await control_plane_state_store.get_values(db, HEALTH_SUMMARY_NAMESPACE, device_ids)
    return {
        device_id: build_public_health_summary(value) for device_id, value in values.items() if isinstance(value, dict)
    }


def health_snapshot_allows_allocation(snapshot: dict[str, Any] | None) -> bool:
    return build_public_health_summary(snapshot).get("healthy") is not False


async def device_allows_allocation(db: AsyncSession, device: Device) -> bool:
    return health_snapshot_allows_allocation(await get_health_snapshot(db, str(device.id)))


async def _lock_device_for_health_transition(db: AsyncSession, device: Device | str) -> Device | None:
    if not isinstance(device, Device):
        return None

    from app.services import device_locking

    try:
        return await device_locking.lock_device(db, device.id)
    except NoResultFound:
        return None


async def _mark_offline_for_failed_health_signal(
    db: AsyncSession,
    device: Device | str,
    *,
    failed: bool,
    reason: str,
) -> None:
    if not failed:
        return
    locked = await _lock_device_for_health_transition(db, device)
    if locked is None:
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
    device: Device | str,
    snapshot: dict[str, Any],
    *,
    locked_device: Device | None = None,
) -> None:
    locked = locked_device or await _lock_device_for_health_transition(db, device)
    if locked is None:
        return
    if locked.availability_status != DeviceAvailabilityStatus.offline:
        return
    if not locked.auto_manage:
        return
    if snapshot.get("node_running") is not True:
        return
    if build_public_health_summary(snapshot).get("healthy") is not True:
        return

    from app.models.appium_node import NodeState
    from app.services.device_availability import set_device_availability_status
    from app.services.device_readiness import is_ready_for_use_async

    node = locked.__dict__.get("appium_node")
    if node is None or node.state != NodeState.running:
        return
    if not await is_ready_for_use_async(db, locked):
        return

    await set_device_availability_status(
        locked,
        DeviceAvailabilityStatus.available,
        reason="Health checks recovered",
    )


async def patch_health_snapshot(db: AsyncSession, device: Device | str, updates: dict[str, Any]) -> dict[str, Any]:
    device_key = str(device.id) if isinstance(device, Device) else str(device)
    locked = await _lock_device_for_health_transition(db, device)
    patch = {**updates, "last_checked_at": updates.get("last_checked_at", _now_iso())}
    await control_plane_state_store.patch_value(db, HEALTH_SUMMARY_NAMESPACE, device_key, patch)
    next_snapshot = await get_health_snapshot(db, device_key) or patch
    await _restore_available_for_healthy_signal(db, device, next_snapshot, locked_device=locked)
    return next_snapshot


async def update_device_checks(
    db: AsyncSession,
    device: Device | str,
    *,
    healthy: bool | None,
    summary: str,
) -> dict[str, Any]:
    await _mark_offline_for_failed_health_signal(
        db,
        device,
        failed=healthy is False,
        reason=summary,
    )
    return await patch_health_snapshot(
        db,
        device,
        {
            "device_checks_healthy": healthy,
            "device_checks_summary": summary,
            "device_checks_checked_at": _now_iso(),
        },
    )


async def update_node_state(
    db: AsyncSession,
    device: Device | str,
    *,
    running: bool | None,
    state: str | None,
    mark_offline_on_failure: bool = True,
) -> dict[str, Any]:
    if mark_offline_on_failure:
        await _mark_offline_for_failed_health_signal(
            db,
            device,
            failed=running is False,
            reason=f"Node: {state or 'not running'}",
        )
    return await patch_health_snapshot(
        db,
        device,
        {
            "node_running": running,
            "node_state": state,
            "node_checked_at": _now_iso(),
        },
    )


async def update_session_viability(
    db: AsyncSession,
    device: Device | str,
    *,
    status: str | None,
    error: str | None,
) -> dict[str, Any]:
    await _mark_offline_for_failed_health_signal(
        db,
        device,
        failed=status == "failed",
        reason=error or "Session viability failed",
    )
    return await patch_health_snapshot(
        db,
        device,
        {
            "session_viability_status": status,
            "session_viability_error": error,
            "session_viability_checked_at": _now_iso(),
        },
    )
