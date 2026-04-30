"""Session factory."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from app.models.session import Session, SessionStatus

if TYPE_CHECKING:
    from app.models.device import Device
    from app.models.test_run import TestRun
    from app.seeding.context import SeedContext


def make_session(
    ctx: SeedContext,
    *,
    run: TestRun,
    device: Device,
    status: SessionStatus,
    started_at: datetime,
    duration_seconds: float | None,
    test_name: str | None = None,
    error_type: str | None = None,
    error_message: str | None = None,
) -> Session:
    session = Session(
        session_id=str(uuid.uuid4()),
        device_id=device.id,
        run_id=run.id,
        test_name=test_name or f"test_{ctx.rng.randrange(1_000_000):06d}",
        status=status,
        started_at=started_at,
        requested_pack_id=device.pack_id,
        requested_platform_id=device.platform_id,
        requested_device_type=device.device_type,
        requested_connection_type=device.connection_type,
        requested_capabilities={"deviceName": device.name},
        error_type=error_type,
        error_message=error_message,
    )
    if duration_seconds is not None:
        session.ended_at = started_at + timedelta(seconds=duration_seconds)
    return session
