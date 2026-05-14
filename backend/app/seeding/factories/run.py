"""TestRun + DeviceReservation factory."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from app.devices.models import DeviceReservation
from app.runs.models import RunState, TestRun

if TYPE_CHECKING:
    from app.devices.models import Device
    from app.seeding.context import SeedContext


def make_run(
    ctx: SeedContext,
    *,
    name: str,
    state: RunState,
    started_at: datetime,
    duration_seconds: float | None,
    requirements: list[dict[str, Any]],
    ttl_minutes: int = 60,
    heartbeat_timeout_sec: int = 120,
    created_by: str | None = "demo-seed",
    error: str | None = None,
) -> TestRun:
    run = TestRun(
        name=name,
        state=state,
        requirements=requirements,
        ttl_minutes=ttl_minutes,
        heartbeat_timeout_sec=heartbeat_timeout_sec,
        created_by=created_by,
        started_at=started_at,
        error=error,
    )
    # Pin created_at to the historical timestamp so dashboards that sort by
    # created_at DESC order runs by when they actually happened — otherwise
    # the server_default (now()) collapses all 500 seeded rows onto seed time
    # and active runs are drowned by the terminal backfill.
    run.created_at = started_at
    if state is RunState.active:
        run.last_heartbeat = ctx.now - timedelta(seconds=ctx.rng.randint(0, 30))
    elif duration_seconds is not None:
        run.completed_at = started_at + timedelta(seconds=duration_seconds)
    return run


def make_reservation(
    ctx: SeedContext,
    *,
    run: TestRun,
    device: Device,
    released: bool,
    excluded: bool = False,
    exclusion_reason: str | None = None,
    host_ip: str | None = None,
) -> DeviceReservation:
    reservation = DeviceReservation(
        run=run,
        device_id=device.id,
        identity_value=device.identity_value,
        connection_target=device.connection_target,
        pack_id=device.pack_id,
        platform_id=device.platform_id,
        platform_label=None,
        os_version=device.os_version,
        host_ip=host_ip,
        excluded=excluded,
        exclusion_reason=exclusion_reason if excluded else None,
        excluded_at=ctx.now if excluded else None,
    )
    if released:
        reservation.released_at = run.completed_at or ctx.now
    return reservation
