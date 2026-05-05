"""Minimal seed scenario — 1 host, 2 devices, 1 completed run + 1 active run."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

from app.models.device import ConnectionType, DeviceOperationalState, DeviceType
from app.models.host import HostStatus, OSType
from app.models.session import SessionStatus
from app.models.test_run import RunState
from app.seeding.factories.device import make_device
from app.seeding.factories.host import make_host
from app.seeding.factories.run import make_reservation, make_run
from app.seeding.factories.session import make_session

if TYPE_CHECKING:
    from app.seeding.context import SeedContext


async def apply_minimal(ctx: SeedContext) -> None:
    host = make_host(
        ctx,
        hostname="lab-linux-01",
        ip="10.0.0.11",
        os_type=OSType.linux,
        status=HostStatus.online,
    )
    ctx.session.add(host)
    await ctx.session.flush()

    device_a = make_device(
        ctx,
        host_id=host.id,
        platform_id="android_mobile",
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
        identity_value="SERIALA",
        name="Pixel 7",
        model="Pixel 7",
        manufacturer="Google",
        os_version="14",
    )
    device_b = make_device(
        ctx,
        host_id=host.id,
        platform_id="android_mobile",
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
        identity_value="SERIALB",
        name="Galaxy S23",
        model="Galaxy S23",
        manufacturer="Samsung",
        os_version="14",
    )
    ctx.session.add_all([device_a, device_b])
    await ctx.session.flush()

    completed_started = ctx.now - timedelta(hours=2)
    completed_run = make_run(
        ctx,
        name="smoke-2",
        state=RunState.completed,
        started_at=completed_started,
        duration_seconds=600,
        requirements=[{"pack_id": "appium-uiautomator2", "platform_id": "android_mobile", "count": 2}],
    )
    ctx.session.add(completed_run)
    await ctx.session.flush()
    ctx.session.add_all(
        [
            make_reservation(ctx, run=completed_run, device=device_a, released=True),
            make_reservation(ctx, run=completed_run, device=device_b, released=True),
        ]
    )
    ctx.session.add(
        make_session(
            ctx,
            run=completed_run,
            device=device_a,
            status=SessionStatus.passed,
            started_at=completed_started,
            duration_seconds=120,
        )
    )

    active_started = ctx.now - timedelta(minutes=3)
    active_run = make_run(
        ctx,
        name="live",
        state=RunState.active,
        started_at=active_started,
        duration_seconds=None,
        requirements=[{"pack_id": "appium-uiautomator2", "platform_id": "android_mobile", "count": 1}],
    )
    ctx.session.add(active_run)
    await ctx.session.flush()
    device_b.operational_state = DeviceOperationalState.busy
    ctx.session.add(make_reservation(ctx, run=active_run, device=device_b, released=False))
    ctx.session.add(
        make_session(
            ctx,
            run=active_run,
            device=device_b,
            status=SessionStatus.running,
            started_at=active_started,
            duration_seconds=None,
        )
    )
