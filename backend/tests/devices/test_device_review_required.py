"""Behaviour of the ``Device.review_required`` shelving flag.

Once a device has been promoted into this state, automated recovery loops
must skip it; only sanctioned operator actions clear it back.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock

import pytest

from app.devices.models import ConnectionType, Device, DeviceOperationalState, DeviceType
from app.devices.services.maintenance import MaintenanceService
from app.lifecycle.services import policy as lifecycle_policy_module
from app.lifecycle.services import remediation_log
from app.lifecycle.services.actions import LifecyclePolicyActionsService
from app.lifecycle.services.incidents import LifecycleIncidentService
from app.lifecycle.services.policy import LifecyclePolicyService
from app.runs.service_reservation import RunReservationService
from tests.fakes import FakeSettingsReader, build_review_service
from tests.helpers import create_device, create_reserved_run
from tests.helpers import test_event_bus as event_bus

pytestmark = pytest.mark.usefixtures("seeded_driver_packs")

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.hosts.models import Host


async def test_mark_and_clear_review_required(db_session: AsyncSession, db_host: Host) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="review-toggle")

    set_result = await build_review_service().mark_review_required(
        db_session, device, reason="probe failed too many times", source="session_viability"
    )
    await db_session.commit()
    await db_session.refresh(device)
    assert set_result is True
    assert device.review_required is True
    assert device.review_reason == "probe failed too many times"
    assert device.review_set_at is not None

    cleared = await build_review_service().clear_review_required(
        db_session, device, reason="operator action", source="operator"
    )
    await db_session.commit()
    await db_session.refresh(device)
    assert cleared is True
    assert device.review_required is False
    assert device.review_reason is None
    assert device.review_set_at is None


async def test_mark_review_required_is_idempotent(db_session: AsyncSession, db_host: Host) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="review-idempotent")
    await build_review_service().mark_review_required(db_session, device, reason="initial", source="session_viability")
    await db_session.commit()

    second = await build_review_service().mark_review_required(
        db_session, device, reason="initial", source="session_viability"
    )
    assert second is False


async def test_mark_review_required_audits_reason_updates(db_session: AsyncSession, db_host: Host) -> None:
    """A reason change on an already-flagged device must record an audit
    event so the operator-visible history is not silently rewritten."""
    from sqlalchemy import select

    from app.devices.models import DeviceEvent, DeviceEventType

    device = await create_device(db_session, host_id=db_host.id, name="review-reason-audit")
    await build_review_service().mark_review_required(
        db_session, device, reason="first reason", source="session_viability"
    )
    await db_session.commit()

    result_changed = await build_review_service().mark_review_required(
        db_session, device, reason="second reason", source="session_viability"
    )
    await db_session.commit()
    await db_session.refresh(device)

    assert result_changed is False  # still already-flagged semantics
    assert device.review_reason == "second reason"

    events = (
        (
            await db_session.execute(
                select(DeviceEvent)
                .where(DeviceEvent.device_id == device.id)
                .where(DeviceEvent.event_type == DeviceEventType.lifecycle_recovery_suppressed)
                .order_by(DeviceEvent.created_at.asc())
            )
        )
        .scalars()
        .all()
    )
    assert len(events) == 2  # initial set + reason update
    update_event = events[-1]
    assert update_event.details.get("reason_updated") is True
    assert update_event.details.get("previous_reason") == "first reason"
    assert update_event.details.get("review_reason") == "second reason"


async def test_exit_maintenance_clears_review_required(db_session: AsyncSession, db_host: Host) -> None:
    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="review-cleared-on-exit",
        operational_state=DeviceOperationalState.maintenance,
        lifecycle_policy_state={"maintenance_reason": "Operator entered maintenance"},
    )
    await build_review_service().mark_review_required(db_session, device, reason="stuck", source="session_viability")
    await db_session.commit()

    await MaintenanceService(
        review=build_review_service(), settings=FakeSettingsReader({}), publisher=event_bus
    ).exit_maintenance(db_session, device)
    await db_session.refresh(device)
    assert device.review_required is False
    assert device.review_reason is None


async def test_enter_maintenance_keeps_review_required(db_session: AsyncSession, db_host: Host) -> None:
    """Entering maintenance does NOT clear the flag — it is a separate signal.
    Only the exit transition (operator promise that the device is ready again)
    clears it.
    """
    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="review-survives-enter",
        operational_state=DeviceOperationalState.available,
    )
    await build_review_service().mark_review_required(db_session, device, reason="stuck", source="session_viability")
    await db_session.commit()

    await MaintenanceService(
        review=build_review_service(), settings=FakeSettingsReader({}), publisher=event_bus
    ).enter_maintenance(db_session, device)
    await db_session.refresh(device)
    assert device.review_required is True


async def test_restore_device_to_run_clears_review_required(db_session: AsyncSession, db_host: Host) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="review-cleared-on-restore")
    await create_reserved_run(db_session, name="run-for-restore", devices=[device])

    # Seed a hard exclusion (post-escalation shape) and the review flag.
    from sqlalchemy import select

    from app.devices.models import DeviceReservation

    reservation = (
        await db_session.execute(select(DeviceReservation).where(DeviceReservation.device_id == device.id))
    ).scalar_one()
    from datetime import UTC, datetime, timedelta

    reservation.excluded = True
    reservation.exclusion_reason = "escalated"
    reservation.excluded_at = datetime.now(UTC) - timedelta(minutes=5)
    reservation.excluded_until = None
    reservation.cooldown_count = 3
    await build_review_service().mark_review_required(db_session, device, reason="stuck", source="session_viability")
    await db_session.commit()

    # restore_device_to_run is transaction-local now (no internal commit); the
    # caller owns the boundary, so commit before re-reading the review flag.
    await RunReservationService(review=build_review_service()).restore_device_to_run(db_session, device.id)
    await db_session.commit()
    await db_session.refresh(device)
    assert device.review_required is False
    assert device.review_reason is None


@pytest.fixture
def _speed_up_recovery_probe_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lifecycle_policy_module, "RECOVERY_PROBE_RETRY_DELAY_SEC", 0, raising=False)
    monkeypatch.setattr(lifecycle_policy_module, "RECOVERY_NODE_START_WAIT_TIMEOUT_SEC", 0, raising=False)


def _settings_stub(review_threshold: int) -> dict[str, object]:
    return {
        "general.lifecycle_recovery_backoff_base_sec": 5,
        "general.lifecycle_recovery_backoff_max_sec": 20,
        "general.lifecycle_recovery_review_threshold": review_threshold,
        "appium.port_range_start": 4720,
        "appium.port_range_end": 4800,
    }


def _failing_probe() -> AsyncMock:
    return AsyncMock(
        return_value={
            "status": "failed",
            "last_attempted_at": datetime.now(UTC).isoformat(),
            "last_succeeded_at": None,
            "error": "Probe failed",
            "checked_by": "recovery",
            "consecutive_failures": 1,
        }
    )


async def _mark_device_available(
    db: object,
    *,
    device_id: object,
    intents: object,
    **kwargs: object,
) -> None:
    del intents, kwargs
    from app.devices.models import Device as _Device

    device = await db.get(_Device, device_id)  # type: ignore[attr-defined]
    assert device is not None
    device.operational_state_last_emitted = DeviceOperationalState.available


async def _make_offline_verified_device(db_session: AsyncSession, db_host: Host, name: str) -> Device:
    device = Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value=name,
        connection_target=name,
        name=name,
        os_version="14",
        host_id=db_host.id,
        operational_state=DeviceOperationalState.offline,
        verified_at=datetime.now(UTC),
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )
    db_session.add(device)
    await db_session.commit()
    return device


async def test_attempt_auto_recovery_promotes_to_review_after_threshold(
    db_session: AsyncSession,
    db_host: Host,
    _speed_up_recovery_probe_retries: None,
) -> None:
    """Drive the recovery finalize past the configured threshold and verify the
    device is shelved into ``review_required``. Below the threshold the
    counter accumulates without shelving; crossing it shelves."""
    from app.devices import locking as device_locking
    from app.devices.services.decision_snapshot import load_device_decision_snapshot
    from app.devices.services.lifecycle_policy_state import set_recovery_generation

    threshold = 2
    device = await _make_offline_verified_device(db_session, db_host, "review-promotion")

    settings = FakeSettingsReader(
        {
            **_settings_stub(threshold),
            "general.lifecycle_recovery_backoff_base_sec": 0,
            "general.lifecycle_recovery_backoff_max_sec": 0,
        }
    )
    svc = LifecyclePolicyService(
        review=build_review_service(),
        publisher=Mock(),
        settings=settings,
        actions=LifecyclePolicyActionsService(
            publisher=Mock(),
            reservation=RunReservationService(review=build_review_service()),
            incidents=LifecycleIncidentService(),
        ),
        incidents=LifecycleIncidentService(),
        viability=AsyncMock(),
        node_manager=AsyncMock(),
    )
    # Attempt #1 — first probe failure. recovery_backoff_attempts -> 1
    # which is below the threshold of 2, so no promotion yet.
    gen1 = uuid.uuid4()
    locked = await device_locking.lock_device_handle(db_session, device.id)
    set_recovery_generation(locked.device, gen1)
    await db_session.commit()
    locked = await device_locking.lock_device_handle(db_session, device.id)
    snapshot = await load_device_decision_snapshot(db_session, locked, packs={}, now=datetime.now(UTC))
    outcome1 = await svc.finalize_auto_recovery_locked(
        db_session,
        locked,
        snapshot,
        generation=gen1,
        result={"status": "failed", "error": "Probe failed"},
        source="device_checks",
        reason="r1",
    )
    await db_session.commit()
    await db_session.refresh(device)
    assert outcome1 == "failed"
    assert device.review_required is False
    attempts_after_first = (await remediation_log.load_ladder(db_session, device.id)).attempts
    assert attempts_after_first == 1

    # Attempt #2 — counter crosses threshold, device gets shelved.
    gen2 = uuid.uuid4()
    locked = await device_locking.lock_device_handle(db_session, device.id)
    set_recovery_generation(locked.device, gen2)
    await db_session.commit()
    locked = await device_locking.lock_device_handle(db_session, device.id)
    snapshot = await load_device_decision_snapshot(db_session, locked, packs={}, now=datetime.now(UTC))
    outcome2 = await svc.finalize_auto_recovery_locked(
        db_session,
        locked,
        snapshot,
        generation=gen2,
        result={"status": "failed", "error": "Probe failed"},
        source="device_checks",
        reason="r2",
    )
    await db_session.commit()
    await db_session.refresh(device)
    assert outcome2 == "failed"
    assert device.review_required is True
    assert device.review_reason == "Probe failed"


async def test_review_required_short_circuits_auto_recovery(
    db_session: AsyncSession,
    db_host: Host,
    _speed_up_recovery_probe_retries: None,
) -> None:
    """When the flag is on, ``prepare_auto_recovery_locked`` must not even reach
    the probe — backoff and intent state stay frozen."""
    from app.devices import locking as device_locking
    from app.devices.services.decision_snapshot import load_device_decision_snapshot

    device = await _make_offline_verified_device(db_session, db_host, "review-shortcircuit")
    await build_review_service().mark_review_required(
        db_session, device, reason="shelved earlier", source="session_viability"
    )
    await db_session.commit()

    viability_mock = Mock()
    viability_mock.run_session_viability_probe = AsyncMock()
    svc = LifecyclePolicyService(
        review=build_review_service(),
        publisher=event_bus,
        settings=FakeSettingsReader(_settings_stub(5)),
        actions=LifecyclePolicyActionsService(
            publisher=event_bus,
            reservation=RunReservationService(review=build_review_service()),
            incidents=LifecycleIncidentService(),
        ),
        incidents=LifecycleIncidentService(),
        viability=viability_mock,
        node_manager=AsyncMock(),
    )
    locked = await device_locking.lock_device_handle(db_session, device.id)
    snapshot = await load_device_decision_snapshot(db_session, locked, packs={}, now=datetime.now(UTC))
    prepared = await svc.prepare_auto_recovery_locked(
        db_session,
        locked,
        snapshot,
        generation=uuid.uuid4(),
        source="device_checks",
        reason="ignored",
        enqueue_job=False,
    )
    await db_session.commit()

    assert prepared is False
    viability_mock.run_session_viability_probe.assert_not_awaited()
