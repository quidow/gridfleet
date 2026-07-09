from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock

import pytest

from app.devices import locking as device_locking
from app.devices.models import Device, DeviceEventType
from app.devices.services.lifecycle_policy_state import now, write_state
from app.devices.services.lifecycle_policy_state import state as policy_state
from app.lifecycle.services import actions
from app.lifecycle.services.actions import (
    LifecyclePolicyActionsService,
    escalate_device_remediation_failure,
    reset_reconciler_start_failure_state,
)
from app.lifecycle.services.incidents import LifecycleIncidentService
from app.runs.models import RunState
from app.runs.service_reservation import RunReservationService
from tests.fakes import FakeSettingsReader, build_review_service
from tests.helpers import create_device

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.hosts.models import Host


def test_lifecycle_policy_action_small_branch_helpers() -> None:
    assert actions.failure_event_type("connectivity") == DeviceEventType.connectivity_lost

    device = Device(id=__import__("uuid").uuid4())
    intents = actions._crash_intents(device)
    assert intents[0].source == f"health_failure:node:{device.id}"
    assert intents[0].payload["stop_mode"] == "graceful"


async def test_restore_run_if_needed_early_return_branches() -> None:
    svc = LifecyclePolicyActionsService(
        publisher=Mock(),
        reservation=RunReservationService(review=build_review_service()),
        incidents=LifecycleIncidentService(),
    )
    run = SimpleNamespace(state=RunState.completed)
    assert await svc.restore_run_if_needed(AsyncMock(), SimpleNamespace(), run, None, reason="r", source="s") == (
        run,
        None,
    )


@pytest.mark.db
async def test_reset_start_failure_keeps_recovery_sourced_backoff(db_session: AsyncSession, db_host: Host) -> None:
    """A successful node start must not wipe backoff recorded by a failed recovery probe."""
    device = await create_device(db_session, host_id=db_host.id, name="keep-recovery-sourced-backoff")
    locked = await device_locking.lock_device(db_session, device.id)
    state = policy_state(locked)
    state["last_failure_source"] = "session_viability"
    state["last_failure_reason"] = "Recovery probe failed"
    state["recovery_backoff_attempts"] = 2
    state["backoff_until"] = (now() + timedelta(seconds=600)).isoformat()
    write_state(locked, state)
    await db_session.commit()

    locked = await device_locking.lock_device(db_session, device.id)
    reset_reconciler_start_failure_state(locked)
    after = policy_state(locked)
    assert after["recovery_backoff_attempts"] == 2
    assert after["backoff_until"] is not None
    assert after["last_failure_source"] == "session_viability"


@pytest.mark.db
async def test_reset_start_failure_clears_reconciler_sourced_residue(db_session: AsyncSession, db_host: Host) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="clear-reconciler-sourced-residue")
    locked = await device_locking.lock_device(db_session, device.id)
    state = policy_state(locked)
    state["last_failure_source"] = "appium_reconciler"
    state["last_failure_reason"] = "port_conflict"
    state["recovery_backoff_attempts"] = 3
    state["backoff_until"] = (now() + timedelta(seconds=600)).isoformat()
    write_state(locked, state)
    await db_session.commit()

    locked = await device_locking.lock_device(db_session, device.id)
    reset_reconciler_start_failure_state(locked)
    after = policy_state(locked)
    assert after["recovery_backoff_attempts"] == 0
    assert after["backoff_until"] is None
    assert after["last_failure_source"] is None


@pytest.mark.db
async def test_escalate_device_remediation_failure_backs_off_and_shelves(
    db_session: AsyncSession, db_host: Host
) -> None:
    settings = FakeSettingsReader(
        {
            "general.lifecycle_recovery_backoff_base_sec": 60,
            "general.lifecycle_recovery_backoff_max_sec": 900,
            "general.lifecycle_recovery_review_threshold": 2,
        }
    )
    device = await create_device(db_session, host_id=db_host.id, name="escalate-device-remediation-failure")

    locked = await device_locking.lock_device(db_session, device.id)
    first = await escalate_device_remediation_failure(
        db_session, locked, settings=settings, source="appium_reconciler", reason="spawn_failed"
    )
    await db_session.commit()
    assert first.attempts == 1 and first.shelved is False
    after = policy_state(locked)
    assert after["backoff_until"] is not None
    assert after["last_failure_source"] == "appium_reconciler"

    locked = await device_locking.lock_device(db_session, device.id)
    second = await escalate_device_remediation_failure(
        db_session, locked, settings=settings, source="appium_reconciler", reason="spawn_failed"
    )
    await db_session.commit()
    assert second.attempts == 2 and second.shelved is True
    refreshed = await db_session.get(Device, device.id)
    assert refreshed is not None and refreshed.review_required is True
