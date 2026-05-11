"""Full-demo seed scenario — 4 hosts, 35 devices, ~500 runs over 90 days, chaotic live state."""

from __future__ import annotations

import random as _random_module
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from app.models.appium_node import AppiumNode, NodeState
from app.models.appium_plugin import AppiumPlugin
from app.models.config_audit_log import ConfigAuditLog
from app.models.device import (
    ConnectionType,
    DeviceHold,
    DeviceOperationalState,
    DeviceType,
    HardwareHealthStatus,
)
from app.models.device_event import DeviceEventType
from app.models.device_group import DeviceGroupMembership, GroupType
from app.models.host import HostStatus, OSType
from app.models.host_pack_installation import HostPackDoctorResult, HostPackInstallation
from app.models.host_plugin_runtime_status import HostPluginRuntimeStatus
from app.models.host_runtime_installation import HostRuntimeInstallation
from app.models.host_terminal_session import HostTerminalSession
from app.models.session import SessionStatus
from app.models.test_run import RunState
from app.seeding.factories.device import make_device
from app.seeding.factories.driver_pack import seed_demo_driver_packs
from app.seeding.factories.event import make_device_event, make_system_event
from app.seeding.factories.host import make_host
from app.seeding.factories.job import make_job
from app.seeding.factories.meta import make_device_group, make_setting
from app.seeding.factories.run import make_reservation, make_run
from app.seeding.factories.session import make_session
from app.seeding.factories.telemetry import host_resource_series, make_capacity_snapshot
from app.seeding.factories.webhook import make_webhook, make_webhook_delivery
from app.seeding.time_patterns import log_normal_duration_seconds, sample_run_timestamps

if TYPE_CHECKING:
    import uuid

    from app.models.device import Device
    from app.models.host import Host
    from app.models.system_event import SystemEvent
    from app.models.test_run import TestRun
    from app.seeding.context import SeedContext


ANDROID_MOBILE_PLATFORM_IDS = {"android_mobile"}
IOS_PLATFORM_IDS = {"ios"}
FIRETV_PLATFORM_IDS = {"firetv_real"}


# ---------------------------------------------------------------------------
# Fleet builder helpers
# ---------------------------------------------------------------------------


def _build_linux01_devices(ctx: SeedContext, host: Host) -> list[Device]:
    """10 android_mobile + 3 firetv for lab-linux-01."""
    devices: list[Device] = []

    # Android mobile: 6 real (usb), 4 emulators
    android_real_specs = [
        ("Pixel 6", "Pixel 6", "Google", "13"),
        ("Pixel 7", "Pixel 7", "Google", "14"),
        ("Pixel 8", "Pixel 8", "Google", "14"),
        ("Galaxy S22", "Galaxy S22", "Samsung", "13"),
        ("Galaxy S23", "Galaxy S23", "Samsung", "14"),
        ("Galaxy S22 Ultra", "Galaxy S22 Ultra", "Samsung", "13"),
    ]
    for i, (name, model, mfr, os_ver) in enumerate(android_real_specs):
        devices.append(
            make_device(
                ctx,
                host_id=host.id,
                platform_id="android_mobile",
                device_type=DeviceType.real_device,
                connection_type=ConnectionType.usb,
                identity_value=f"LL01AM{i:02d}SERIAL",
                name=name,
                model=model,
                manufacturer=mfr,
                os_version=os_ver,
            )
        )

    emulator_specs = [
        ("Pixel 7 Emu", "Pixel 7", "Google", "14", "emulator-5554"),
        ("Pixel 6 Emu", "Pixel 6", "Google", "13", "emulator-5556"),
        ("S22 Emu", "Galaxy S22", "Samsung", "13", "emulator-5558"),
        ("S23 Emu", "Galaxy S23", "Samsung", "14", "emulator-5560"),
    ]
    for name, model, mfr, os_ver, serial in emulator_specs:
        devices.append(
            make_device(
                ctx,
                host_id=host.id,
                platform_id="android_mobile",
                device_type=DeviceType.emulator,
                connection_type=ConnectionType.virtual,
                identity_value=serial,
                name=name,
                model=model,
                manufacturer=mfr,
                os_version=os_ver,
            )
        )

    # FireTV: 3 real devices (usb)
    firetv_specs = [
        ("FireTV Stick 4K", "Fire TV Stick 4K", "Amazon", "7.6.8.9", "LL01FT00SERIAL"),
        ("FireTV Cube 2nd", "Fire TV Cube", "Amazon", "8.1.0.9", "LL01FT01SERIAL"),
        ("FireTV Cube 3rd", "Fire TV Cube (3rd Gen)", "Amazon", "8.4.3.4", "LL01FT02SERIAL"),
    ]
    for name, model, mfr, fireos, serial in firetv_specs:
        devices.append(
            make_device(
                ctx,
                host_id=host.id,
                platform_id="firetv_real",
                device_type=DeviceType.real_device,
                connection_type=ConnectionType.usb,
                identity_value=serial,
                name=name,
                model=model,
                manufacturer=mfr,
                os_version=fireos,
            )
        )

    return devices


def _build_roku_devices(ctx: SeedContext, host: Host) -> list[Device]:
    """Two network Roku devices: one ready, one setup-required."""
    return [
        make_device(
            ctx,
            host_id=host.id,
            platform_id="roku_network",
            device_type=DeviceType.real_device,
            connection_type=ConnectionType.network,
            identity_value="roku-living-room-001",
            connection_target="192.168.50.41",
            name="Living Room Roku",
            model="Roku Ultra",
            manufacturer="Roku",
            os_version="12.5",
            ip_address="192.168.50.41",
            device_config={"roku_password": "demo-password"},
        ),
        make_device(
            ctx,
            host_id=host.id,
            platform_id="roku_network",
            device_type=DeviceType.real_device,
            connection_type=ConnectionType.network,
            identity_value="roku-lab-setup-002",
            connection_target="192.168.50.42",
            name="Setup Required Roku",
            model="Roku Streaming Stick 4K",
            manufacturer="Roku",
            os_version="12.0",
            ip_address="192.168.50.42",
            device_config={},
        ),
    ]


def _build_linux02_devices(ctx: SeedContext, host: Host) -> list[Device]:
    """3 android_tv for lab-linux-02 (offline host — devices also appear offline)."""
    android_tv_specs = [
        ("Android TV 4K", "SHIELD TV", "NVIDIA", "11", "LL02AT00SERIAL"),
        ("Android TV Box", "Chromecast with Google TV", "Google", "12", "LL02AT01SERIAL"),
        ("Android TV Stick", "Mi TV Stick", "Xiaomi", "9", "LL02AT02SERIAL"),
    ]
    devices: list[Device] = []
    for name, model, mfr, os_ver, serial in android_tv_specs:
        devices.append(
            make_device(
                ctx,
                host_id=host.id,
                platform_id="android_tv",
                device_type=DeviceType.real_device,
                connection_type=ConnectionType.usb,
                identity_value=serial,
                name=name,
                model=model,
                manufacturer=mfr,
                os_version=os_ver,
                # offline because the host went down
                status=DeviceOperationalState.offline,
            )
        )
    return devices


