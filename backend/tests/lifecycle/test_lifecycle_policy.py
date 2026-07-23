import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock, patch

import pytest
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.appium_nodes.models import AppiumDesiredState, AppiumNode
from app.devices import locking as device_locking
from app.devices.models import (
    ConnectionType,
    Device,
    DeviceEvent,
    DeviceEventType,
    DeviceIntent,
    DeviceOperationalState,
    DeviceReservation,
    DeviceType,
    ExclusionKind,
)
from app.devices.models.remediation_log import DeviceRemediationLogEntry
from app.devices.services.claims import device_is_reserved
from app.devices.services.decision_snapshot import load_device_decision_snapshot
from app.devices.services.health import DeviceHealthService
from app.devices.services.intent import IntentService
from app.devices.services.lifecycle_policy_state import set_maintenance_reason
from app.devices.services.lifecycle_policy_summary import (
    build_lifecycle_policy,
    build_lifecycle_policy_summary,
)
from app.events.models import SystemEvent
from app.lifecycle.services import policy as lifecycle_policy_module
from app.lifecycle.services import remediation_log
from app.lifecycle.services.actions import LifecyclePolicyActionsService
from app.lifecycle.services.incidents import LifecycleIncidentService
from app.lifecycle.services.policy import DeferredStopOutcome, LifecyclePolicyService
from app.runs.models import RunState, TestRun
from app.runs.service_reservation import RunReservationService
from app.sessions.models import Session, SessionStatus
from tests.fakes import FakeSettingsReader, build_review_service
from tests.helpers import create_device, create_reserved_run, settle_after_commit_tasks
from tests.helpers import test_event_bus as event_bus

if TYPE_CHECKING:
    from app.hosts.models import Host

pytestmark = pytest.mark.usefixtures("seeded_driver_packs")


def _make_svc(
    publisher: object = None,
    settings: object = None,
    viability: object = None,
    node_manager: object = None,
    *,
    publish_incidents: bool = False,
) -> LifecyclePolicyService:
    from unittest.mock import AsyncMock, Mock

    pub = publisher if publisher is not None else Mock()
    svc_settings = settings if settings is not None else FakeSettingsReader({})
    via = viability if viability is not None else AsyncMock()
    nm = node_manager if node_manager is not None else AsyncMock()
    incidents = LifecycleIncidentService(publisher=pub) if publish_incidents else LifecycleIncidentService()
    return LifecyclePolicyService(
        review=build_review_service(),
        publisher=pub,  # type: ignore[arg-type]
        settings=svc_settings,  # type: ignore[arg-type]
        actions=LifecyclePolicyActionsService(
            publisher=pub,
            reservation=RunReservationService(review=build_review_service()),
            incidents=incidents,
        ),  # type: ignore[arg-type]
        incidents=incidents,
        viability=via,  # type: ignore[arg-type]
        node_manager=nm,  # type: ignore[arg-type]
    )


def _allow_recovery() -> AsyncMock:
    """AsyncMock replacing recovery_availability with an allowed verdict, for
    unit tests that exercise the recovery execution path past the guard ladder."""
    from app.devices.services.recovery_projection import RecoveryAvailability

    return AsyncMock(return_value=RecoveryAvailability(True, None, None))


async def _append_deferred_stop(
    db: AsyncSession, device: Device, *, reason: str = "ADB not responsive", source: str = "node_health"
) -> None:
    await remediation_log.append_action(
        db,
        device.id,
        source=source,
        action=remediation_log.ACTION_AUTO_STOP_DEFERRED,
        reason=reason,
    )


@pytest.fixture(autouse=True)
def _speed_up_recovery_probe_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.lifecycle.services import recovery_job as recovery_job_mod

    monkeypatch.setattr(recovery_job_mod, "RECOVERY_PROBE_RETRY_DELAY_SEC", 0, raising=False)
    monkeypatch.setattr(recovery_job_mod, "RECOVERY_PROBE_JITTER_MAX_SEC", 0, raising=False)
    monkeypatch.setattr(recovery_job_mod, "RECOVERY_NODE_START_WAIT_TIMEOUT_SEC", 0, raising=False)


@pytest.fixture(autouse=True)
def _derive_state_for_unit_objects(monkeypatch: pytest.MonkeyPatch) -> None:
    real_derive = lifecycle_policy_module.derive_operational_state

    async def _derive(db: object, device: object, *, now: object) -> DeviceOperationalState:
        if isinstance(device, Device):
            return await real_derive(db, device, now=now)  # type: ignore[arg-type]
        return device.operational_state  # type: ignore[union-attr,no-any-return]

    monkeypatch.setattr(lifecycle_policy_module, "derive_operational_state", _derive)
    monkeypatch.setattr("app.lifecycle.services.actions.derive_operational_state", _derive)


async def _mark_device_available(
    db: AsyncSession,
    *,
    device_id: object,
    intents: object,
    **kwargs: object,
) -> None:
    del intents, kwargs
    device = await db.get(Device, device_id)
    assert device is not None


async def _event_types_for_device(db_session: AsyncSession, device_id: object) -> list[DeviceEventType]:
    result = await db_session.execute(
        select(DeviceEvent.event_type).where(DeviceEvent.device_id == device_id).order_by(DeviceEvent.created_at.asc())
    )
    return list(result.scalars().all())


async def test_idle_health_failure_stops_device(db_session: AsyncSession, db_host: Host) -> None:
    device = Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value="policy-idle-1",
        connection_target="policy-idle-1",
        name="Idle Device",
        os_version="14",
        host_id=db_host.id,
        operational_state=DeviceOperationalState.available,
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )
    db_session.add(device)
    await db_session.commit()

    result = await _make_svc(publisher=Mock()).handle_health_failure(
        db_session, device, source="device_checks", reason="ADB not responsive"
    )

    await db_session.refresh(device)
    assert result == "stopped"
    assert device.operational_state_last_emitted == DeviceOperationalState.offline
    policy = await build_lifecycle_policy(db_session, device)
    assert policy["last_failure_reason"] == "ADB not responsive"
    assert policy["last_action"] == "auto_stopped"


async def test_active_session_failure_defers_stop(db_session: AsyncSession, db_host: Host) -> None:
    device = Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value="policy-busy-1",
        connection_target="policy-busy-1",
        name="Busy Device",
        os_version="14",
        host_id=db_host.id,
        operational_state=DeviceOperationalState.busy,
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )
    db_session.add(device)
    await db_session.flush()
    db_session.add(Session(session_id="sess-policy-1", device_id=device.id, status=SessionStatus.running))
    await db_session.commit()

    result = await _make_svc(publisher=event_bus).handle_health_failure(
        db_session, device, source="device_checks", reason="ADB not responsive"
    )

    await db_session.refresh(device)
    assert result == "deferred"
    assert device.operational_state_last_emitted == DeviceOperationalState.busy
    policy = await build_lifecycle_policy(db_session, device)
    assert policy["deferred_stop"] is True
    assert policy["recovery_state"] == "waiting_for_session_end"
    assert await _event_types_for_device(db_session, device.id) == [DeviceEventType.lifecycle_deferred_stop]


