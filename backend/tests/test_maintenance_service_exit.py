"""D3: exit_maintenance must enqueue a recovery job."""

from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.devices import locking as device_locking
from app.devices.models import DeviceHold, DeviceOperationalState
from app.devices.services import maintenance as maintenance_service
from app.hosts.models import Host
from app.jobs.kinds import JOB_KIND_DEVICE_RECOVERY
from app.jobs.models import Job
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

    NOTE: spy on ``logger.warning`` directly instead of going through
    ``caplog`` or a handler attached to the maintenance_service logger.
    Both of those routes go through stdlib logging filtering
    (``Logger.isEnabledFor``, ``Logger.disabled``, parent-logger state)
    and other tests running in the same xdist worker can leave that state
    in a configuration where the WARNING record never reaches handlers —
    which has produced a flake on CI. Spying on the call site bypasses the
    pipeline entirely and verifies the contract directly.
    """
    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="exit-enqueue-fail",
        hold=DeviceHold.maintenance,
        operational_state=DeviceOperationalState.offline,
    )

    locked = await device_locking.lock_device(db_session, device.id)

    mock_schedule = AsyncMock(side_effect=RuntimeError("simulated transient DB error"))
    with (
        patch("app.devices.services.maintenance.schedule_device_recovery", new=mock_schedule),
        patch.object(maintenance_service.logger, "warning") as warning_spy,
    ):
        # Must NOT raise even though schedule_device_recovery raises.
        result = await maintenance_service.exit_maintenance(db_session, locked)

    # Sanity: the patched mock actually intercepted the call. If this fires,
    # the warning-call assertion below would also fail but for a different
    # reason — fail loudly here so the cause is unambiguous.
    assert mock_schedule.await_count == 1, "schedule_device_recovery patch did not intercept the call"

    # State mutation must be committed regardless of enqueue failure.
    assert result.hold is None, "hold must be cleared (committed) even when enqueue fails"
    assert result.operational_state == DeviceOperationalState.offline, (
        "operational_state must remain offline after exit_maintenance"
    )

    # A warning must have been logged so ops can triage.
    assert warning_spy.called, "exit_maintenance must call logger.warning when recovery enqueue fails"
    warning_args, _ = warning_spy.call_args
    assert "exit_maintenance" in warning_args[0], (
        f"warning message must mention exit_maintenance (got: {warning_args[0]!r})"
    )