def _build_mac01_devices(ctx: SeedContext, host: Host) -> list[Device]:
    """6 ios + 3 tvos for lab-mac-01."""
    devices: list[Device] = []

    # iOS: 3 real (usb), 3 simulators
    ios_specs = [
        ("iPhone 15 Pro", "iPhone 15 Pro", "Apple", "17.2", "LM01IP00UDID0000000000000000000000000000"),
        ("iPhone 14", "iPhone 14", "Apple", "16.7", "LM01IP01UDID0000000000000000000000000000"),
        ("iPhone 13", "iPhone 13", "Apple", "16.5", "LM01IP02UDID0000000000000000000000000000"),
    ]
    for name, model, mfr, os_ver, udid in ios_specs:
        devices.append(
            make_device(
                ctx,
                host_id=host.id,
                platform_id="ios",
                device_type=DeviceType.real_device,
                connection_type=ConnectionType.usb,
                identity_value=udid,
                name=name,
                model=model,
                manufacturer=mfr,
                os_version=os_ver,
            )
        )

    ios_sim_specs = [
        ("iPhone 15 Sim", "iPhone 15", "Apple", "17.2", "LM01SIM00-0000-0000-0000-000000000000"),
        ("iPhone SE Sim", "iPhone SE (3rd Gen)", "Apple", "16.4", "LM01SIM01-0000-0000-0000-000000000000"),
        ("iPhone 14 Sim", "iPhone 14", "Apple", "16.7", "LM01SIM02-0000-0000-0000-000000000000"),
    ]
    for name, model, mfr, os_ver, simid in ios_sim_specs:
        devices.append(
            make_device(
                ctx,
                host_id=host.id,
                platform_id="ios",
                device_type=DeviceType.simulator,
                connection_type=ConnectionType.virtual,
                identity_value=simid,
                name=name,
                model=model,
                manufacturer=mfr,
                os_version=os_ver,
            )
        )

    # tvOS: 3 simulators
    tvos_sim_specs = [
        ("Apple TV 4K Sim", "Apple TV 4K (2nd gen)", "Apple", "16.4", "LM01TV00-0000-0000-0000-000000000000"),
        ("Apple TV 4K 3rd Sim", "Apple TV 4K (3rd gen)", "Apple", "17.0", "LM01TV01-0000-0000-0000-000000000000"),
        ("Apple TV HD Sim", "Apple TV HD", "Apple", "16.4", "LM01TV02-0000-0000-0000-000000000000"),
    ]
    for name, model, mfr, os_ver, simid in tvos_sim_specs:
        devices.append(
            make_device(
                ctx,
                host_id=host.id,
                platform_id="tvos",
                device_type=DeviceType.simulator,
                connection_type=ConnectionType.virtual,
                identity_value=simid,
                name=name,
                model=model,
                manufacturer=mfr,
                os_version=os_ver,
            )
        )

    return devices


def _build_mac02_devices(ctx: SeedContext, host: Host) -> list[Device]:
    """5 android_mobile + 3 ios for lab-mac-02."""
    devices: list[Device] = []

    # Android mobile: 3 real (usb) + 2 emulators
    android_specs = [
        (
            "Pixel 8 Pro",
            "Pixel 8 Pro",
            "Google",
            "14",
            DeviceType.real_device,
            ConnectionType.usb,
            "LM02AM00SERIAL",
            None,
            None,
        ),
        (
            "Galaxy S23+",
            "Galaxy S23+",
            "Samsung",
            "14",
            DeviceType.real_device,
            ConnectionType.usb,
            "LM02AM01SERIAL",
            None,
            None,
        ),
        (
            "Pixel 7a",
            "Pixel 7a",
            "Google",
            "14",
            DeviceType.real_device,
            ConnectionType.network,
            "LM02AM02SERIAL",
            "10.0.0.61:5555",
            "10.0.0.61",
        ),
        (
            "Pixel 8 Emu",
            "Pixel 8",
            "Google",
            "14",
            DeviceType.emulator,
            ConnectionType.virtual,
            "emulator-5554",
            None,
            None,
        ),
        (
            "S23 Emu",
            "Galaxy S23",
            "Samsung",
            "14",
            DeviceType.emulator,
            ConnectionType.virtual,
            "emulator-5556",
            None,
            None,
        ),
    ]
    for name, model, mfr, os_ver, dt, ct, serial, connection_target, ip_address in android_specs:
        devices.append(
            make_device(
                ctx,
                host_id=host.id,
                platform_id="android_mobile",
                device_type=dt,
                connection_type=ct,
                identity_value=serial,
                connection_target=connection_target,
                name=name,
                model=model,
                manufacturer=mfr,
                os_version=os_ver,
                ip_address=ip_address,
            )
        )

    # iOS: 3 real (usb)
    ios_specs = [
        ("iPhone 15", "iPhone 15", "Apple", "17.1", "LM02IP00UDID0000000000000000000000000000"),
        ("iPhone 14 Pro", "iPhone 14 Pro", "Apple", "16.6", "LM02IP01UDID0000000000000000000000000000"),
        ("iPhone SE", "iPhone SE (3rd Gen)", "Apple", "16.4", "LM02IP02UDID0000000000000000000000000000"),
    ]
    for name, model, mfr, os_ver, udid in ios_specs:
        devices.append(
            make_device(
                ctx,
                host_id=host.id,
                platform_id="ios",
                device_type=DeviceType.real_device,
                connection_type=ConnectionType.usb,
                identity_value=udid,
                name=name,
                model=model,
                manufacturer=mfr,
                os_version=os_ver,
            )
        )

    return devices