async def test_handle_health_failure_locked_reuses_lock_and_does_not_commit(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="locked-health-failure",
        operational_state=DeviceOperationalState.available,
        device_checks_healthy=True,
        device_checks_summary="Healthy",
    )
    await db_session.commit()
    locked = await device_locking.lock_device_handle(db_session, device.id)
    lock_spy = AsyncMock(wraps=device_locking.lock_device)
    monkeypatch.setattr(device_locking, "lock_device", lock_spy)
    commits = 0

    def count_commit(_session: object) -> None:
        nonlocal commits
        commits += 1

    event.listen(db_session.sync_session, "after_commit", count_commit)
    try:
        snapshot = await load_device_decision_snapshot(db_session, locked, packs={}, now=datetime.now(UTC))
        outcome = await _make_svc(publisher=event_bus, publish_incidents=True).handle_health_failure_locked(
            db_session,
            locked,
            snapshot,
            source="device_checks",
            reason="ADB not responsive",
        )

        assert outcome.decision_facts.in_maintenance is False
        assert outcome.ladder.deferred_stop_pending is False
        lock_spy.assert_not_awaited()
        assert commits == 0
        history = (
            (
                await db_session.execute(
                    select(DeviceRemediationLogEntry)
                    .where(DeviceRemediationLogEntry.device_id == device.id)
                    .order_by(DeviceRemediationLogEntry.at, DeviceRemediationLogEntry.id)
                )
            )
            .scalars()
            .all()
        )
        assert [(row.kind, row.action) for row in history] == [
            ("failure", "failure_observed"),
            ("action", "auto_stopped"),
        ]
        assert await _event_types_for_device(db_session, device.id) == [
            DeviceEventType.health_check_fail,
            DeviceEventType.node_crash,
            DeviceEventType.lifecycle_auto_stopped,
        ]

        await db_session.commit()
        await settle_after_commit_tasks()
        system_event_types = list(
            (
                await db_session.execute(
                    select(SystemEvent.type)
                    .where(SystemEvent.data.contains({"device_id": str(device.id)}))
                    .order_by(SystemEvent.id)
                )
            ).scalars()
        )
        assert system_event_types == [
            "device.crashed",
            "device.operational_state_changed",
            "device.health_changed",
            "device.lifecycle_incident",
        ]
    finally:
        event.remove(db_session.sync_session, "after_commit", count_commit)


async def test_handle_health_failure_locked_defers_without_commit(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="locked-health-failure-deferred",
        operational_state=DeviceOperationalState.busy,
    )
    db_session.add(Session(session_id="locked-deferred", device_id=device.id, status=SessionStatus.running))
    await db_session.commit()
    locked = await device_locking.lock_device_handle(db_session, device.id)
    lock_spy = AsyncMock(wraps=device_locking.lock_device)
    monkeypatch.setattr(device_locking, "lock_device", lock_spy)
    commits = 0

    def count_commit(_session: object) -> None:
        nonlocal commits
        commits += 1

    event.listen(db_session.sync_session, "after_commit", count_commit)
    try:
        snapshot = await load_device_decision_snapshot(db_session, locked, packs={}, now=datetime.now(UTC))
        outcome = await _make_svc(publisher=event_bus, publish_incidents=True).handle_health_failure_locked(
            db_session,
            locked,
            snapshot,
            source="device_checks",
            reason="ADB not responsive",
        )

        assert outcome.ladder.deferred_stop_pending is True
        lock_spy.assert_not_awaited()
        assert commits == 0
        history = (
            (
                await db_session.execute(
                    select(DeviceRemediationLogEntry)
                    .where(DeviceRemediationLogEntry.device_id == device.id)
                    .order_by(DeviceRemediationLogEntry.at, DeviceRemediationLogEntry.id)
                )
            )
            .scalars()
            .all()
        )
        assert [(row.kind, row.action) for row in history] == [
            ("failure", "failure_observed"),
            ("action", "auto_stop_deferred"),
        ]
        assert await _event_types_for_device(db_session, device.id) == [DeviceEventType.lifecycle_deferred_stop]

        await db_session.commit()
        await settle_after_commit_tasks()
        system_event_types = list(
            (
                await db_session.execute(
                    select(SystemEvent.type)
                    .where(SystemEvent.data.contains({"device_id": str(device.id)}))
                    .order_by(SystemEvent.id)
                )
            ).scalars()
        )
        assert system_event_types == ["device.lifecycle_incident"]
    finally:
        event.remove(db_session.sync_session, "after_commit", count_commit)


async def test_handle_health_failure_compatibility_wrapper_locks_and_commits_both_branches(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stopped = await create_device(
        db_session,
        host_id=db_host.id,
        name="compat-health-failure-stopped",
        operational_state=DeviceOperationalState.available,
        device_checks_healthy=True,
    )
    deferred = await create_device(
        db_session,
        host_id=db_host.id,
        name="compat-health-failure-deferred",
        operational_state=DeviceOperationalState.busy,
    )
    db_session.add(Session(session_id="compat-deferred", device_id=deferred.id, status=SessionStatus.running))
    await db_session.commit()
    lock_spy = AsyncMock(wraps=device_locking.lock_device_handle)
    monkeypatch.setattr(device_locking, "lock_device_handle", lock_spy)
    commits = 0

    def count_commit(_session: object) -> None:
        nonlocal commits
        commits += 1

    event.listen(db_session.sync_session, "after_commit", count_commit)
    try:
        service = _make_svc(publisher=Mock())
        assert (
            await service.handle_health_failure(
                db_session,
                stopped,
                source="device_checks",
                reason="stopped failure",
            )
            == "stopped"
        )
        assert commits == 1
        assert any(call.args[1] == stopped.id for call in lock_spy.await_args_list)

        lock_spy.reset_mock()
        assert (
            await service.handle_health_failure(
                db_session,
                deferred,
                source="device_checks",
                reason="deferred failure",
            )
            == "deferred"
        )
        assert commits == 2
        lock_spy.assert_awaited_once_with(db_session, deferred.id, load_sessions=True)
    finally:
        event.remove(db_session.sync_session, "after_commit", count_commit)


async def test_reserved_idle_failure_excludes_run(db_session: AsyncSession, db_host: Host) -> None:
    device = Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value="policy-run-1",
        connection_target="policy-run-1",
        name="Reserved Device",
        os_version="14",
        host_id=db_host.id,
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )
    db_session.add(device)
    await db_session.flush()
    run = TestRun(
        name="Active Run",
        state=RunState.active,
        requirements=[{"pack_id": "appium-uiautomator2", "platform_id": "android_mobile", "count": 1}],
        ttl_minutes=60,
        heartbeat_timeout_sec=120,
        reserved_devices=[
            {
                "device_id": str(device.id),
                "identity_value": device.identity_value,
                "connection_target": device.connection_target,
                "pack_id": "appium-uiautomator2",
                "platform_id": "android_mobile",
                "os_version": device.os_version,
                "host_ip": None,
                "excluded": False,
                "exclusion_reason": None,
                "excluded_at": None,
            }
        ],
    )
    db_session.add(run)
    await db_session.commit()

    await _make_svc(publisher=event_bus).handle_health_failure(
        db_session, device, source="device_checks", reason="Health probe failed"
    )

    await db_session.refresh(device)
    await db_session.refresh(run, ["device_reservations"])
    assert device.operational_state_last_emitted == DeviceOperationalState.offline
    assert run.reserved_devices is not None
    assert run.reserved_devices[0]["excluded"] is True
    assert run.reserved_devices[0]["exclusion_reason"] == "Health probe failed"
    assert run.device_reservations[0].excluded is True
    assert run.device_reservations[0].exclusion_reason == "Health probe failed"


async def test_session_finish_completes_deferred_stop_and_excludes_run(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    device = Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value="policy-run-2",
        connection_target="policy-run-2",
        name="Deferred Device",
        os_version="14",
        host_id=db_host.id,
        operational_state=DeviceOperationalState.busy,
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )
    db_session.add(device)
    await db_session.flush()
    run = TestRun(
        name="Deferred Run",
        state=RunState.active,
        requirements=[{"pack_id": "appium-uiautomator2", "platform_id": "android_mobile", "count": 1}],
        ttl_minutes=60,
        heartbeat_timeout_sec=120,
        reserved_devices=[
            {
                "device_id": str(device.id),
                "identity_value": device.identity_value,
                "connection_target": device.connection_target,
                "pack_id": "appium-uiautomator2",
                "platform_id": "android_mobile",
                "os_version": device.os_version,
                "host_ip": None,
                "excluded": False,
                "exclusion_reason": None,
                "excluded_at": None,
            }
        ],
    )
    session = Session(session_id="sess-policy-2", device_id=device.id, status=SessionStatus.running)
    db_session.add_all([run, session])
    await db_session.commit()

    await _make_svc(publisher=Mock()).handle_health_failure(
        db_session, device, source="device_checks", reason="Health probe failed"
    )
    session.status = SessionStatus.passed
    session.ended_at = datetime.now(UTC)
    await db_session.commit()

    stopped = await _make_svc(publisher=Mock()).handle_session_finished(db_session, device)

    await db_session.refresh(device)
    await db_session.refresh(run, ["device_reservations"])
    assert stopped is DeferredStopOutcome.AUTO_STOPPED
    assert device.operational_state_last_emitted == DeviceOperationalState.offline
    assert run.reserved_devices is not None
    assert run.reserved_devices[0]["excluded"] is True
    policy = await build_lifecycle_policy(db_session, device)
    assert policy["deferred_stop"] is False
    assert policy["excluded_from_run"] is True


