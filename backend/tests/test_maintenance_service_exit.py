"""D3: exit_maintenance must enqueue a recovery job."""

import logging
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.device import DeviceHold, DeviceOperationalState
from app.models.host import Host
from app.models.job import Job
from app.services import device_locking, maintenance_service
from app.services.job_kind_constants import JOB_KIND_DEVICE_RECOVERY
from tests.helpers import create_device

pytestmark = pytest.mark.asyncio


async def test_exit_maintenance_enqueues_recovery_job(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="exit-enqueues-job",
        hold=DeviceHold.maintenance,
        operational_state=DeviceOperationalState.offline,
    )

    locked = await device_locking.lock_device(db_session, device.id)
    await maintenance_service.exit_maintenance(db_session, locked)

    rows = (await db_session.execute(select(Job).where(Job.kind == JOB_KIND_DEVICE_RECOVERY))).scalars().all()
    assert len(rows) == 1
    payload = rows[0].payload
    assert payload["device_id"] == str(device.id)
    assert payload["source"] == "exit_maintenance"


async def test_exit_maintenance_enqueue_failure_does_not_propagate(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    """Regression: exit_maintenance must not raise when schedule_device_recovery fails.

    Before the fix, exit_maintenance(commit=True) committed the device-state
    mutation and THEN called schedule_device_recovery. If create_job raised,
    the exception propagated — the operator got a 500 while the device was
    already out of maintenance (state mutation committed) but had no recovery
    job scheduled. The device was stranded until the next
    device_connectivity_loop tick.

    After the fix, the exception is swallowed with a WARNING log, and the
    committed device state mutation is preserved.

    NOTE: Capture the warning via a handler attached directly to the
    maintenance_service logger instead of pytest's ``caplog`` fixture. Under
    ``pytest -n auto`` on CI, another test in the same xdist worker mutates
    ``root_logger.handlers`` while ``configure_logging`` runs, which has
    intermittently left ``caplog`` unable to see records propagated from
    ``app.services.maintenance_service``. A direct handler is independent of
    propagation and root state and produces a deterministic capture.
    """
    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="exit-enqueue-fail",
        hold=DeviceHold.maintenance,
        operational_state=DeviceOperationalState.offline,
    )

    locked = await device_locking.lock_device(db_session, device.id)

    target_logger = logging.getLogger("app.services.maintenance_service")
    captured: list[logging.LogRecord] = []

    class _ListHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(record)

    capture_handler = _ListHandler(level=logging.WARNING)
    original_level = target_logger.level
    target_logger.setLevel(logging.WARNING)
    target_logger.addHandler(capture_handler)
    mock_schedule = AsyncMock(side_effect=RuntimeError("simulated transient DB error"))
    try:
        with patch("app.services.maintenance_service.schedule_device_recovery", new=mock_schedule):
            # Must NOT raise even though schedule_device_recovery raises.
            result = await maintenance_service.exit_maintenance(db_session, locked)
    finally:
        target_logger.removeHandler(capture_handler)
        target_logger.setLevel(original_level)

    # Sanity: the patched mock actually intercepted the call. If this fires,
    # the warning-records assertion below would also fail but for a different
    # reason — fail loudly here so the cause is unambiguous.
    assert mock_schedule.await_count == 1, "schedule_device_recovery patch did not intercept the call"

    # State mutation must be committed regardless of enqueue failure.
    assert result.hold is None, "hold must be cleared (committed) even when enqueue fails"
    assert result.operational_state == DeviceOperationalState.offline, (
        "operational_state must remain offline after exit_maintenance"
    )

    # A warning must have been logged so ops can triage.
    warning_records = [r for r in captured if r.levelno >= logging.WARNING]
    assert any("exit_maintenance" in r.getMessage() for r in warning_records), (
        "exit_maintenance must log a WARNING when recovery enqueue fails"
    )