def _apply_special_device_states(
    ctx: SeedContext,
    all_devices: list[Device],
) -> tuple[Device, Device, Device]:
    """Apply chaotic special-case states to select devices.

    Returns (flapping_device, reserved_device, excluded_device).
    The caller must tie the live-run devices to active reservations after runs
    are built.
    """
    # 3 devices with verified_at=None (pending verification)
    for d in all_devices[:3]:
        d.verified_at = None

    # 2 devices in maintenance
    for d in all_devices[3:5]:
        d.hold = DeviceHold.maintenance

    # 1 device offline (device-only fault, host is online)
    offline_device = all_devices[5]
    offline_device.operational_state = DeviceOperationalState.offline
    offline_device.hardware_health_status = HardwareHealthStatus.unknown
    offline_device.hardware_telemetry_reported_at = None

    # 1 device reserved — mark availability; caller handles open reservation
    reserved_device = all_devices[6]
    reserved_device.hold = DeviceHold.reserved

    # 1 flapping device — the connectivity events are added in _build_events
    flapping_device = all_devices[7]
    flapping_device.hardware_health_status = HardwareHealthStatus.warning

    # 1 excluded-from-run device — kept reserved and attached to an active run
    # later so the dashboard can surface the Excluded state.
    excluded_device = all_devices[9]
    excluded_device.hold = DeviceHold.reserved

    # 2 devices reporting a critical hardware health status (e.g. overheating
    # or low battery) so the dashboard shows the warning/critical UI states.
    for d in all_devices[8:10]:
        d.hardware_health_status = HardwareHealthStatus.critical
    # ~5 more random warnings scattered across the remaining fleet
    candidates = [d for d in all_devices[10:] if d.hardware_health_status is HardwareHealthStatus.healthy]
    for d in ctx.rng.sample(candidates, min(5, len(candidates))):
        d.hardware_health_status = HardwareHealthStatus.warning

    # Populate lifecycle_policy_state on a handful of devices so the dashboard's
    # "Device recovery" top list shows Deferred Stop / Backing Off / Recovery
    # Eligible / Manual Recovery entries instead of "No recovery work right now".
    _apply_lifecycle_policy_states(ctx, all_devices, offline_device=offline_device)

    return flapping_device, reserved_device, excluded_device


def _apply_device_config_defaults(all_devices: list[Device]) -> None:
    """Populate pack-required setup fields while leaving one setup-required example."""
    for device in all_devices:
        if device.pack_id == "appium-xcuitest":
            device.device_config = {}


def _apply_lifecycle_policy_states(
    ctx: SeedContext,
    all_devices: list[Device],
    *,
    offline_device: Device,
) -> None:
    """Seed a variety of lifecycle_policy_state payloads across the fleet.

    The backend derives the dashboard "Device recovery" summary states from
    this JSON — see app.services.lifecycle_policy_summary.build_lifecycle_policy.
    """

    def _iso(offset_seconds: int) -> str:
        return (ctx.now + timedelta(seconds=offset_seconds)).isoformat()

    # Deferred Stop — available/reserved device has a stop queued, waiting on
    # the client to finish its current session.
    deferred_device = all_devices[10]
    deferred_device.lifecycle_policy_state = _policy(
        stop_pending=True,
        stop_pending_reason="Waiting for active client session to finish",
        stop_pending_since=_iso(-300),
    )

    # Backing Off — repeated recovery failures pushed the device into backoff.
    backoff_device = all_devices[11]
    backoff_device.lifecycle_policy_state = _policy(
        last_action="restart_appium_node",
        last_action_at=_iso(-180),
        last_failure_source="appium_node",
        last_failure_reason="Appium node failed health check 3 times in a row",
        recovery_backoff_attempts=2,
        backoff_until=_iso(600),
    )

    # Recovery Eligible — the already-offline device gets a last_action so the
    # summary promotes it from idle to "Recovery Eligible".
    offline_device.auto_manage = True
    offline_device.lifecycle_policy_state = _policy(
        last_action="mark_unreachable",
        last_action_at=_iso(-900),
        last_failure_source="device_connectivity",
        last_failure_reason="Device dropped off ADB bus",
    )

    # Manual Recovery — another offline device with auto_manage disabled, so
    # the operator has to intervene by hand.
    manual_device = all_devices[12]
    manual_device.operational_state = DeviceOperationalState.offline
    manual_device.hardware_health_status = HardwareHealthStatus.unknown
    manual_device.hardware_telemetry_reported_at = None
    manual_device.auto_manage = False
    manual_device.lifecycle_policy_state = _policy(
        last_action="mark_unreachable",
        last_action_at=_iso(-1800),
        last_failure_source="appium_node",
        last_failure_reason="Node refused to start — USB hub power cycle required",
        recovery_suppressed_reason="auto_manage is disabled for this device",
    )

    # Suppressed — available device where auto-recovery is explicitly paused.
    suppressed_device = all_devices[13]
    suppressed_device.lifecycle_policy_state = _policy(
        last_action="suppress_recovery",
        last_action_at=_iso(-60),
        last_failure_source="operator",
        last_failure_reason="Paused by operator during firmware rollout",
        recovery_suppressed_reason="Paused by operator during firmware rollout",
    )


def _policy(**overrides: object) -> dict[str, Any]:
    state: dict[str, Any] = {
        "last_failure_source": None,
        "last_failure_reason": None,
        "last_action": None,
        "last_action_at": None,
        "stop_pending": False,
        "stop_pending_reason": None,
        "stop_pending_since": None,
        "recovery_suppressed_reason": None,
        "backoff_until": None,
        "recovery_backoff_attempts": 0,
    }
    state.update(overrides)
    return state


# ---------------------------------------------------------------------------
# Run builder
# ---------------------------------------------------------------------------


def _build_runs(
    ctx: SeedContext,
    all_devices: list[Device],
    reserved_device: Device,
    excluded_device: Device,
) -> tuple[list[TestRun], list[Device]]:
    """Build ~500 runs across 90 days with target outcome distribution.

    Returns (runs, active_run_devices) where active_run_devices are the devices
    held by active runs (for open reservations + sessions).
    """
    rng = ctx.rng
    now = ctx.now

    # Outage: day 45 14:00 → day 46 20:00
    outage_start = now - timedelta(days=45, hours=10)
    outage_end = now - timedelta(days=44, hours=4)

    timestamps = sample_run_timestamps(
        rng=rng,
        now=now,
        days_back=90,
        target_total=500,
        outage_window=(outage_start, outage_end),
    )

    # Outcome weights: 72% completed, 15% failed, 8% cancelled, 3% expired, 2% active
    # Active runs: exactly 2 (last 2 timestamps become active)
    n_active = 2
    terminal_timestamps = timestamps[:-n_active]
    active_timestamps = timestamps[-n_active:]

    # Partition terminal timestamps by target distribution
    terminal_states = _assign_terminal_states(rng, len(terminal_timestamps))

    # Platform pool for reservations (exclude offline/maintenance/reserved devices that can't run)
    runnable = [
        d
        for d in all_devices
        if d.hold != DeviceHold.maintenance
        and d.operational_state != DeviceOperationalState.offline
        and d not in {reserved_device, excluded_device}
    ]
    active_busy_candidates = [
        d
        for d in runnable
        if d.platform_id in ANDROID_MOBILE_PLATFORM_IDS
        and d.operational_state is DeviceOperationalState.available
        and d.verified_at is not None
    ]

    runs: list[TestRun] = []

    # Build terminal runs
    for ts, state in zip(terminal_timestamps, terminal_states, strict=True):
        duration = log_normal_duration_seconds(rng)
        run = make_run(
            ctx,
            name=f"run-{rng.randrange(100_000):05d}",
            state=state,
            started_at=ts,
            duration_seconds=duration if state in {RunState.completed, RunState.failed} else duration * 0.3,
            requirements=_random_requirements(rng),
            error="Task timed out" if state is RunState.failed else None,
        )
        runs.append(run)

    # Build active runs — started recently, no duration
    active_run_devices: list[Device] = []
    for i, _ts in enumerate(active_timestamps):
        active_started = now - timedelta(minutes=rng.randint(3, 20))
        active_requirements: list[dict[str, object]] = [
            {"pack_id": "appium-uiautomator2", "platform_id": "android_mobile", "count": 1}
        ]
        if i == 0:
            active_requirements = [{"pack_id": "appium-uiautomator2", "platform_id": "android_mobile", "count": 3}]
        run = make_run(
            ctx,
            name=f"live-run-{i:02d}",
            state=RunState.active,
            started_at=active_started,
            duration_seconds=None,
            requirements=active_requirements,
        )
        runs.append(run)
    active_run_devices.extend(rng.sample(active_busy_candidates, n_active))

    return runs, active_run_devices


