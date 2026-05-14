import uuid
from datetime import timedelta

from app.devices.models import ConnectionType, Device, DeviceType
from app.runs.models import RunState
from app.seeding.factories.run import make_reservation, make_run
from tests.seeding.helpers import build_test_seed_context


def _fake_device() -> Device:
    return Device(
        id=uuid.uuid4(),
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value="SERIAL1",
        connection_target="SERIAL1",
        name="Pixel",
        os_version="14",
        host_id=uuid.uuid4(),
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )


def test_make_run_completed_sets_completed_at() -> None:
    ctx = build_test_seed_context(seed=42)
    started = ctx.now - timedelta(hours=6)
    run = make_run(
        ctx,
        name="nightly #42",
        state=RunState.completed,
        started_at=started,
        duration_seconds=600.0,
        requirements=[{"platform_id": "android_mobile", "count": 2}],
    )
    assert run.state is RunState.completed
    assert run.started_at == started
    assert run.completed_at == started + timedelta(seconds=600)


def test_make_run_active_has_no_completed_at() -> None:
    ctx = build_test_seed_context(seed=1)
    run = make_run(
        ctx,
        name="live",
        state=RunState.active,
        started_at=ctx.now - timedelta(minutes=5),
        duration_seconds=None,
        requirements=[{"platform_id": "ios", "count": 1}],
    )
    assert run.completed_at is None


def test_make_reservation_terminal_run_has_released_at() -> None:
    ctx = build_test_seed_context(seed=1)
    run = make_run(
        ctx,
        name="r",
        state=RunState.completed,
        started_at=ctx.now - timedelta(hours=2),
        duration_seconds=1800,
        requirements=[{"platform_id": "android_mobile", "count": 1}],
    )
    device = _fake_device()
    reservation = make_reservation(ctx, run=run, device=device, released=True)
    assert reservation.released_at == run.completed_at


def test_make_reservation_active_run_has_no_released_at() -> None:
    ctx = build_test_seed_context(seed=1)
    run = make_run(
        ctx,
        name="r",
        state=RunState.active,
        started_at=ctx.now - timedelta(minutes=3),
        duration_seconds=None,
        requirements=[{"platform_id": "android_mobile", "count": 1}],
    )
    device = _fake_device()
    reservation = make_reservation(ctx, run=run, device=device, released=False)
    assert reservation.released_at is None
