"""Chaos seed scenario — 2 hosts, 8 devices, concentrated on error paths.

Target: <5s runtime, <200 DB rows.
Every error signal present:
  - offline host
  - maintenance device
  - flapping events (connectivity_lost + connectivity_restored)
  - stuck session (status=running, ended_at IS NULL, old started_at)
  - failed job
  - device pending verification (needs_attention via readiness)
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

from app.models.device import ConnectionType, DeviceAvailabilityStatus, DeviceType
from app.models.device_event import DeviceEventType
from app.models.host import HostStatus, OSType
from app.models.session import SessionStatus
from app.models.test_run import RunState
from app.seeding.factories.device import make_device
from app.seeding.factories.event import make_device_event
from app.seeding.factories.host import make_host
from app.seeding.factories.job import make_job
from app.seeding.factories.run import make_reservation, make_run
from app.seeding.factories.session import make_session

if TYPE_CHECKING:
    from app.seeding.context import SeedContext


async def apply_chaos(ctx: SeedContext) -> None:
    """Seed the chaos scenario: 2 hosts, 8 devices, every error path present."""
    session = ctx.session
    now = ctx.now

    # ── Hosts ─────────────────────────────────────────────────────────────────
    # host_a: online
    host_a = make_host(
        ctx,
        hostname="chaos-linux-01",
        ip="10.99.0.11",
        os_type=OSType.linux,
        status=HostStatus.online,
        agent_version="1.5.0",
        last_heartbeat_offset=timedelta(seconds=-5),
    )
    # host_b: offline — heartbeat stale > 15 min (satisfies: at least one host offline)
    host_b = make_host(
        ctx,
        hostname="chaos-linux-02",
        ip="10.99.0.12",
        os_type=OSType.linux,
        status=HostStatus.offline,
        agent_version="1.4.2",
        last_heartbeat_offset=timedelta(minutes=-30),
    )
    session.add_all([host_a, host_b])
    await session.flush()

    # ── Devices ───────────────────────────────────────────────────────────────
    # 5 on online host, 2 on offline host
    dev_normal = make_device(
        ctx,
        host_id=host_a.id,
        platform_id="android_mobile",
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
        identity_value="CHAOS_AM00SERIAL",
        name="Pixel 7",
        model="Pixel 7",
        manufacturer="Google",
        os_version="14",
    )
    # maintenance device (satisfies: at least one device with availability_status=maintenance)
    dev_maintenance = make_device(
        ctx,
        host_id=host_a.id,
        platform_id="android_mobile",
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
        identity_value="CHAOS_AM01SERIAL",
        name="Galaxy S22",
        model="Galaxy S22",
        manufacturer="Samsung",
        os_version="13",
        availability_status=DeviceAvailabilityStatus.maintenance,
    )
    # flapping device — will get connectivity_lost/restored event pairs
    dev_flapping = make_device(
        ctx,
        host_id=host_a.id,
        platform_id="ios",
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
        identity_value="CHAOS_IP00UDID000000000000000000000000000",
        name="iPhone 14",
        model="iPhone 14",
        manufacturer="Apple",
        os_version="16.7",
    )
    # device that holds the stuck session
    dev_stuck = make_device(
        ctx,
        host_id=host_a.id,
        platform_id="android_mobile",
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
        identity_value="CHAOS_AM02SERIAL",
        name="Pixel 6",
        model="Pixel 6",
        manufacturer="Google",
        os_version="13",
    )
    # devices on the offline host — mark them offline
    dev_offline_a = make_device(
        ctx,
        host_id=host_b.id,
        platform_id="android_tv",
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
        identity_value="CHAOS_AT00SERIAL",
        name="Android TV Box",
        model="SHIELD TV",
        manufacturer="NVIDIA",
        os_version="11",
        availability_status=DeviceAvailabilityStatus.offline,
    )
    dev_offline_b = make_device(
        ctx,
        host_id=host_b.id,
        platform_id="android_tv",
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
        identity_value="CHAOS_AT01SERIAL",
        name="Chromecast TV",
        model="Chromecast with Google TV",
        manufacturer="Google",
        os_version="12",
        availability_status=DeviceAvailabilityStatus.offline,
    )
    # verification_required: discovered but not yet verified by an admin
    dev_unverified = make_device(
        ctx,
        host_id=host_a.id,
        platform_id="android_mobile",
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
        identity_value="CHAOS_AM03SERIAL",
        name="Pending Pixel",
        model="Pixel 8",
        manufacturer="Google",
        os_version="14",
        verified=False,
    )
    # setup_required: Roku needs developer credentials before it can run sessions.
    dev_roku_setup = make_device(
        ctx,
        host_id=host_a.id,
        platform_id="roku_network",
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.network,
        identity_value="chaos-roku-001",
        connection_target="192.168.60.51",
        name="Chaos Roku",
        model="Roku Ultra",
        manufacturer="Roku",
        os_version="12.5",
        ip_address="192.168.60.51",
        device_config={},
    )

    all_devices = [
        dev_normal,
        dev_maintenance,
        dev_flapping,
        dev_stuck,
        dev_offline_a,
        dev_offline_b,
        dev_unverified,
        dev_roku_setup,
    ]
    session.add_all(all_devices)
    await session.flush()

    # ── Runs ──────────────────────────────────────────────────────────────────
    # 1 completed run (normal)
    completed_started = now - timedelta(hours=3)
    run_completed = make_run(
        ctx,
        name="chaos-completed-01",
        state=RunState.completed,
        started_at=completed_started,
        duration_seconds=300,
        requirements=[{"pack_id": "appium-uiautomator2", "platform_id": "android_mobile", "count": 1}],
    )
    # 1 failed run
    failed_started = now - timedelta(hours=2)
    run_failed = make_run(
        ctx,
        name="chaos-failed-01",
        state=RunState.failed,
        started_at=failed_started,
        duration_seconds=120,
        requirements=[{"pack_id": "appium-uiautomator2", "platform_id": "android_mobile", "count": 1}],
        error="Task timed out",
    )
    # 1 active run (holds dev_stuck in a stuck session)
    active_started = now - timedelta(minutes=45)
    run_active = make_run(
        ctx,
        name="chaos-active-stuck",
        state=RunState.active,
        started_at=active_started,
        duration_seconds=None,
        requirements=[{"pack_id": "appium-uiautomator2", "platform_id": "android_mobile", "count": 1}],
    )

    session.add_all([run_completed, run_failed, run_active])
    await session.flush()

    # ── Reservations ──────────────────────────────────────────────────────────
    session.add(make_reservation(ctx, run=run_completed, device=dev_normal, released=True))
    session.add(make_reservation(ctx, run=run_failed, device=dev_normal, released=True))
    # open reservation for the active/stuck run
    dev_stuck.availability_status = DeviceAvailabilityStatus.busy
    session.add(make_reservation(ctx, run=run_active, device=dev_stuck, released=False))

    # ── Sessions ──────────────────────────────────────────────────────────────
    # Normal terminal sessions
    session.add(
        make_session(
            ctx,
            run=run_completed,
            device=dev_normal,
            status=SessionStatus.passed,
            started_at=completed_started,
            duration_seconds=280,
        )
    )
    session.add(
        make_session(
            ctx,
            run=run_failed,
            device=dev_normal,
            status=SessionStatus.error,
            started_at=failed_started,
            duration_seconds=110,
        )
    )
    # Stuck session: running for >30 min, no ended_at
    # (satisfies: at least one session with status=running and ended_at IS NULL)
    session.add(
        make_session(
            ctx,
            run=run_active,
            device=dev_stuck,
            status=SessionStatus.running,
            started_at=active_started,
            duration_seconds=None,  # no ended_at → stuck
        )
    )

    # ── DeviceEvents (flapping) ───────────────────────────────────────────────
    # Satisfies: at least one connectivity_lost and one connectivity_restored
    events = []
    for pair_i in range(5):
        offset_min = pair_i * 8.0
        lost_ts = now - timedelta(minutes=offset_min + 4)
        restored_ts = now - timedelta(minutes=offset_min + 1)
        events.append(
            make_device_event(
                ctx,
                device_id=dev_flapping.id,
                event_type=DeviceEventType.connectivity_lost,
                created_at=lost_ts,
                details={"pair": pair_i},
            )
        )
        events.append(
            make_device_event(
                ctx,
                device_id=dev_flapping.id,
                event_type=DeviceEventType.connectivity_restored,
                created_at=restored_ts,
                details={"pair": pair_i},
            )
        )
    session.add_all(events)

    # ── Jobs ─────────────────────────────────────────────────────────────────
    jobs = [
        # 1 completed job
        make_job(
            ctx,
            kind="node_health_check",
            status="completed",
            scheduled_at=now - timedelta(hours=1),
            duration_seconds=15.0,
            attempts=1,
        ),
        # 1 failed job at retry limit (satisfies: at least one job with status="failed")
        make_job(
            ctx,
            kind="webhook_delivery",
            status="failed",
            scheduled_at=now - timedelta(hours=2),
            duration_seconds=30.0,
            attempts=3,
            max_attempts=3,
        ),
        # 1 running job
        make_job(
            ctx,
            kind="session_viability",
            status="running",
            scheduled_at=now - timedelta(minutes=10),
            attempts=1,
        ),
    ]
    session.add_all(jobs)