def _assign_terminal_states(rng: object, n: int) -> list[RunState]:
    """Distribute n terminal runs across states per spec ratios."""
    assert isinstance(rng, _random_module.Random)

    # 72 / 15 / 8 / 3 split (sums to 98; remainder goes to completed)
    weights = [
        (RunState.completed, 72),
        (RunState.failed, 15),
        (RunState.cancelled, 8),
        (RunState.expired, 5),
    ]
    total_weight = sum(w for _, w in weights)
    result: list[RunState] = []
    for state, weight in weights:
        count = round(n * weight / total_weight)
        result.extend([state] * count)
    # Fill remainder with completed to hit exactly n
    while len(result) < n:
        result.append(RunState.completed)
    result = result[:n]
    rng.shuffle(result)
    return result


def _random_requirements(rng: object) -> list[dict[str, object]]:
    assert isinstance(rng, _random_module.Random)

    roll = rng.random()
    android = {"pack_id": "appium-uiautomator2", "platform_id": "android_mobile"}
    ios = {"pack_id": "appium-xcuitest", "platform_id": "ios"}
    firetv = {"pack_id": "appium-uiautomator2", "platform_id": "firetv_real"}
    if roll < 0.60:
        base = rng.choice([android, android, android, ios, ios, firetv])
        return [{**base, "count": rng.randint(1, 3)}]
    if roll < 0.85:
        return [
            {**android, "count": rng.randint(1, 2)},
            {**ios, "count": 1},
        ]
    return [
        {
            **android,
            "count": 1,
            "tags": {"manufacturer": "Google"},
        }
    ]


# ---------------------------------------------------------------------------
# Reservations + Sessions builder
# ---------------------------------------------------------------------------


def _build_reservations_and_sessions(
    ctx: SeedContext,
    runs: list[TestRun],
    all_devices: list[Device],
    active_run_devices: list[Device],
    reserved_device: Device,
    excluded_device: Device,
) -> dict[object, uuid.UUID]:
    """Add reservations and sessions for every run directly to ctx.session."""
    rng = ctx.rng
    session = ctx.session

    # Separate active vs terminal
    active_runs = [r for r in runs if r.state is RunState.active]
    terminal_runs = [r for r in runs if r.state is not RunState.active]

    # Runnable pool for terminal runs
    runnable = [d for d in all_devices if d.hold is not DeviceHold.maintenance]

    # Active runs: open reservations + running sessions. Seed one busy device,
    # one still-reserved device, and one excluded reservation in the live fleet.
    active_grid_run_ids: dict[object, uuid.UUID] = {}
    for i, (run, device) in enumerate(zip(active_runs, active_run_devices, strict=True)):
        session.add(make_reservation(ctx, run=run, device=device, released=False))
        active_grid_run_ids[device.id] = run.id
        run_started = run.started_at
        assert run_started is not None  # make_run always sets started_at
        device.operational_state = DeviceOperationalState.busy
        session.add(
            make_session(
                ctx,
                run=run,
                device=device,
                status=SessionStatus.running,
                started_at=run_started,
                duration_seconds=None,
            )
        )
        if i == 0:
            session.add(make_reservation(ctx, run=run, device=reserved_device, released=False))
            active_grid_run_ids[reserved_device.id] = run.id
            session.add(
                make_reservation(
                    ctx,
                    run=run,
                    device=excluded_device,
                    released=False,
                    excluded=True,
                    exclusion_reason="Device dropped off ADB bus mid-run",
                )
            )

    # Terminal runs: released reservations + terminal sessions
    # Also inject 2 stuck sessions (running > 10 min, no ended_at) into early runs
    stuck_injected = 0
    for i, run in enumerate(terminal_runs):
        n_devices = rng.randint(1, 6)
        reserved_devices = rng.sample(runnable, min(n_devices, len(runnable)))

        excluded_idx = None
        # 10% chance of one excluded device
        if rng.random() < 0.10 and len(reserved_devices) > 1:
            excluded_idx = rng.randrange(len(reserved_devices))

        for j, device in enumerate(reserved_devices):
            excluded = j == excluded_idx
            session.add(
                make_reservation(
                    ctx,
                    run=run,
                    device=device,
                    released=True,
                    excluded=excluded,
                    exclusion_reason="device_health_fail" if excluded else None,
                )
            )
            if not excluded:
                # Stuck sessions: first 2 terminal runs get a running session > 10 min old
                if stuck_injected < 2 and i < 5:
                    stuck_started = ctx.now - timedelta(minutes=rng.randint(15, 60))
                    session.add(
                        make_session(
                            ctx,
                            run=run,
                            device=device,
                            status=SessionStatus.running,
                            started_at=stuck_started,
                            duration_seconds=None,
                        )
                    )
                    stuck_injected += 1
                else:
                    status = _random_session_status(rng)
                    duration = log_normal_duration_seconds(rng)
                    run_started = run.started_at
                    assert run_started is not None  # make_run always sets started_at
                    session.add(
                        make_session(
                            ctx,
                            run=run,
                            device=device,
                            status=status,
                            started_at=run_started,
                            duration_seconds=duration,
                        )
                    )

    return active_grid_run_ids


def _random_session_status(rng: object) -> SessionStatus:
    assert isinstance(rng, _random_module.Random)
    r = rng.random()
    if r < 0.75:
        return SessionStatus.passed
    elif r < 0.90:
        return SessionStatus.failed
    else:
        return SessionStatus.error


# ---------------------------------------------------------------------------
# Events builder
# ---------------------------------------------------------------------------