async def test_recovery_is_suppressed_during_backoff(db_session: AsyncSession, db_host: Host) -> None:
    device = Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value="policy-recover-2",
        connection_target="policy-recover-2",
        name="Backoff Device",
        os_version="14",
        host_id=db_host.id,
        operational_state=DeviceOperationalState.offline,
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )
    db_session.add(device)
    await db_session.commit()
    await remediation_log.append_entry(
        db_session,
        device.id,
        kind=remediation_log.KIND_ATTEMPT,
        source="node_health",
        action="recovery_failed",
        reason="backoff",
        backoff_until=datetime.now(UTC) + timedelta(minutes=5),
    )
    await db_session.commit()

    locked = await device_locking.lock_device_handle(db_session, device.id)
    snapshot = await load_device_decision_snapshot(db_session, locked, packs={}, now=datetime.now(UTC))
    prepared = await _make_svc(publisher=event_bus).prepare_auto_recovery_locked(
        db_session,
        locked,
        snapshot,
        generation=uuid.uuid4(),
        source="device_checks",
        reason="Healthy again",
        enqueue_job=False,
    )
    await db_session.commit()

    assert prepared is False
    policy = await build_lifecycle_policy(db_session, device)
    assert policy["recovery_state"] == "backoff"


async def test_successful_recovery_rejoins_run(db_session: AsyncSession, db_host: Host) -> None:
    from app.devices.services.lifecycle_policy_state import set_recovery_generation

    device = Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value="policy-recover-3",
        connection_target="policy-recover-3",
        name="Recovering Device",
        os_version="14",
        host_id=db_host.id,
        operational_state=DeviceOperationalState.offline,
        verified_at=datetime.now(UTC),
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )
    db_session.add(device)
    await db_session.flush()
    run = TestRun(
        name="Recovering Run",
        state=RunState.active,
        requirements=[{"pack_id": "appium-uiautomator2", "platform_id": "android_mobile", "count": 1}],
        ttl_minutes=60,
        heartbeat_timeout_sec=120,
        reserved_devices=[
            {
                "device_id": str(device.id),
                "identity_value": device.identity_value,
                "connection_target": device.connection_target,
                "pack_id": "appium-uiautomator2",
                "platform_id": "android_mobile",
                "os_version": device.os_version,
                "host_ip": None,
                "excluded": True,
                "exclusion_reason": "Health probe failed",
                "excluded_at": datetime.now(UTC).isoformat(),
            }
        ],
    )
    db_session.add(run)
    await db_session.commit()

    generation = uuid.uuid4()
    locked = await device_locking.lock_device_handle(db_session, device.id)
    set_recovery_generation(locked.device, generation)
    await db_session.commit()

    svc = _make_svc(publisher=Mock())
    locked = await device_locking.lock_device_handle(db_session, device.id)
    snapshot = await load_device_decision_snapshot(db_session, locked, packs={}, now=datetime.now(UTC))
    outcome = await svc.finalize_auto_recovery_locked(
        db_session,
        locked,
        snapshot,
        generation=generation,
        result={"status": "passed"},
        source="device_checks",
        reason="Healthy again",
    )
    await db_session.commit()

    assert outcome == "recovered"
    await db_session.refresh(run, ["device_reservations"])
    await db_session.refresh(device)
    assert await device_is_reserved(db_session, device.id)
    assert run.reserved_devices is not None
    assert run.reserved_devices[0]["excluded"] is False
    assert run.device_reservations[0].excluded is False
    policy = await build_lifecycle_policy(db_session, device)
    assert policy["last_action"] == "auto_recovered"
    assert policy["excluded_from_run"] is False
    event_types = await _event_types_for_device(db_session, device.id)
    assert DeviceEventType.lifecycle_recovered in event_types


@pytest.mark.db
async def test_auto_recovery_supersedes_stale_stop_directive(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    from app.devices.services.lifecycle_policy_state import set_recovery_generation

    device = Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value="policy-recover-stale-intents",
        connection_target="policy-recover-stale-intents",
        name="Recovering Stale Intent Device",
        os_version="14",
        host_id=db_host.id,
        operational_state=DeviceOperationalState.offline,
        verified_at=datetime.now(UTC),
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )
    db_session.add(device)
    await db_session.flush()
    db_session.add(AppiumNode(device_id=device.id, port=4723))
    await remediation_log.append_action(
        db_session,
        device.id,
        source="health_check_fail",
        action=remediation_log.ACTION_AUTO_STOP_COMMISSIONED,
        reason="stale stop",
    )
    await db_session.commit()

    generation = uuid.uuid4()
    locked = await device_locking.lock_device_handle(db_session, device.id)
    set_recovery_generation(locked.device, generation)
    await db_session.commit()

    svc = _make_svc(publisher=event_bus)
    locked = await device_locking.lock_device_handle(db_session, device.id)
    snapshot = await load_device_decision_snapshot(db_session, locked, packs={}, now=datetime.now(UTC))
    outcome = await svc.finalize_auto_recovery_locked(
        db_session,
        locked,
        snapshot,
        generation=generation,
        result={"status": "passed"},
        source="device_checks",
        reason="Healthy again",
    )
    await db_session.commit()

    assert outcome == "recovered"
    ladder = await remediation_log.load_ladder(db_session, device.id)
    assert ladder.node_directive is None
    assert ladder.last_action == "auto_recovered"


@pytest.mark.db
async def test_auto_recovery_start_directive_has_no_ttl(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    """A recovery START directive persists until a log reset supersedes it."""
    device = Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value="auto-recovery-precondition",
        connection_target="auto-recovery-precondition",
        name="Auto Recovery Precondition Device",
        os_version="14",
        host_id=db_host.id,
        operational_state=DeviceOperationalState.offline,
        verified_at=datetime.now(UTC),
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )
    db_session.add(device)
    await db_session.flush()
    db_session.add(AppiumNode(device_id=device.id, port=4723))
    await db_session.commit()
    await db_session.refresh(device, attribute_names=["appium_node"])

    generation = uuid.uuid4()
    locked = await device_locking.lock_device_handle(db_session, device.id)
    snapshot = await load_device_decision_snapshot(db_session, locked, packs={}, now=datetime.now(UTC))
    prepared = await _make_svc(publisher=event_bus).prepare_auto_recovery_locked(
        db_session,
        locked,
        snapshot,
        generation=generation,
        source="recovery",
        reason="recovery started",
        enqueue_job=False,
    )
    await db_session.commit()
    assert prepared is True
    ladder = await remediation_log.load_ladder(db_session, device.id)
    assert ladder.node_directive is not None
    assert ladder.node_directive.kind == remediation_log.DIRECTIVE_START
    assert (
        await db_session.execute(select(DeviceIntent).where(DeviceIntent.device_id == device.id))
    ).scalars().all() == []


@pytest.mark.db
async def test_auto_recovery_clears_blocking_node_stop_when_observed_running_is_stale(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    """Recovery must unblock an offline device even when ``observed_running`` is stale.

    ``observed_running`` (``pid``/``active_connection_target``) is eventually
    consistent: after an appium process dies there is a window (one
    ``appium_reconciler`` interval) where the DB row still reports the node
    running. If recovery fires during that window for an *offline* device, the
    short-circuit at ``attempt_auto_recovery`` (``if node is None or not
    node.observed_running``) skips the reconcile that applies the recovery START
    directive over the stop directive, so the node can never be told to start and the device is
    stranded in backoff until the stale observation happens to clear. An offline
    device has no usable running node, so recovery must clear that blocking stop
    regardless of the stale snapshot.
    """
    from app.devices.services.lifecycle_policy_state import set_recovery_generation

    device = Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value="stale-observed-running",
        connection_target="stale-observed-running",
        name="Stale Observed Running Device",
        os_version="14",
        host_id=db_host.id,
        operational_state=DeviceOperationalState.offline,
        verified_at=datetime.now(UTC),
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )
    db_session.add(device)
    await db_session.flush()
    # Stale-positive observation: pid + active_connection_target set, so
    # ``observed_running`` is True even though the process is actually dead.
    db_session.add(
        AppiumNode(
            device_id=device.id,
            port=4723,
            pid=99999,
            active_connection_target="stale-observed-running",
        )
    )
    await db_session.commit()

    # The stop directive the crash handler leaves behind.
    await remediation_log.append_action(
        db_session,
        device.id,
        source="health_check_fail",
        action=remediation_log.ACTION_AUTO_STOP_COMMISSIONED,
        reason="offline",
    )
    await db_session.commit()

    generation = uuid.uuid4()
    locked = await device_locking.lock_device_handle(db_session, device.id)
    set_recovery_generation(locked.device, generation)
    await db_session.commit()

    svc = _make_svc(publisher=event_bus)
    locked = await device_locking.lock_device_handle(db_session, device.id)
    snapshot = await load_device_decision_snapshot(db_session, locked, packs={}, now=datetime.now(UTC))
    outcome = await svc.finalize_auto_recovery_locked(
        db_session,
        locked,
        snapshot,
        generation=generation,
        result={"status": "passed"},
        source="device_checks",
        reason="Recovering offline device",
    )
    await db_session.commit()

    assert outcome == "recovered"
    ladder = await remediation_log.load_ladder(db_session, device.id)
    assert ladder.node_directive is None
    assert ladder.last_action == "auto_recovered"


