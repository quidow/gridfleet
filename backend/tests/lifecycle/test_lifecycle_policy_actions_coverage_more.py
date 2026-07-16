from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import event

from app.appium_nodes.models import AppiumDesiredState, AppiumNode
from app.devices import locking as device_locking
from app.devices.models import Device, DeviceEventType, DeviceOperationalState
from app.devices.services.intent import IntentService
from app.lifecycle.services import actions, remediation_log
from app.lifecycle.services.actions import (
    LifecyclePolicyActionsService,
    escalate_device_remediation_failure,
    reset_reconciler_start_failure_if_needed,
)
from app.lifecycle.services.incidents import LifecycleIncidentService
from app.runs.models import RunState
from app.runs.service_reservation import RunReservationService
from tests.fakes import FakeSettingsReader, build_review_service
from tests.helpers import create_device, create_reserved_run

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.hosts.models import Host


def test_lifecycle_policy_action_small_branch_helpers() -> None:
    assert actions.failure_event_type("connectivity") == DeviceEventType.connectivity_lost
    assert remediation_log.ACTION_AUTO_STOP_COMMISSIONED == "auto_stop_commissioned"


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
async def test_exclude_run_if_needed_locked_reuses_proof_without_commit(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="locked-run-exclusion",
        operational_state=DeviceOperationalState.available,
    )
    await create_reserved_run(db_session, name="locked-run", devices=[device])
    await db_session.commit()
    locked = await device_locking.lock_device_handle(db_session, device.id)
    lock_spy = AsyncMock(wraps=device_locking.lock_device)
    reconcile_locked = AsyncMock()
    reconcile_now = AsyncMock()
    monkeypatch.setattr(device_locking, "lock_device", lock_spy)
    monkeypatch.setattr(IntentService, "reconcile_locked", reconcile_locked)
    monkeypatch.setattr(IntentService, "reconcile_now", reconcile_now)
    commits = 0

    def count_commit(_session: object) -> None:
        nonlocal commits
        commits += 1

    event.listen(db_session.sync_session, "after_commit", count_commit)
    try:
        run, entry = await LifecyclePolicyActionsService(
            publisher=Mock(),
            reservation=RunReservationService(review=build_review_service()),
            incidents=LifecycleIncidentService(),
        ).exclude_run_if_needed_locked(
            db_session,
            locked,
            reason="health failed",
            source="device_checks",
        )

        assert run is not None
        assert entry is not None and entry.excluded is True
        locked.assert_active(db_session)
        lock_spy.assert_not_awaited()
        reconcile_locked.assert_awaited_once()
        reconcile_now.assert_not_awaited()
        assert commits == 0
    finally:
        event.remove(db_session.sync_session, "after_commit", count_commit)


@pytest.mark.db
async def test_handle_node_crash_locked_reuses_proof_without_commit(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="locked-node-crash",
        operational_state=DeviceOperationalState.available,
        device_checks_healthy=False,
    )
    node = AppiumNode(
        device_id=device.id,
        desired_state=AppiumDesiredState.running,
        desired_port=4723,
        port=4723,
        pid=123,
        active_connection_target=device.identity_value,
    )
    db_session.add(node)
    await db_session.commit()
    locked = await device_locking.lock_device_handle(db_session, device.id)
    lock_spy = AsyncMock(wraps=device_locking.lock_device)
    reconcile_locked = AsyncMock()
    reconcile_now = AsyncMock()
    monkeypatch.setattr(device_locking, "lock_device", lock_spy)
    monkeypatch.setattr(IntentService, "reconcile_locked", reconcile_locked)
    monkeypatch.setattr(IntentService, "reconcile_now", reconcile_now)
    commits = 0

    def count_commit(_session: object) -> None:
        nonlocal commits
        commits += 1

    event.listen(db_session.sync_session, "after_commit", count_commit)
    try:
        await LifecyclePolicyActionsService(
            publisher=Mock(),
            reservation=RunReservationService(review=build_review_service()),
            incidents=LifecycleIncidentService(),
        ).handle_node_crash_locked(
            db_session,
            locked,
            source="device_checks",
            reason="health failed",
        )

        locked.assert_active(db_session)
        lock_spy.assert_not_awaited()
        reconcile_locked.assert_awaited_once()
        reconcile_now.assert_not_awaited()
        assert commits == 0
    finally:
        event.remove(db_session.sync_session, "after_commit", count_commit)


@pytest.mark.db
async def test_reset_start_failure_keeps_recovery_sourced_backoff(db_session: AsyncSession, db_host: Host) -> None:
    """A successful node start must not wipe backoff recorded by a failed recovery probe."""
    device = await create_device(db_session, host_id=db_host.id, name="keep-recovery-sourced-backoff")
    locked = await device_locking.lock_device(db_session, device.id)
    settings = FakeSettingsReader(
        {
            "general.lifecycle_recovery_backoff_base_sec": 60,
            "general.lifecycle_recovery_backoff_max_sec": 900,
        }
    )
    await remediation_log.append_attempt(
        db_session, locked.id, source="session_viability", reason="Recovery probe failed", settings=settings
    )
    await remediation_log.append_attempt(
        db_session, locked.id, source="session_viability", reason="Recovery probe failed", settings=settings
    )
    await db_session.commit()

    locked = await device_locking.lock_device(db_session, device.id)
    assert await reset_reconciler_start_failure_if_needed(db_session, locked) is False
    after = await remediation_log.load_ladder(db_session, locked.id)
    assert after.attempts == 2
    assert after.backoff_until is not None
    assert after.last_failure_source == "session_viability"


@pytest.mark.db
async def test_reset_start_failure_clears_reconciler_sourced_residue(db_session: AsyncSession, db_host: Host) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="clear-reconciler-sourced-residue")
    locked = await device_locking.lock_device(db_session, device.id)
    settings = FakeSettingsReader(
        {
            "general.lifecycle_recovery_backoff_base_sec": 60,
            "general.lifecycle_recovery_backoff_max_sec": 900,
        }
    )
    await remediation_log.append_attempt(
        db_session, locked.id, source="appium_reconciler", reason="port_conflict", settings=settings
    )
    await remediation_log.append_attempt(
        db_session, locked.id, source="appium_reconciler", reason="port_conflict", settings=settings
    )
    await db_session.commit()

    locked = await device_locking.lock_device(db_session, device.id)
    assert await reset_reconciler_start_failure_if_needed(db_session, locked) is True
    after = await remediation_log.load_ladder(db_session, locked.id)
    assert after.attempts == 0
    assert after.backoff_until is None
    assert after.last_failure_source is None


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
    after = await remediation_log.load_ladder(db_session, locked.id)
    assert after.backoff_until is not None
    assert after.last_failure_source == "appium_reconciler"

    locked = await device_locking.lock_device(db_session, device.id)
    second = await escalate_device_remediation_failure(
        db_session, locked, settings=settings, source="appium_reconciler", reason="spawn_failed"
    )
    await db_session.commit()
    assert second.attempts == 2 and second.shelved is True
    refreshed = await db_session.get(Device, device.id)
    assert refreshed is not None and refreshed.review_required is True