def _build_device_events(
    ctx: SeedContext,
    all_devices: list[Device],
    flapping_device: Device,
) -> None:
    """Add ~2000 DeviceEvent rows to ctx.session."""
    rng = ctx.rng
    session = ctx.session

    now = ctx.now
    normal_types = [
        DeviceEventType.health_check_fail,
        DeviceEventType.connectivity_lost,
        DeviceEventType.connectivity_restored,
        DeviceEventType.node_crash,
        DeviceEventType.node_restart,
        DeviceEventType.hardware_health_changed,
        DeviceEventType.lifecycle_recovered,
        DeviceEventType.lifecycle_run_excluded,
        DeviceEventType.lifecycle_run_restored,
        DeviceEventType.lifecycle_recovery_backoff,
        DeviceEventType.lifecycle_recovery_failed,
        DeviceEventType.lifecycle_auto_stopped,
    ]

    # ~40 events per regular device distributed across 90 days
    events = []
    for device in all_devices:
        if device is flapping_device:
            continue
        n_events = rng.randint(30, 60)
        for _ in range(n_events):
            offset_s = rng.uniform(0, 90 * 24 * 3600)
            ts = now - timedelta(seconds=offset_s)
            events.append(
                make_device_event(
                    ctx,
                    device_id=device.id,
                    event_type=rng.choice(normal_types),
                    created_at=ts,
                )
            )

    # Flapping device: ~40 paired connectivity_lost / connectivity_restored in last hour
    for pair_i in range(20):
        offset_min = rng.uniform(0, 55)
        lost_ts = now - timedelta(minutes=offset_min + 2)
        restored_ts = now - timedelta(minutes=offset_min)
        events.append(
            make_device_event(
                ctx,
                device_id=flapping_device.id,
                event_type=DeviceEventType.connectivity_lost,
                created_at=lost_ts,
                details={"pair": pair_i},
            )
        )
        events.append(
            make_device_event(
                ctx,
                device_id=flapping_device.id,
                event_type=DeviceEventType.connectivity_restored,
                created_at=restored_ts,
                details={"pair": pair_i},
            )
        )

    session.add_all(events)


def _build_system_events(ctx: SeedContext) -> list[SystemEvent]:
    """Build ~350 SystemEvent rows. Returns the list (caller flushes to get IDs)."""
    rng = ctx.rng
    now = ctx.now

    event_types = [
        "run.completed",
        "run.failed",
        "run.cancelled",
        "host.offline",
        "host.online",
        "device.maintenance_start",
        "device.maintenance_end",
        "webhook.delivered",
        "webhook.failed",
        "config.updated",
        "session.stuck",
        "device.verified",
        "lifecycle.incident_open",
        "lifecycle.incident_resolved",
        "node.crash",
        "node.restart",
    ]

    events = []
    for _ in range(350):
        offset_s = rng.uniform(0, 90 * 24 * 3600)
        ts = now - timedelta(seconds=offset_s)
        etype = rng.choice(event_types)
        events.append(
            make_system_event(
                ctx,
                event_type=etype,
                data={"seed": True, "event_type": etype},
                created_at=ts,
            )
        )

    return events


# ---------------------------------------------------------------------------
# Jobs builder
# ---------------------------------------------------------------------------


def _build_jobs(ctx: SeedContext) -> None:
    """14 durable job rows per spec."""
    rng = ctx.rng
    session = ctx.session

    job_kinds = [
        "property_refresh",
        "discovery_sync",
        "node_health_check",
        "session_viability",
        "run_reaper",
        "data_cleanup",
        "webhook_delivery",
    ]

    jobs = []
    now = ctx.now

    # 8 completed
    for _ in range(8):
        jobs.append(
            make_job(
                ctx,
                kind=rng.choice(job_kinds),
                status="completed",
                scheduled_at=now - timedelta(hours=rng.randint(1, 72)),
                duration_seconds=rng.uniform(10, 300),
                attempts=1,
            )
        )

    # 2 running
    for _ in range(2):
        jobs.append(
            make_job(
                ctx,
                kind=rng.choice(job_kinds),
                status="running",
                scheduled_at=now - timedelta(minutes=rng.randint(1, 30)),
                attempts=1,
            )
        )

    # 1 queued (pending)
    jobs.append(
        make_job(
            ctx,
            kind=rng.choice(job_kinds),
            status="pending",
            scheduled_at=now + timedelta(minutes=5),
            attempts=0,
        )
    )

    # 3 failed at retry limit
    for _ in range(3):
        jobs.append(
            make_job(
                ctx,
                kind=rng.choice(job_kinds),
                status="failed",
                scheduled_at=now - timedelta(hours=rng.randint(2, 48)),
                duration_seconds=rng.uniform(5, 60),
                attempts=3,
                max_attempts=3,
            )
        )

    session.add_all(jobs)


# ---------------------------------------------------------------------------
# Pack runtime, node, and operator-history builders
# ---------------------------------------------------------------------------