async def test_recovery_reloads_device_before_starting_node(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    db_host: Host,
) -> None:
    device = Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value="policy-race-1",
        connection_target="policy-race-1",
        name="Race Device",
        os_version="14",
        host_id=db_host.id,
        operational_state=DeviceOperationalState.offline,
        verified_at=datetime.now(UTC),
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )
    db_session.add(device)
    await db_session.commit()

    async with db_session_maker() as other_session:
        current = await other_session.get(Device, device.id)
        assert current is not None
        current.operational_state_last_emitted = DeviceOperationalState.available
        other_session.add(
            AppiumNode(
                device_id=device.id,
                port=4724,
                pid=1234,
                active_connection_target=device.connection_target,
                desired_state=AppiumDesiredState.running,
                desired_port=4724,
            )
        )
        await other_session.commit()

    register_recovery = AsyncMock()
    with patch.object(IntentService, "register_intents_and_reconcile", new=register_recovery):
        locked = await device_locking.lock_device_handle(db_session, device.id)
        snapshot = await load_device_decision_snapshot(db_session, locked, packs={}, now=datetime.now(UTC))
        prepared = await _make_svc(publisher=event_bus).prepare_auto_recovery_locked(
            db_session,
            locked,
            snapshot,
            generation=uuid.uuid4(),
            source="device_checks",
            reason="Healthy again",
            enqueue_job=False,
        )
        await db_session.commit()

    assert prepared is False
    register_recovery.assert_not_awaited()


async def test_failed_recovery_sets_backoff_and_keeps_exclusion(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    from app.devices.services.lifecycle_policy_state import set_recovery_generation

    device = Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value="policy-recover-4",
        connection_target="policy-recover-4",
        name="Flaky Device",
        os_version="14",
        host_id=db_host.id,
        operational_state=DeviceOperationalState.offline,
        verified_at=datetime.now(UTC),
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )
    db_session.add(device)
    await db_session.flush()
    run = TestRun(
        name="Flaky Run",
        state=RunState.active,
        requirements=[{"pack_id": "appium-uiautomator2", "platform_id": "android_mobile", "count": 1}],
        ttl_minutes=60,
        heartbeat_timeout_sec=120,
        reserved_devices=[
            {
                "device_id": str(device.id),
                "identity_value": device.identity_value,
                "connection_target": device.connection_target,
                "pack_id": "appium-uiautomator2",
                "platform_id": "android_mobile",
                "os_version": device.os_version,
                "host_ip": None,
                "excluded": True,
                "exclusion_reason": "Health probe failed",
                "excluded_at": datetime.now(UTC).isoformat(),
            }
        ],
    )
    db_session.add(run)
    await db_session.commit()

    generation = uuid.uuid4()
    locked = await device_locking.lock_device_handle(db_session, device.id)
    set_recovery_generation(locked.device, generation)
    await db_session.commit()

    svc = _make_svc(publisher=Mock())
    locked = await device_locking.lock_device_handle(db_session, device.id)
    snapshot = await load_device_decision_snapshot(db_session, locked, packs={}, now=datetime.now(UTC))
    outcome = await svc.finalize_auto_recovery_locked(
        db_session,
        locked,
        snapshot,
        generation=generation,
        result={"status": "failed", "error": "Session create failed"},
        source="device_checks",
        reason="Healthy again",
    )
    await db_session.commit()

    assert outcome == "failed"
    await db_session.refresh(run, ["device_reservations"])
    await db_session.refresh(device)
    assert device.operational_state_last_emitted == DeviceOperationalState.offline
    assert run.reserved_devices is not None
    assert run.reserved_devices[0]["excluded"] is True
    assert run.device_reservations[0].excluded is True
    policy = await build_lifecycle_policy(db_session, device)
    assert policy["last_action"] == "recovery_failed"
    assert policy["backoff_until"] is not None
    event_types = await _event_types_for_device(db_session, device.id)
    assert DeviceEventType.lifecycle_recovery_failed in event_types
    assert DeviceEventType.lifecycle_recovery_backoff in event_types


async def test_recovery_retries_transient_probe_failure_before_stopping_node(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    from app.lifecycle.services.recovery_job import RecoveryJobService

    device = Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value="policy-retry-1",
        connection_target="policy-retry-1",
        name="Retry Device",
        os_version="14",
        host_id=db_host.id,
        operational_state=DeviceOperationalState.offline,
        verified_at=datetime.now(UTC),
        device_type=DeviceType.emulator,
        connection_type=ConnectionType.virtual,
    )
    db_session.add(device)
    await db_session.commit()

    mock_probe = AsyncMock(
        side_effect=[
            {
                "status": "failed",
                "last_attempted_at": datetime.now(UTC).isoformat(),
                "last_succeeded_at": None,
                "error": "Android settings service is not ready",
                "checked_by": "recovery",
            },
            {
                "status": "passed",
                "last_attempted_at": datetime.now(UTC).isoformat(),
                "last_succeeded_at": datetime.now(UTC).isoformat(),
                "error": None,
                "checked_by": "recovery",
            },
        ]
    )
    viability = AsyncMock()
    viability.run_session_viability_probe = mock_probe
    assert db_session.bind is not None
    sf = async_sessionmaker(db_session.bind, class_=AsyncSession, expire_on_commit=False)
    worker = RecoveryJobService(
        session_factory=sf,
        publisher=event_bus,
        settings=FakeSettingsReader({}),
        lifecycle_policy=_make_svc(publisher=event_bus, viability=viability),
        viability=viability,
    )
    result = await worker._run_probe(device.id)

    assert result["status"] == "passed"
    assert mock_probe.await_count == 2


async def test_deferred_stop_survives_restart_boundary(db_session: AsyncSession, db_host: Host) -> None:
    device = Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value="policy-restart-1",
        connection_target="policy-restart-1",
        name="Restart Deferred Device",
        os_version="14",
        host_id=db_host.id,
        operational_state=DeviceOperationalState.busy,
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )
    db_session.add(device)
    await db_session.flush()
    session = Session(session_id="sess-policy-restart", device_id=device.id, status=SessionStatus.running)
    db_session.add(session)
    await db_session.commit()

    result = await _make_svc(publisher=Mock()).handle_health_failure(
        db_session, device, source="device_checks", reason="ADB not responsive"
    )
    assert result == "deferred"

    ladder = await remediation_log.load_ladder(db_session, device.id)
    assert ladder.deferred_stop_pending is True

    session.status = SessionStatus.passed
    session.ended_at = datetime.now(UTC)
    await db_session.commit()

    reloaded = await db_session.get(Device, device.id)
    assert reloaded is not None
    stopped = await _make_svc(publisher=Mock()).handle_session_finished(db_session, reloaded)

    assert stopped is DeferredStopOutcome.AUTO_STOPPED
    assert reloaded.operational_state_last_emitted == DeviceOperationalState.offline
    assert (await remediation_log.load_ladder(db_session, reloaded.id)).deferred_stop_pending is False


