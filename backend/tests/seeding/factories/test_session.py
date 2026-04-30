from __future__ import annotations

import uuid
from datetime import timedelta

from app.models.device import (
    ConnectionType,
    Device,
    DeviceType,
)
from app.models.session import SessionStatus
from app.models.test_run import RunState, TestRun
from app.seeding.context import SeedContext
from app.seeding.factories.session import make_session


def _fake_device() -> Device:
    return Device(
        id=uuid.uuid4(),
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value="S1",
        connection_target="S1",
        name="d",
        os_version="14",
        host_id=uuid.uuid4(),
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )


def _fake_run(state: RunState, started_at) -> TestRun:  # noqa: ANN001
    return TestRun(
        id=uuid.uuid4(),
        name="r",
        state=state,
        requirements=[{"platform_id": "android_mobile", "count": 1}],
        ttl_minutes=60,
        heartbeat_timeout_sec=120,
        started_at=started_at,
    )


def test_make_terminal_session_has_ended_at() -> None:
    ctx = SeedContext.build(session=None, seed=42)  # type: ignore[arg-type]
    run = _fake_run(RunState.completed, ctx.now - timedelta(hours=2))
    device = _fake_device()
    session = make_session(
        ctx,
        run=run,
        device=device,
        status=SessionStatus.passed,
        started_at=run.started_at,
        duration_seconds=300.0,
    )
    assert session.status is SessionStatus.passed
    assert session.ended_at == run.started_at + timedelta(seconds=300)


def test_make_active_session_has_no_ended_at() -> None:
    ctx = SeedContext.build(session=None, seed=1)  # type: ignore[arg-type]
    run = _fake_run(RunState.active, ctx.now - timedelta(minutes=3))
    device = _fake_device()
    session = make_session(
        ctx,
        run=run,
        device=device,
        status=SessionStatus.running,
        started_at=run.started_at,
        duration_seconds=None,
    )
    assert session.ended_at is None