def _build_pack_runtime_status(ctx: SeedContext, hosts: list[Host]) -> None:
    """Seed rows that mirror agent /agent/driver-packs/status payloads."""
    session = ctx.session
    host_by_name = {host.hostname: host for host in hosts}

    session.add_all(
        [
            AppiumPlugin(
                name="images",
                version="2.0.0",
                source="npm",
                package="appium-plugin-images",
                enabled=True,
                notes="Required by visual comparison smoke suites.",
            ),
            AppiumPlugin(
                name="relaxed-caps",
                version="1.0.6",
                source="npm",
                package="appium-relaxed-caps-plugin",
                enabled=False,
                notes="Kept disabled for compatibility testing.",
            ),
        ]
    )

    def _runtime(
        host: Host,
        runtime_id: str,
        *,
        appium_version: str,
        driver_package: str,
        driver_version: str,
        status: str = "installed",
        blocked_reason: str | None = None,
    ) -> HostRuntimeInstallation:
        return HostRuntimeInstallation(
            host_id=host.id,
            runtime_id=runtime_id,
            appium_server_package="appium",
            appium_server_version=appium_version,
            driver_specs=[{"package": driver_package, "version": driver_version}],
            plugin_specs=[{"package": "appium-plugin-images", "version": "2.0.0"}],
            appium_home=f"/opt/gridfleet-agent/runtimes/{runtime_id}",
            refcount=1 if status == "installed" else 0,
            status=status,
            blocked_reason=blocked_reason,
        )

    linux01 = host_by_name["lab-linux-01"]
    linux02 = host_by_name["lab-linux-02"]
    mac01 = host_by_name["lab-mac-01"]
    mac02 = host_by_name["lab-mac-02"]

    runtime_rows = [
        _runtime(
            linux01,
            "runtime-uiautomator2-3-6",
            appium_version="2.11.5",
            driver_package="appium-uiautomator2-driver",
            driver_version="3.6.0",
        ),
        _runtime(
            linux01,
            "runtime-roku-0-13",
            appium_version="2.11.5",
            driver_package="@dlenroc/appium-roku-driver",
            driver_version="0.13.3",
        ),
        _runtime(
            linux02,
            "runtime-uiautomator2-3-5",
            appium_version="2.11.5",
            driver_package="appium-uiautomator2-driver",
            driver_version="3.5.1",
        ),
        _runtime(
            mac01,
            "runtime-uiautomator2-3-6",
            appium_version="2.11.5",
            driver_package="appium-uiautomator2-driver",
            driver_version="3.6.0",
        ),
        _runtime(
            mac01,
            "runtime-xcuitest-9-3",
            appium_version="2.19.0",
            driver_package="appium-xcuitest-driver",
            driver_version="9.3.1",
        ),
        _runtime(
            mac02,
            "runtime-uiautomator2-3-6",
            appium_version="2.11.5",
            driver_package="appium-uiautomator2-driver",
            driver_version="3.6.0",
        ),
        _runtime(
            mac02,
            "runtime-xcuitest-9-1",
            appium_version="2.18.0",
            driver_package="appium-xcuitest-driver",
            driver_version="9.1.0",
        ),
    ]
    session.add_all(runtime_rows)

    install_rows = [
        _pack_install(linux01, "appium-uiautomator2", "runtime-uiautomator2-3-6", "3.6.0", installed=True, ctx=ctx),
        _pack_install(linux01, "appium-roku-dlenroc", "runtime-roku-0-13", "0.13.3", installed=True, ctx=ctx),
        _pack_install(
            linux01,
            "appium-xcuitest",
            None,
            "9.3.1",
            installed=False,
            blocked_reason="Xcode is required for XCUITest runtimes",
            ctx=ctx,
        ),
        _pack_install(linux02, "appium-uiautomator2", "runtime-uiautomator2-3-5", "3.6.0", installed=True, ctx=ctx),
        _pack_install(
            linux02,
            "appium-roku-dlenroc",
            None,
            "0.13.3",
            installed=False,
            blocked_reason="Host is offline; Roku runtime install is pending",
            ctx=ctx,
        ),
        _pack_install(
            linux02,
            "appium-xcuitest",
            None,
            "9.3.1",
            installed=False,
            blocked_reason="Host is offline; last install attempt was never completed",
            ctx=ctx,
        ),
        _pack_install(mac01, "appium-uiautomator2", "runtime-uiautomator2-3-6", "3.6.0", installed=True, ctx=ctx),
        _pack_install(mac01, "appium-xcuitest", "runtime-xcuitest-9-3", "9.3.1", installed=True, ctx=ctx),
        _pack_install(mac02, "appium-uiautomator2", "runtime-uiautomator2-3-6", "3.6.0", installed=True, ctx=ctx),
        _pack_install(mac02, "appium-xcuitest", "runtime-xcuitest-9-1", "9.3.1", installed=True, ctx=ctx),
    ]
    session.add_all(install_rows)

    doctor_rows = [
        HostPackDoctorResult(
            host_id=linux01.id,
            pack_id="appium-uiautomator2",
            check_id="adb",
            ok=True,
            message="adb found at /usr/bin/adb",
        ),
        HostPackDoctorResult(
            host_id=linux01.id,
            pack_id="appium-uiautomator2",
            check_id="driver",
            ok=True,
            message="appium-uiautomator2-driver 3.6.0 installed",
        ),
        HostPackDoctorResult(
            host_id=linux01.id,
            pack_id="appium-roku-dlenroc",
            check_id="ecp",
            ok=True,
            message="Roku ECP endpoint reachable for demo devices",
        ),
        HostPackDoctorResult(
            host_id=linux01.id,
            pack_id="appium-roku-dlenroc",
            check_id="driver",
            ok=True,
            message="@dlenroc/appium-roku-driver 0.13.3 installed",
        ),
        HostPackDoctorResult(
            host_id=linux01.id,
            pack_id="appium-xcuitest",
            check_id="xcode",
            ok=False,
            message="Xcode not installed on Linux host",
        ),
        HostPackDoctorResult(
            host_id=mac01.id,
            pack_id="appium-xcuitest",
            check_id="xcode",
            ok=True,
            message="Xcode 15.2 available",
        ),
        HostPackDoctorResult(
            host_id=mac01.id,
            pack_id="appium-xcuitest",
            check_id="driver",
            ok=True,
            message="appium-xcuitest-driver 9.3.1 installed",
        ),
        HostPackDoctorResult(
            host_id=mac02.id,
            pack_id="appium-xcuitest",
            check_id="driver",
            ok=False,
            message="Installed 9.1.0, desired 9.3.1",
        ),
    ]
    session.add_all(doctor_rows)

    session.add_all(
        [
            HostPluginRuntimeStatus(
                host_id=linux01.id,
                runtime_id="runtime-uiautomator2-3-6",
                plugin_name="images",
                version="2.0.0",
                status="installed",
            ),
            HostPluginRuntimeStatus(
                host_id=mac02.id,
                runtime_id="runtime-xcuitest-9-1",
                plugin_name="images",
                version="1.4.0",
                status="blocked",
                blocked_reason="Plugin version does not satisfy desired 2.0.0",
            ),
        ]
    )


def _pack_install(
    host: Host,
    pack_id: str,
    runtime_id: str | None,
    desired_driver_version: str,
    *,
    installed: bool,
    ctx: SeedContext,
    blocked_reason: str | None = None,
) -> HostPackInstallation:
    return HostPackInstallation(
        host_id=host.id,
        pack_id=pack_id,
        pack_release="2026.04.0",
        runtime_id=runtime_id,
        status="installed" if installed else "blocked",
        resolved_install_spec={"appium_driver_version": desired_driver_version},
        installer_log_excerpt="installed from demo driver-pack cache" if installed else "install blocked",
        resolver_version="resolver-2026.04",
        blocked_reason=blocked_reason,
        installed_at=ctx.now - timedelta(hours=ctx.rng.randint(1, 48)) if installed else None,
    )