async def test_failed_recovery_backoff_survives_restart_and_uses_settings(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    from app.devices.services.lifecycle_policy_state import set_recovery_generation

    device = Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value="policy-restart-2",
        connection_target="policy-restart-2",
        name="Restart Backoff Device",
        os_version="14",
        host_id=db_host.id,
        operational_state=DeviceOperationalState.offline,
        verified_at=datetime.now(UTC),
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )
    db_session.add(device)
    await db_session.commit()

    settings = FakeSettingsReader(
        {
            "general.lifecycle_recovery_backoff_base_sec": 5,
            "general.lifecycle_recovery_backoff_max_sec": 20,
            "general.lifecycle_recovery_review_threshold": 5,
            "appium.port_range_start": 4720,
            "appium.port_range_end": 4800,
        }
    )
    generation = uuid.uuid4()
    locked = await device_locking.lock_device_handle(db_session, device.id)
    set_recovery_generation(locked.device, generation)
    await db_session.commit()

    recovery_started_at = datetime.now(UTC)
    svc = _make_svc(publisher=Mock(), settings=settings)
    locked = await device_locking.lock_device_handle(db_session, device.id)
    snapshot = await load_device_decision_snapshot(db_session, locked, packs={}, now=datetime.now(UTC))
    outcome = await svc.finalize_auto_recovery_locked(
        db_session,
        locked,
        snapshot,
        generation=generation,
        result={"status": "failed", "error": "Probe failed"},
        source="device_checks",
        reason="Healthy again",
    )
    await db_session.commit()

    assert outcome == "failed"
    await db_session.refresh(device)
    ladder = await remediation_log.load_ladder(db_session, device.id)
    assert ladder.backoff_until is not None
    backoff_until = ladder.backoff_until
    assert 5 <= (backoff_until - recovery_started_at).total_seconds() <= 8

    reloaded = await db_session.get(Device, device.id)
    assert reloaded is not None
    policy = await build_lifecycle_policy(db_session, reloaded)
    assert policy["recovery_state"] == "backoff"
    assert policy["backoff_until"] == ladder.backoff_until.isoformat()


async def test_lifecycle_summary_reports_deferred_and_excluded_states(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    device = Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value="policy-summary-1",
        connection_target="policy-summary-1",
        name="Summary Device",
        os_version="14",
        host_id=db_host.id,
        operational_state=DeviceOperationalState.busy,
        lifecycle_policy_state={
            "deferred_stop": True,
            "deferred_stop_reason": "ADB not responsive",
        },
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )
    db_session.add(device)
    await db_session.flush()
    run = TestRun(
        name="Summary Run",
        state=RunState.active,
        requirements=[{"pack_id": "appium-uiautomator2", "platform_id": "android_mobile", "count": 1}],
        ttl_minutes=60,
        heartbeat_timeout_sec=120,
        reserved_devices=[
            {
                "device_id": str(device.id),
                "identity_value": device.identity_value,
                "connection_target": device.connection_target,
                "pack_id": "appium-uiautomator2",
                "platform_id": "android_mobile",
                "os_version": device.os_version,
                "host_ip": None,
                "excluded": True,
                "exclusion_reason": "ADB not responsive",
                "excluded_at": datetime.now(UTC).isoformat(),
            }
        ],
    )
    db_session.add(run)
    await db_session.commit()
    await _append_deferred_stop(db_session, device)

    policy = await build_lifecycle_policy(db_session, device)
    summary = build_lifecycle_policy_summary(policy)
    assert summary["state"] == "deferred_stop"
    assert summary["label"] == "Stopping Soon"

    await remediation_log.append_action(
        db_session,
        device.id,
        source="node_health",
        action=remediation_log.ACTION_AUTO_STOP_CLEARED,
        reason="recovered",
    )
    await db_session.commit()

    policy = await build_lifecycle_policy(db_session, device)
    summary = build_lifecycle_policy_summary(policy)
    assert summary["state"] == "excluded"
    assert summary["detail"] == "Excluded from Summary Run"


def test_lifecycle_summary_surfaces_reconciler_start_failure() -> None:
    summary = build_lifecycle_policy_summary(
        {
            "recovery_state": "idle",
            "last_failure_source": "appium_reconciler",
            "last_failure_reason": "port_occupied",
            "backoff_until": None,
        }
    )

    assert summary["state"] == "recoverable"
    assert summary["label"] == "Start Failed"
    assert summary["detail"] == "port_occupied"


async def test_clear_pending_auto_stop_on_recovery_drops_intent_and_records_incident(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    device = Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value="lifecycle-clear-pending-1",
        connection_target="lifecycle-clear-pending-1",
        name="Clear Pending Device",
        os_version="14",
        host_id=db_host.id,
        operational_state=DeviceOperationalState.busy,
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
        lifecycle_policy_state={
            "deferred_stop": True,
            "deferred_stop_reason": "ADB not responsive",
            "deferred_stop_since": "2026-05-04T10:00:00+00:00",
            "last_action": "auto_stop_deferred",
            "last_failure_source": "node_health",
            "last_failure_reason": "Probe failed",
            "recovery_suppressed_reason": None,
        },
    )
    db_session.add(device)
    await db_session.commit()

    await _append_deferred_stop(db_session, device, reason="ADB not responsive")

    cleared = await _make_svc().clear_pending_auto_stop_on_recovery(
        db_session,
        device,
        source="node_health",
        reason="Node health checks recovered",
    )
    await db_session.commit()
    assert cleared is True

    reloaded = await db_session.get(Device, device.id)
    assert reloaded is not None
    assert (await remediation_log.load_ladder(db_session, reloaded.id)).deferred_stop_pending is False

    incident_stmt = select(DeviceEvent).where(
        DeviceEvent.device_id == device.id,
        DeviceEvent.event_type == DeviceEventType.lifecycle_recovered,
    )
    incidents = list((await db_session.execute(incident_stmt)).scalars().all())
    assert len(incidents) == 1
    details = incidents[0].details or {}
    detail = details.get("detail", "")
    assert "ADB not responsive" in detail
    assert "deferred stop" in detail.lower()


async def test_clear_pending_auto_stop_on_recovery_no_op_when_not_pending(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    device = Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value="lifecycle-clear-pending-2",
        connection_target="lifecycle-clear-pending-2",
        name="No Pending Device",
        os_version="14",
        host_id=db_host.id,
        operational_state=DeviceOperationalState.available,
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
        lifecycle_policy_state={
            "deferred_stop": False,
            "deferred_stop_reason": None,
            "deferred_stop_since": None,
            "last_action": "node_monitor_recovered",
        },
    )
    db_session.add(device)
    await db_session.commit()

    cleared = await _make_svc().clear_pending_auto_stop_on_recovery(
        db_session,
        device,
        source="node_health",
        reason="Node health checks recovered",
    )
    assert cleared is False

    incident_stmt = select(DeviceEvent).where(DeviceEvent.device_id == device.id)
    incidents = list((await db_session.execute(incident_stmt)).scalars().all())
    assert incidents == []


