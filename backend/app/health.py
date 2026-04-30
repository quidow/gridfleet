from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import text

from app.observability import BACKGROUND_LOOP_NAMES, get_background_loop_snapshots, loop_heartbeat_fresh
from app.shutdown import shutdown_coordinator

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


def _now() -> datetime:
    return datetime.now(UTC)


async def check_liveness() -> dict[str, str]:
    return {"status": "ok"}


async def check_readiness(db: AsyncSession) -> tuple[dict[str, Any], int]:
    checks: dict[str, Any] = {}
    shutting_down = shutdown_coordinator.is_shutting_down()
    checks["shutdown"] = {
        "shutting_down": shutting_down,
        "active_requests": shutdown_coordinator.active_requests(),
    }

    if shutting_down:
        return {
            "status": "unhealthy",
            "checks": checks,
        }, 503

    try:
        await db.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception as exc:
        checks["database"] = str(exc)
        payload = {
            "status": "unhealthy",
            "checks": checks,
        }
        return payload, 503

    snapshots = await get_background_loop_snapshots(db)
    current_time = _now()
    loop_checks: dict[str, Any] = {}
    leader_ready = True

    for loop_name in BACKGROUND_LOOP_NAMES:
        snapshot = snapshots.get(loop_name)
        if snapshot is None:
            leader_ready = False
            loop_checks[loop_name] = {
                "healthy": False,
                "reason": "missing",
            }
            continue

        healthy = loop_heartbeat_fresh(snapshot, now=current_time)
        loop_checks[loop_name] = {
            "healthy": healthy,
            "owner": snapshot.get("owner"),
            "last_started_at": snapshot.get("last_started_at"),
            "last_succeeded_at": snapshot.get("last_succeeded_at"),
            "last_error_at": snapshot.get("last_error_at"),
            "last_error": snapshot.get("last_error"),
            "next_expected_at": snapshot.get("next_expected_at"),
        }
        if not healthy:
            leader_ready = False

    checks["control_plane_leader"] = leader_ready
    checks["background_loops"] = loop_checks

    return (
        {
            "status": "ok" if leader_ready else "unhealthy",
            "checks": checks,
        },
        200 if leader_ready else 503,
    )