def _build_appium_nodes(
    ctx: SeedContext, devices: list[Device], hosts: list[Host], active_grid_run_ids: dict[object, uuid.UUID]
) -> None:
    """Seed Appium node rows only where the real start-node flow could do so."""
    host_by_id = {host.id: host for host in hosts}
    port = 4723
    nodes: list[AppiumNode] = []
    for device in devices:
        host = host_by_id[device.host_id]
        if host.status is not HostStatus.online:
            continue
        if device.verified_at is None:
            continue
        if not _has_started_node_setup(device):
            continue
        if device.operational_state is DeviceOperationalState.offline or device.hold is DeviceHold.maintenance:
            continue
        active_grid_run_id = active_grid_run_ids.get(device.id)
        nodes.append(
            AppiumNode(
                device_id=device.id,
                port=port,
                grid_url="http://selenium-hub:4444",
                pid=20_000 + port,
                active_connection_target=device.connection_target,
                desired_state=NodeState.running,
                desired_port=port,
                desired_grid_run_id=active_grid_run_id,
                grid_run_id=active_grid_run_id,
                started_at=ctx.now - timedelta(minutes=ctx.rng.randint(10, 240)),
            )
        )
        port += 1
        if len(nodes) >= 18:
            break

    stopped_candidates = [
        device
        for device in devices
        if host_by_id[device.host_id].status is HostStatus.online
        and device.operational_state is DeviceOperationalState.offline
    ]
    for device in stopped_candidates[:2]:
        nodes.append(
            AppiumNode(
                device_id=device.id,
                port=port,
                grid_url="http://selenium-hub:4444",
                pid=None,
                active_connection_target=None,
                desired_state=NodeState.stopped,
                desired_port=None,
                started_at=ctx.now - timedelta(hours=ctx.rng.randint(2, 12)),
            )
        )
        port += 1

    ctx.session.add_all(nodes)


def _has_started_node_setup(device: Device) -> bool:
    if device.pack_id == "appium-roku-dlenroc":
        return bool((device.device_config or {}).get("roku_password"))
    return True


def _build_operator_history(ctx: SeedContext, devices: list[Device], hosts: list[Host]) -> None:
    session = ctx.session
    config_targets = [devices[14], devices[20], devices[27]]
    config_payloads: list[dict[str, Any]] = [
        {"appium:noReset": True, "suite": "checkout-smoke"},
        {"wda_reuse": True},
        {"appium:systemPort": 8261, "network_profile": "office-wifi"},
    ]
    for index, (device, new_config) in enumerate(zip(config_targets, config_payloads, strict=True)):
        previous_config = dict(device.device_config or {})
        device.device_config = new_config
        device.verified_at = ctx.now - timedelta(minutes=20 - index)
        session.add(
            ConfigAuditLog(
                device_id=device.id,
                previous_config=previous_config,
                new_config=new_config,
                changed_by="demo-admin",
                changed_at=ctx.now - timedelta(days=7 - index, minutes=5),
            )
        )

    online_hosts = [host for host in hosts if host.status is HostStatus.online]
    terminal_rows = [
        HostTerminalSession(
            host_id=online_hosts[0].id,
            opened_by="demo-admin",
            opened_at=ctx.now - timedelta(days=2, minutes=12),
            closed_at=ctx.now - timedelta(days=2, minutes=4),
            close_reason="client_closed",
            client_ip="10.0.0.42",
            shell="/bin/zsh",
            agent_pid=43122,
        ),
        HostTerminalSession(
            host_id=online_hosts[1].id,
            opened_by="demo-operator",
            opened_at=ctx.now - timedelta(hours=9, minutes=30),
            closed_at=ctx.now - timedelta(hours=9, minutes=11),
            close_reason="agent_closed",
            client_ip="10.0.0.43",
            shell="/bin/bash",
            agent_pid=28645,
        ),
        HostTerminalSession(
            host_id=online_hosts[2].id,
            opened_by="demo-admin",
            opened_at=ctx.now - timedelta(hours=1, minutes=20),
            closed_at=ctx.now - timedelta(hours=1, minutes=17),
            close_reason="proxy_error",
            client_ip="10.0.0.42",
            shell="/bin/zsh",
            agent_pid=39211,
        ),
    ]
    session.add_all(terminal_rows)


# ---------------------------------------------------------------------------
# Telemetry builder
# ---------------------------------------------------------------------------


async def _build_telemetry(ctx: SeedContext, hosts: list[Host]) -> None:
    """Insert HostResourceSample + AnalyticsCapacitySnapshot rows."""
    session = ctx.session
    batch_size = 500

    # Host resource samples — batched to avoid memory spike
    for host in hosts:
        batch: list[object] = []
        for sample in host_resource_series(ctx, host_id=host.id, days_back=90):
            batch.append(sample)
            if len(batch) >= batch_size:
                session.add_all(batch)
                await session.flush()
                batch = []
        if batch:
            session.add_all(batch)
            await session.flush()

    # Capacity snapshots: hourly for 90 days
    now = ctx.now
    rng = ctx.rng
    snapshots = []
    ts = now - timedelta(days=90)
    while ts <= now:
        snapshots.append(
            make_capacity_snapshot(
                ctx,
                captured_at=ts,
                total_capacity_slots=35,
                active_sessions=rng.randint(0, 12),
                queued_requests=rng.randint(0, 5),
                hosts_total=4,
                hosts_online=rng.choice([3, 4]),
                devices_total=35,
                devices_available=rng.randint(15, 30),
            )
        )
        ts += timedelta(hours=1)
        if len(snapshots) >= batch_size:
            session.add_all(snapshots)
            await session.flush()
            snapshots = []
    if snapshots:
        session.add_all(snapshots)
        await session.flush()


# ---------------------------------------------------------------------------
# Main scenario entry point
# ---------------------------------------------------------------------------