async def test_handle_session_finished_drops_intent_when_healthy(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    device = Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value="lifecycle-finish-healthy",
        connection_target="lifecycle-finish-healthy",
        name="Finish Healthy",
        os_version="14",
        host_id=db_host.id,
        operational_state=DeviceOperationalState.busy,
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
        lifecycle_policy_state={
            "deferred_stop": True,
            "deferred_stop_reason": "ADB not responsive",
            "deferred_stop_since": "2026-05-04T10:00:00+00:00",
            "last_action": "auto_stop_deferred",
            "last_failure_source": "node_health",
            "last_failure_reason": "ADB not responsive",
            "recovery_suppressed_reason": None,
        },
    )
    db_session.add(device)
    await db_session.flush()
    await _append_deferred_stop(db_session, device)
    node = AppiumNode(
        device_id=device.id,
        port=4781,
        desired_state=AppiumDesiredState.running,
        desired_port=4781,
        pid=0,
        active_connection_target="",
    )
    db_session.add(node)
    await db_session.commit()

    _health_svc = DeviceHealthService(publisher=event_bus)
    await _health_svc.apply_node_state_transition(
        db_session,
        device,
        health_running=None,
        health_state=None,
        mark_offline=False,
    )
    await _health_svc.update_device_checks(db_session, device, healthy=True, summary="Healthy")
    await db_session.commit()

    reloaded = await db_session.get(Device, device.id)
    assert reloaded is not None
    stopped = await _make_svc(publisher=event_bus).handle_session_finished(db_session, reloaded)
    await db_session.commit()
    # CLEARED_RECOVERED: intent dropped, no auto-stop. Callers must use the
    # explicit outcome (not "not AUTO_STOPPED") to decide whether to restore
    # availability — this is the contract that replaces the old True/False
    # boolean.
    assert stopped is DeferredStopOutcome.NO_PENDING_OR_RECOVERED

    await db_session.refresh(reloaded)
    assert (await remediation_log.load_ladder(db_session, reloaded.id)).deferred_stop_pending is False
    # last_action must be refreshed so the audit trail does not show a stale
    # ``auto_stop_deferred`` after the intent was cleared by the healthy
    # session-end branch (see ``clear_pending_auto_stop_on_recovery``).
    assert (await remediation_log.load_ladder(db_session, reloaded.id)).last_action == "auto_stop_cleared"
    # reconcile_device (now called with publisher) derives operational_state
    # authoritatively. The intent reconciler set desired_state=stopped, so
    # stop_in_flight is True → offline is the correct derived value here.
    # Restoration to available/busy is the session_sync caller's responsibility
    # (covered by test_session_sync_restores_busy_after_healthy_drop).
    assert reloaded.operational_state_last_emitted == DeviceOperationalState.offline


async def test_handle_session_finished_executes_stop_when_unhealthy(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    device = Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value="lifecycle-finish-unhealthy",
        connection_target="lifecycle-finish-unhealthy",
        name="Finish Unhealthy",
        os_version="14",
        host_id=db_host.id,
        operational_state=DeviceOperationalState.busy,
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
        lifecycle_policy_state={
            "deferred_stop": True,
            "deferred_stop_reason": "ADB not responsive",
            "deferred_stop_since": "2026-05-04T10:00:00+00:00",
            "last_action": "auto_stop_deferred",
            "last_failure_source": "node_health",
            "last_failure_reason": "ADB not responsive",
            "recovery_suppressed_reason": None,
        },
    )
    db_session.add(device)
    await db_session.flush()
    await _append_deferred_stop(db_session, device)
    await db_session.commit()

    _health_svc = DeviceHealthService(publisher=event_bus)
    await _health_svc.apply_node_state_transition(
        db_session,
        device,
        health_running=False,
        health_state="error",
        mark_offline=False,
    )
    await _health_svc.update_device_checks(db_session, device, healthy=False, summary="Probe failed")
    await db_session.commit()

    reloaded = await db_session.get(Device, device.id)
    assert reloaded is not None
    stopped = await _make_svc(publisher=Mock()).handle_session_finished(db_session, reloaded)
    await db_session.commit()
    assert stopped is DeferredStopOutcome.AUTO_STOPPED

    await db_session.refresh(reloaded)
    assert (await remediation_log.load_ladder(db_session, reloaded.id)).deferred_stop_pending is False
    assert reloaded.operational_state_last_emitted == DeviceOperationalState.offline  # complete_auto_stop ran


async def test_handle_session_finished_executes_stop_when_node_not_running(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    device = Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value="lifecycle-finish-node-stopped",
        connection_target="lifecycle-finish-node-stopped",
        name="Finish Node Stopped",
        os_version="14",
        host_id=db_host.id,
        operational_state=DeviceOperationalState.busy,
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
        lifecycle_policy_state={
            "deferred_stop": True,
            "deferred_stop_reason": "Disconnected",
            "deferred_stop_since": "2026-05-04T10:00:00+00:00",
            "last_action": "auto_stop_deferred",
            "last_failure_source": "device_checks",
            "last_failure_reason": "Disconnected",
            "recovery_suppressed_reason": None,
        },
    )
    db_session.add(device)
    await db_session.flush()
    await _append_deferred_stop(db_session, device, reason="Disconnected", source="device_checks")
    # Node already stopped - even if health checks read healthy, complete_auto_stop must still run.
    node = AppiumNode(
        device_id=device.id,
        port=4783,
        desired_state=AppiumDesiredState.stopped,
        desired_port=None,
        pid=None,
        active_connection_target=None,
    )
    db_session.add(node)
    await db_session.commit()
    await DeviceHealthService(publisher=Mock()).update_device_checks(
        db_session, device, healthy=True, summary="Healthy"
    )
    await db_session.commit()

    reloaded = await db_session.get(Device, device.id)
    assert reloaded is not None
    stopped = await _make_svc(publisher=Mock()).handle_session_finished(db_session, reloaded)
    await db_session.commit()
    assert stopped is DeferredStopOutcome.AUTO_STOPPED

    await db_session.refresh(reloaded)
    assert (await remediation_log.load_ladder(db_session, reloaded.id)).deferred_stop_pending is False
    assert reloaded.operational_state_last_emitted == DeviceOperationalState.offline


async def test_handle_session_finished_returns_no_pending_when_intent_absent(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    device = Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value="lifecycle-no-pending",
        connection_target="lifecycle-no-pending",
        name="No Pending",
        os_version="14",
        host_id=db_host.id,
        operational_state=DeviceOperationalState.busy,
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
        lifecycle_policy_state={"deferred_stop": False, "last_action": "idle"},
    )
    db_session.add(device)
    await db_session.commit()

    reloaded = await db_session.get(Device, device.id)
    assert reloaded is not None
    outcome = await _make_svc(publisher=event_bus).handle_session_finished(db_session, reloaded)
    assert outcome is DeferredStopOutcome.NO_PENDING_OR_RECOVERED


async def test_handle_session_finished_applies_held_graceful_stop_intent(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    """End-to-end: a graceful-stop intent registered while a client session is
    running stays held by the intent reconciler, then applies on the same tick
    the session ends via ``handle_session_finished``.

    Exercises the lifecycle-policy → reconciler handshake added so that
    intent-driven deferrals (which do not touch ``lifecycle_policy_state``)
    still converge promptly when the held session finishes; without the
    up-front reconcile, the held intent would wait for the next full scan.
    """
    from app.devices.services.intent_reconciler import reconcile_device

    device = Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value="lifecycle-held-intent",
        connection_target="lifecycle-held-intent",
        name="Held Intent",
        os_version="14",
        host_id=db_host.id,
        operational_state=DeviceOperationalState.busy,
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
        lifecycle_policy_state={"deferred_stop": False, "last_action": "idle"},
    )
    db_session.add(device)
    await db_session.flush()
    node = AppiumNode(
        device_id=device.id,
        port=4796,
        desired_state=AppiumDesiredState.running,
        desired_port=4796,
        pid=42,
        active_connection_target=device.connection_target,
    )
    db_session.add(node)
    session = Session(
        session_id="held-intent-session",
        device_id=device.id,
        status=SessionStatus.running,
    )
    db_session.add(session)
    await db_session.commit()

    await remediation_log.append_action(
        db_session,
        device.id,
        source="health_check_fail",
        action=remediation_log.ACTION_AUTO_STOP_COMMISSIONED,
        reason="session held",
    )
    await db_session.commit()

    # Held while the session is running.
    await reconcile_device(db_session, device.id, publisher=event_bus)
    await db_session.commit()
    await db_session.refresh(node)
    assert node.desired_state == AppiumDesiredState.running
    assert node.stop_pending is True
    assert node.accepting_new_sessions is False

    # Session ends, then the handshake applies the held intent.
    session.status = SessionStatus.passed
    session.ended_at = datetime.now(UTC)
    await db_session.commit()
    reloaded = await db_session.get(Device, device.id)
    assert reloaded is not None
    outcome = await _make_svc(publisher=event_bus).handle_session_finished(db_session, reloaded)
    assert outcome is DeferredStopOutcome.NO_PENDING_OR_RECOVERED

    await db_session.refresh(node)
    assert node.desired_state == AppiumDesiredState.stopped
    assert node.stop_pending is True
    assert node.accepting_new_sessions is False