async def apply_full_demo(ctx: SeedContext, *, skip_telemetry: bool = False) -> None:
    """Seed the full_demo scenario: 4 hosts, 35 devices, ~500 runs over 90 days."""
    session = ctx.session
    rng = ctx.rng

    await seed_demo_driver_packs(session)

    # ── 1. Settings ──────────────────────────────────────────────────────────
    settings = [
        make_setting(ctx, key="general.heartbeat_interval_sec", value=30, category="general"),
        make_setting(ctx, key="general.max_missed_heartbeats", value=10, category="general"),
        make_setting(ctx, key="general.node_check_interval_sec", value=30, category="general"),
        make_setting(ctx, key="general.session_viability_timeout_sec", value=15, category="general"),
        make_setting(ctx, key="grid.hub_url", value="http://selenium-hub:4444", category="grid"),
        make_setting(ctx, key="appium.default_plugins", value="images", category="grid"),
        make_setting(ctx, key="reservations.default_ttl_minutes", value=60, category="reservations"),
        make_setting(ctx, key="retention.sessions_days", value=90, category="retention"),
        make_setting(ctx, key="retention.device_events_days", value=90, category="retention"),
        make_setting(ctx, key="retention.capacity_snapshots_days", value=90, category="retention"),
    ]
    session.add_all(settings)

    # ── DeviceGroups ─────────────────────────────────────────────────────────
    groups = [
        make_device_group(ctx, name="smoke-core", description="Core smoke suite devices"),
        make_device_group(ctx, name="ios-fleet", description="All iOS devices", group_type=GroupType.dynamic),
        make_device_group(ctx, name="firetv-fleet", description="FireTV devices"),
        make_device_group(ctx, name="pilot", description="Pilot / experimental devices"),
    ]
    session.add_all(groups)
    await session.flush()

    # ── 2. Hosts ─────────────────────────────────────────────────────────────
    host_linux01 = make_host(
        ctx,
        hostname="lab-linux-01",
        ip="10.0.0.11",
        os_type=OSType.linux,
        status=HostStatus.online,
        agent_version="1.5.0",
        last_heartbeat_offset=timedelta(seconds=-8),
    )
    host_linux02 = make_host(
        ctx,
        hostname="lab-linux-02",
        ip="10.0.0.12",
        os_type=OSType.linux,
        # offline: heartbeat > 15 min stale
        status=HostStatus.offline,
        agent_version="1.4.2",
        last_heartbeat_offset=timedelta(minutes=-25),
    )
    host_mac01 = make_host(
        ctx,
        hostname="lab-mac-01",
        ip="10.0.0.21",
        os_type=OSType.macos,
        status=HostStatus.online,
        agent_version="1.5.0",
        last_heartbeat_offset=timedelta(seconds=-12),
    )
    host_mac02 = make_host(
        ctx,
        hostname="lab-mac-02",
        ip="10.0.0.22",
        os_type=OSType.macos,
        status=HostStatus.online,
        # one minor version behind to exercise version-mismatch UI
        agent_version="1.4.9",
        last_heartbeat_offset=timedelta(seconds=-6),
    )
    session.add_all([host_linux01, host_linux02, host_mac01, host_mac02])
    await session.flush()

    hosts = [host_linux01, host_linux02, host_mac01, host_mac02]
    _build_pack_runtime_status(ctx, hosts)

    # ── 3. Devices ────────────────────────────────────────────────────────────
    linux01_devices = _build_linux01_devices(ctx, host_linux01)  # 13 devices
    linux02_devices = _build_linux02_devices(ctx, host_linux02)  # 3 devices
    mac01_devices = _build_mac01_devices(ctx, host_mac01)  # 9 devices
    mac02_devices = _build_mac02_devices(ctx, host_mac02)  # 8 devices
    roku_devices = _build_roku_devices(ctx, host_linux01)  # 2 devices
    all_devices = linux01_devices + linux02_devices + mac01_devices + mac02_devices + roku_devices
    # Total: 35

    _apply_device_config_defaults(all_devices)
    session.add_all(all_devices)
    await session.flush()

    # Apply special states (verified_at=None, maintenance, offline, reserved, excluded, flapping)
    flapping_device, reserved_device, excluded_device = _apply_special_device_states(ctx, all_devices)
    _build_operator_history(ctx, all_devices, hosts)

    # ── 4. DeviceGroupMembership ─────────────────────────────────────────────
    smoke_core_group = groups[0]
    ios_fleet_group = groups[1]
    firetv_fleet_group = groups[2]
    pilot_group = groups[3]

    ios_devices = [d for d in all_devices if d.platform_id in IOS_PLATFORM_IDS]
    firetv_devices = [d for d in all_devices if d.platform_id in FIRETV_PLATFORM_IDS]

    memberships = []
    # smoke-core: first 5 android_mobile
    android_mobile_devices = [d for d in all_devices if d.platform_id in ANDROID_MOBILE_PLATFORM_IDS]
    for d in android_mobile_devices[:5]:
        memberships.append(DeviceGroupMembership(group_id=smoke_core_group.id, device_id=d.id))
    # ios-fleet: all ios
    for d in ios_devices:
        memberships.append(DeviceGroupMembership(group_id=ios_fleet_group.id, device_id=d.id))
    # firetv-fleet: all firetv
    for d in firetv_devices:
        memberships.append(DeviceGroupMembership(group_id=firetv_fleet_group.id, device_id=d.id))
    # pilot: flapping device + 2 others
    for d in [flapping_device, all_devices[20], all_devices[25]]:
        memberships.append(DeviceGroupMembership(group_id=pilot_group.id, device_id=d.id))

    session.add_all(memberships)

    # ── 5. SystemEvents ───────────────────────────────────────────────────────
    sys_events = _build_system_events(ctx)
    session.add_all(sys_events)
    await session.flush()  # need IDs for webhook deliveries

    # ── 6. Webhooks + WebhookDelivery ─────────────────────────────────────────
    webhook_slack = make_webhook(
        ctx,
        name="slack_alerts",
        url="https://hooks.slack.com/services/DEMO/SLACK/HOOK",
        event_types=["run.failed", "host.offline", "lifecycle.incident_open"],
        enabled=True,
    )
    webhook_legacy = make_webhook(
        ctx,
        name="legacy_generic",
        url="https://legacy.example.com/webhook",
        event_types=["run.completed", "run.failed"],
        enabled=False,
    )
    session.add_all([webhook_slack, webhook_legacy])
    await session.flush()

    # ~40 deliveries: 70% delivered, 20% failed, 10% exhausted
    delivery_statuses = ["delivered"] * 28 + ["failed"] * 8 + ["exhausted"] * 4
    rng.shuffle(delivery_statuses)

    deliveries = []
    sampled_events = rng.sample(sys_events, min(40, len(sys_events)))
    for i, (evt, dstatus) in enumerate(zip(sampled_events, delivery_statuses, strict=False)):
        webhook = webhook_slack if i % 2 == 0 else webhook_legacy
        last_http = 200 if dstatus == "delivered" else rng.choice([500, 503, 429])
        deliveries.append(
            make_webhook_delivery(
                ctx,
                webhook_id=webhook.id,
                system_event_id=evt.id,
                event_type=evt.type,
                status=dstatus,
                attempts=1 if dstatus == "delivered" else 3,
                max_attempts=3,
                last_http_status=last_http,
                last_error=None if dstatus == "delivered" else "Connection refused",
            )
        )
    session.add_all(deliveries)

    # ── 7. TestRuns + Reservations + Sessions ─────────────────────────────────
    runs, active_run_devices = _build_runs(ctx, all_devices, reserved_device, excluded_device)
    session.add_all(runs)
    await session.flush()

    active_grid_run_ids = _build_reservations_and_sessions(
        ctx, runs, all_devices, active_run_devices, reserved_device, excluded_device
    )
    _build_appium_nodes(ctx, all_devices, hosts, active_grid_run_ids)

    # ── 8. DeviceEvents ───────────────────────────────────────────────────────
    _build_device_events(ctx, all_devices, flapping_device)

    # ── 9. Jobs ───────────────────────────────────────────────────────────────
    _build_jobs(ctx)

    # ── 10. Telemetry (optional) ──────────────────────────────────────────────
    if not skip_telemetry:
        await _build_telemetry(ctx, hosts)