async def test_handle_session_finished_returns_running_session_exists_under_lock(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    """Authoritative running-session check happens under the device row lock.

    Even when a caller pre-validated outside the lock, a session inserted
    between that pre-check and the locked check must be respected: the helper
    must return RUNNING_SESSION_EXISTS instead of auto-stopping.
    """
    device = Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value="lifecycle-toctou",
        connection_target="lifecycle-toctou",
        name="TOCTOU Device",
        os_version="14",
        host_id=db_host.id,
        operational_state=DeviceOperationalState.busy,
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
        lifecycle_policy_state={
            "deferred_stop": True,
            "deferred_stop_reason": "ADB not responsive",
            "deferred_stop_since": "2026-05-04T10:00:00+00:00",
            "last_action": "auto_stop_deferred",
            "last_failure_source": "device_checks",
            "last_failure_reason": "ADB not responsive",
            "recovery_suppressed_reason": None,
        },
    )
    db_session.add(device)
    await db_session.flush()
    await _append_deferred_stop(db_session, device)
    new_session = Session(
        session_id="sess-toctou-fresh",
        device_id=device.id,
        status=SessionStatus.running,
    )
    db_session.add(new_session)
    await db_session.commit()

    reloaded = await db_session.get(Device, device.id)
    assert reloaded is not None
    outcome = await _make_svc(publisher=event_bus).handle_session_finished(db_session, reloaded)
    assert outcome is DeferredStopOutcome.RUNNING_SESSION_EXISTS

    await db_session.refresh(reloaded)
    # The log must be untouched because we bailed before doing any work.
    ladder = await remediation_log.load_ladder(db_session, reloaded.id)
    assert ladder.deferred_stop_pending is True
    assert ladder.last_action == remediation_log.ACTION_AUTO_STOP_DEFERRED
    # Device must still be busy — caller (session_sync) leaves the new session in charge.
    assert reloaded.operational_state_last_emitted == DeviceOperationalState.busy


async def test_handle_session_finished_clears_intent_on_healthy_projection(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    """When derived health is healthy but ``last_failure_*`` still describes
    a recent failure, the row-derived projection is canonical.

    If the projection is wrong, the next failed probe will re-enter
    ``handle_health_failure`` and re-arm the deferred stop.
    """
    device = Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value="lifecycle-stale-healthy",
        connection_target="lifecycle-stale-healthy",
        name="Stale Healthy",
        os_version="14",
        host_id=db_host.id,
        operational_state=DeviceOperationalState.busy,
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
        lifecycle_policy_state={
            "deferred_stop": True,
            "deferred_stop_reason": "ADB hung",
            "deferred_stop_since": "2026-05-04T10:00:00+00:00",
            "last_action": "auto_stop_deferred",
            "last_failure_source": "node_health",
            "last_failure_reason": "ADB hung",
            "recovery_suppressed_reason": None,
        },
    )
    db_session.add(device)
    await db_session.flush()
    await _append_deferred_stop(db_session, device, reason="ADB hung")
    node = AppiumNode(
        device_id=device.id,
        port=4795,
        desired_state=AppiumDesiredState.running,
        desired_port=4795,
        pid=0,
        active_connection_target="",
    )
    db_session.add(node)
    await remediation_log.append_failure(
        db_session,
        device.id,
        source="node_health",
        reason="ADB hung",
    )
    await db_session.commit()

    # Health reads healthy even though last_failure_* still describes a
    # current failure. The decision is to trust the derived health projection.
    _health_svc = DeviceHealthService(publisher=event_bus)
    await _health_svc.apply_node_state_transition(
        db_session,
        device,
        health_running=None,
        health_state=None,
        mark_offline=False,
    )
    await _health_svc.update_device_checks(db_session, device, healthy=True, summary="Healthy")
    await db_session.commit()

    reloaded = await db_session.get(Device, device.id)
    assert reloaded is not None
    outcome = await _make_svc(publisher=event_bus).handle_session_finished(db_session, reloaded)
    await db_session.commit()
    assert outcome is DeferredStopOutcome.NO_PENDING_OR_RECOVERED

    await db_session.refresh(reloaded)
    assert (await remediation_log.load_ladder(db_session, reloaded.id)).deferred_stop_pending is False
    ladder = await remediation_log.load_ladder(db_session, reloaded.id)
    assert ladder.last_action == "auto_stop_cleared"
    # last_failure_* is preserved (historical) but no longer drives behavior.
    assert ladder.last_failure_reason == "ADB hung"


def test_lifecycle_run_import_order_is_acyclic() -> None:
    import importlib

    lifecycle_policy_summary_mod = importlib.import_module("app.devices.services.lifecycle_policy_summary")
    run_service = importlib.import_module("app.runs.service")

    assert hasattr(lifecycle_policy_summary_mod, "build_lifecycle_policy")
    assert hasattr(run_service, "reservation_entry_is_excluded")


async def test_suppressed_attempt_writes_no_state_and_no_incident(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    """A recovery attempt on a maintenance-held device returns False without
    writing lifecycle JSON or emitting a lifecycle_recovery_suppressed event —
    the badge is projected, and maintenance_entered already evented the cause."""
    device = await create_device(db_session, host_id=db_host.id, name="suppressed-attempt")
    locked = await device_locking.lock_device(db_session, device.id)
    set_maintenance_reason(locked, "hold")
    await db_session.commit()
    before = dict(locked.lifecycle_policy_state or {})

    locked = await device_locking.lock_device_handle(db_session, device.id)
    snapshot = await load_device_decision_snapshot(db_session, locked, packs={}, now=datetime.now(UTC))
    prepared = await _make_svc().prepare_auto_recovery_locked(
        db_session,
        locked,
        snapshot,
        generation=uuid.uuid4(),
        source="device_checks",
        reason="probe failed",
        enqueue_job=False,
    )
    await db_session.commit()
    assert prepared is False

    refreshed = await db_session.get(Device, device.id)
    assert refreshed is not None
    assert dict(refreshed.lifecycle_policy_state or {}) == before
    events = (
        (
            await db_session.execute(
                select(DeviceEvent).where(
                    DeviceEvent.device_id == device.id,
                    DeviceEvent.event_type == DeviceEventType.lifecycle_recovery_suppressed,
                )
            )
        )
        .scalars()
        .all()
    )
    assert events == []


async def test_attempt_auto_recovery_returns_false_when_projection_blocks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the availability projection reports a block, prepare_auto_recovery_locked
    stands down (returns False) without starting a node or writing state."""
    from app.devices.services.decision import DecisionFacts
    from app.devices.services.decision_snapshot import DeviceDecisionSnapshot
    from app.devices.services.state import DeviceStateFacts
    from app.lifecycle.services.remediation_log import EMPTY_LADDER

    db = AsyncMock()
    locked = SimpleNamespace(
        device=SimpleNamespace(
            id=uuid.uuid4(),
            lifecycle_policy_state={},
            review_required=False,
            review_reason=None,
            operational_state=DeviceOperationalState.offline,
            appium_node=None,
        ),
        assert_active=lambda _db: None,
    )
    snapshot = DeviceDecisionSnapshot(
        intents=(),
        has_live_session=False,
        ladder=EMPTY_LADDER,
        decision_facts=DecisionFacts(
            in_maintenance=True,
            device_checks_unhealthy=False,
            in_service=False,
            reservation_run_id=None,
            cooldown_active=False,
            cooldown_reason=None,
        ),
        state_facts=DeviceStateFacts(
            has_running_session=False,
            has_verification_lease=False,
            in_maintenance=True,
            stop_in_flight=False,
            ready=False,
        ),
        host_ip=None,
        host_agent_port=None,
        node_observed_pack_release=None,
        node_port=None,
        reservation=None,
        is_ready_for_use=False,
        review_required=False,
        review_reason=None,
        node_observed_running=False,
        recovery_generation=None,
    )

    svc = _make_svc(publisher=event_bus)
    result = await svc.prepare_auto_recovery_locked(
        db,
        locked,  # type: ignore[arg-type]
        snapshot,
        generation=uuid.uuid4(),
        source="checks",
        reason="reconnected",
        enqueue_job=False,
    )
    assert result is False


async def test_handle_health_failure_suppressed_by_maintenance_reason_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A health failure on a maintenance-held device stands down as "suppressed"
    and records no recovery-suppression incident — the maintenance fact already
    drives the projected badge and was evented by maintenance_entered."""
    db = AsyncMock()
    device = SimpleNamespace(
        id=uuid.uuid4(),
        lifecycle_policy_state={"maintenance_reason": "operator opened maintenance"},
        review_required=False,
        review_reason=None,
        operational_state=DeviceOperationalState.offline,
        appium_node=None,
    )
    monkeypatch.setattr(lifecycle_policy_module, "_reload_device", AsyncMock(return_value=device))
    append_failure = AsyncMock()
    monkeypatch.setattr(lifecycle_policy_module.remediation_log, "append_failure", append_failure)
    incident = AsyncMock()
    monkeypatch.setattr(LifecycleIncidentService, "record_lifecycle_incident", incident)
    locked = SimpleNamespace(device=device, assert_active=lambda _db: None)
    monkeypatch.setattr(lifecycle_policy_module.device_locking, "lock_device_handle", AsyncMock(return_value=locked))
    from app.devices.services.decision import DecisionFacts
    from app.devices.services.decision_snapshot import DeviceDecisionSnapshot
    from app.devices.services.state import DeviceStateFacts
    from app.lifecycle.services.remediation_log import EMPTY_LADDER

    snapshot = DeviceDecisionSnapshot(
        intents=(),
        has_live_session=False,
        ladder=EMPTY_LADDER,
        decision_facts=DecisionFacts(
            in_maintenance=True,
            device_checks_unhealthy=False,
            in_service=False,
            reservation_run_id=None,
            cooldown_active=False,
            cooldown_reason=None,
        ),
        state_facts=DeviceStateFacts(
            has_running_session=False,
            has_verification_lease=False,
            in_maintenance=True,
            stop_in_flight=False,
            ready=False,
        ),
        host_ip=None,
        host_agent_port=None,
        node_observed_pack_release=None,
        node_port=None,
        reservation=None,
        is_ready_for_use=False,
        review_required=False,
        review_reason=None,
        node_observed_running=False,
        recovery_generation=None,
    )
    monkeypatch.setattr(
        lifecycle_policy_module,
        "load_device_decision_snapshot",
        AsyncMock(return_value=snapshot),
    )

    svc = _make_svc(publisher=event_bus)
    assert await svc.handle_health_failure(db, device, source="checks", reason="bad") == "suppressed"
    incident.assert_not_awaited()
    # The failure trail is still written for observability.
    append_failure.assert_awaited_once_with(db, device.id, source="checks", reason="bad")


async def test_attempt_auto_recovery_rejoin_and_busy_autostop_success_branches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Migrated: the rejoin (excluded reservation restored) and busy-autostop
    # (no reservation, already-excluded) success branches are now covered by
    # finalize_auto_recovery_locked's "recovered" path (test_successful_recovery_rejoins_run)
    # and the prepare path (Task 4). The old attempt_auto_recovery orchestration
    # that this test exercised (reload → guards → start node → wait → probe →
    # finalize) is split across prepare_auto_recovery_locked + the worker +
    # finalize_auto_recovery_locked, each covered by targeted tests.
    pass


async def test_attempt_auto_recovery_records_backoff_when_restart_cannot_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Migrated: the node-start-failure backoff path now lives in
    # prepare_auto_recovery_locked (candidate_ports failure → escalate →
    # backoff incident pair → clear generation → return False), covered by
    # Task 4's prepare_auto_recovery_locked tests.
    pass


async def test_attempt_auto_recovery_start_and_probe_outcomes(monkeypatch: pytest.MonkeyPatch) -> None:
    # Migrated: the wait-before-probe ordering is now structurally enforced by
    # the worker's phase ordering (_wait_for_node_running then _run_probe), and
    # the success/failure finalize branches are covered by
    # test_successful_recovery_rejoins_run and test_failed_recovery_sets_backoff_and_keeps_exclusion.
    pass


async def test_node_start_failure_promotes_to_review_at_threshold(db_session: AsyncSession, db_host: Host) -> None:
    # Migrated: node-start-failure review promotion now lives in
    # prepare_auto_recovery_locked's candidate_ports failure path, which calls
    # escalate_remediation_failure (the same shared ladder that promotes to
    # review at threshold). Covered by Task 4's prepare tests.
    pass


# ---------------------------------------------------------------------------
# restore_run_after_self_heal — close the restore-gap where a recovered device
# returns to ``available`` without auto-recovery firing, leaving the no-TTL
# health_failure:reservation intent (and thus the run exclusion) stuck.
# ---------------------------------------------------------------------------


async def _reservation_row(db_session: AsyncSession, device_id: object) -> DeviceReservation:
    return (
        await db_session.execute(
            select(DeviceReservation).where(
                DeviceReservation.device_id == device_id,
                DeviceReservation.released_at.is_(None),
            )
        )
    ).scalar_one()


async def _exclude_reservation(
    db_session: AsyncSession,
    *,
    device_id: object,
    run_id: object,
    reason: str = "Failed checks: ping, ecp",
    excluded_until: object = None,
) -> None:
    """Mark the reservation excluded on the row (no intent axis anymore)."""
    res = await _reservation_row(db_session, device_id)
    res.excluded = True
    res.exclusion_kind = ExclusionKind.cooldown if excluded_until is not None else ExclusionKind.exclusion
    res.exclusion_reason = reason
    res.excluded_at = datetime.now(UTC)
    res.excluded_until = excluded_until
    await db_session.commit()


async def test_restore_run_after_self_heal_clears_health_failure_exclusion(
    db_session: AsyncSession, db_host: Host
) -> None:
    device = await create_device(
        db_session, host_id=db_host.id, name="self-heal-restore", operational_state=DeviceOperationalState.available
    )
    run = await create_reserved_run(db_session, name="self-heal-restore-run", devices=[device])
    await _exclude_reservation(db_session, device_id=device.id, run_id=run.id)

    restored = await _make_svc(publisher=event_bus).restore_run_after_self_heal(
        db_session, device, reason="Device healthy after self-heal"
    )

    assert restored is True
    res = await _reservation_row(db_session, device.id)
    assert res.excluded is False
    assert res.exclusion_reason is None


async def test_restore_run_after_self_heal_leaves_cooldown_exclusion(db_session: AsyncSession, db_host: Host) -> None:
    device = await create_device(
        db_session, host_id=db_host.id, name="self-heal-cooldown", operational_state=DeviceOperationalState.available
    )
    run = await create_reserved_run(db_session, name="self-heal-cooldown-run", devices=[device])
    await _exclude_reservation(
        db_session,
        device_id=device.id,
        run_id=run.id,
        excluded_until=datetime.now(UTC) + timedelta(hours=1),
    )

    restored = await _make_svc(publisher=event_bus).restore_run_after_self_heal(
        db_session, device, reason="Device healthy after self-heal"
    )

    assert restored is False
    res = await _reservation_row(db_session, device.id)
    assert res.excluded is True


async def test_restore_run_after_self_heal_ignores_released_device(db_session: AsyncSession, db_host: Host) -> None:
    device = await create_device(
        db_session, host_id=db_host.id, name="self-heal-released", operational_state=DeviceOperationalState.available
    )
    await create_reserved_run(db_session, name="self-heal-released-run", devices=[device])
    # Release the device from the run (the escalation mechanism) — real services, real reconcile.
    locked = await device_locking.lock_device_handle(db_session, device.id)
    await RunReservationService(review=build_review_service()).release_locked(
        db_session, locked, reason="CI preparation failed", publisher=event_bus
    )
    await db_session.commit()

    # The self-heal loop must NOT rejoin a released device.
    restored = await _make_svc(publisher=event_bus).restore_run_after_self_heal(
        db_session, device, reason="Device healthy after self-heal"
    )

    assert restored is False
    active = (
        await db_session.execute(
            select(DeviceReservation).where(
                DeviceReservation.device_id == device.id,
                DeviceReservation.released_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    assert active is None


async def test_restore_run_after_self_heal_skips_non_available_device(db_session: AsyncSession, db_host: Host) -> None:
    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="self-heal-offline",
        operational_state=DeviceOperationalState.offline,
        verified=False,
    )
    run = await create_reserved_run(db_session, name="self-heal-offline-run", devices=[device])
    await _exclude_reservation(db_session, device_id=device.id, run_id=run.id)

    restored = await _make_svc(publisher=event_bus).restore_run_after_self_heal(
        db_session, device, reason="Device healthy after self-heal"
    )

    assert restored is False
    res = await _reservation_row(db_session, device.id)
    assert res.excluded is True
