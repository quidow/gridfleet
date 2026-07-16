from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, Mock

import pytest
import pytest_asyncio
from sqlalchemy import event, func, select

from app.appium_nodes.models import AppiumDesiredState, AppiumNode
from app.devices import locking as device_locking
from app.devices.models import Device, DeviceEvent, DeviceOperationalState, DeviceReservation
from app.devices.models.remediation_log import DeviceRemediationLogEntry
from app.devices.services.connectivity import ConnectivityService
from app.devices.services.health import DeviceHealthService
from app.devices.services.review import ReviewService
from app.events.models import SystemEvent
from app.hosts.service_status_push import OBSERVATION_REVISION_KEY
from app.lifecycle.services.actions import LifecyclePolicyActionsService
from app.lifecycle.services.incidents import LifecycleIncidentService
from app.lifecycle.services.policy import LifecyclePolicyService
from app.runs.models import RunState, TestRun
from app.runs.service_reservation import RunReservationService
from tests.bench_instrumentation import CommitTap, install_async_session_callsite_profiler
from tests.fakes import FakeSettingsReader
from tests.helpers import create_device, create_reserved_run, seed_host_and_device, settle_after_commit_tasks
from tests.helpers import test_event_bus as event_bus

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

pytestmark = pytest.mark.usefixtures("seeded_driver_packs")

_OBSERVED_AT = datetime(2026, 7, 17, 12, tzinfo=UTC)
_REVISION = 1_000_000_000
_SECTION_SEQUENCE = 7
_BOOT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


@pytest_asyncio.fixture
async def unhealthy_fold(
    db_session: AsyncSession,
) -> tuple[ConnectivityService, Device, AppiumNode, dict[str, Any]]:
    _host, device = await seed_host_and_device(db_session, identity="unhealthy-transition")
    device.device_checks_healthy = True
    device.device_checks_summary = "Healthy"
    device.device_checks_checked_at = _OBSERVED_AT - timedelta(minutes=1)
    device.device_checks_observation_revision = 1
    node = AppiumNode(
        device_id=device.id,
        port=4723,
        desired_state=AppiumDesiredState.running,
        desired_port=4723,
        pid=1000,
        active_connection_target=device.identity_value,
        health_running=True,
        last_health_checked_at=_OBSERVED_AT - timedelta(minutes=1),
        last_observed_at=_OBSERVED_AT - timedelta(minutes=1),
    )
    db_session.add(node)
    await db_session.commit()

    review = ReviewService()
    incidents = LifecycleIncidentService(publisher=event_bus)
    reservation = RunReservationService(review=review)
    actions = LifecyclePolicyActionsService(
        publisher=event_bus,
        reservation=reservation,
        incidents=incidents,
    )
    lifecycle = LifecyclePolicyService(
        publisher=event_bus,
        settings=FakeSettingsReader({}),
        actions=actions,
        incidents=incidents,
        viability=AsyncMock(),
        node_manager=AsyncMock(),
        review=review,
    )
    service = ConnectivityService(
        publisher=event_bus,
        settings=FakeSettingsReader({}),
        circuit_breaker=Mock(),
        lifecycle_policy=lifecycle,
        health=DeviceHealthService(publisher=event_bus),
    )
    section = {
        "reported_at": _OBSERVED_AT.isoformat(),
        "section_sequence": _SECTION_SEQUENCE,
        OBSERVATION_REVISION_KEY: _REVISION,
        "complete_gather": True,
        "devices": [
            {
                "device_id": str(device.id),
                "probe_status": "observed",
                "presence": "present",
                "health": {"healthy": False, "checks": []},
                "lifecycle_state": {"status": "unsupported", "value": None},
            }
        ],
    }
    return service, device, node, section


async def test_unhealthy_fold_preserves_transition_artifacts_and_order(
    db_session: AsyncSession,
    unhealthy_fold: tuple[ConnectivityService, Device, AppiumNode, dict[str, Any]],
) -> None:
    service, device, node, section = unhealthy_fold
    before_counts = (
        await db_session.scalar(
            select(func.count()).select_from(DeviceEvent).where(DeviceEvent.device_id == device.id)
        ),
        await db_session.scalar(
            select(func.count())
            .select_from(SystemEvent)
            .where(SystemEvent.data.contains({"device_id": str(device.id)}))
        ),
    )
    assert before_counts == (0, 0)

    assert await service.fold_host_devices(db_session, device.host_id, section, boot_id=_BOOT_ID) is True
    await settle_after_commit_tasks()

    device_events = (
        (
            await db_session.execute(
                select(DeviceEvent)
                .where(DeviceEvent.device_id == device.id)
                .order_by(DeviceEvent.created_at, DeviceEvent.id)
            )
        )
        .scalars()
        .all()
    )
    system_events = (
        (
            await db_session.execute(
                select(SystemEvent)
                .where(SystemEvent.data.contains({"device_id": str(device.id)}))
                .order_by(SystemEvent.id)
            )
        )
        .scalars()
        .all()
    )
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
    await db_session.refresh(device)
    await db_session.refresh(node)

    assert [(row.event_type.value, row.details) for row in device_events] == [
        (
            "desired_state_changed",
            {
                "field": "accepting_new_sessions",
                "old_value": True,
                "new_value": False,
                "caller": "intent_reconciler",
                "reason": "no reservation routing",
            },
        ),
        (
            "desired_state_changed",
            {
                "field": "stop_pending",
                "old_value": False,
                "new_value": True,
                "caller": "intent_reconciler",
                "reason": "connectivity park",
            },
        ),
        (
            "health_check_fail",
            {"source": "device_checks", "reason": "Device health checks failed"},
        ),
        (
            "desired_state_changed",
            {
                "old_desired_state": "running",
                "new_desired_state": "stopped",
                "desired_port": None,
                "restart_requested_at": None,
                "caller": "intent_reconciler",
                "actor": None,
                "reason": "Device health checks failed",
            },
        ),
        (
            "lifecycle_auto_stopped",
            {
                "summary_state": "recoverable",
                "reason": "Device health checks failed",
                "detail": "Manager stopped the device automatically after a lifecycle failure",
                "source": "device_checks",
            },
        ),
    ]
    assert [(row.type, {**row.data, "device_id": "<device-id>"}, row.severity) for row in system_events] == [
        (
            "device.operational_state_changed",
            {
                "device_id": "<device-id>",
                "device_name": "Device unhealthy-transition",
                "old_operational_state": "available",
                "new_operational_state": "offline",
            },
            "warning",
        ),
        (
            "device.health_changed",
            {
                "device_id": "<device-id>",
                "overall": "failed",
                "device": {
                    "status": "failed",
                    "detail": "Device health checks failed",
                    "checked_at": "2026-07-17T12:00:00+00:00",
                },
                "node": {
                    "status": "ok",
                    "detail": "running",
                    "checked_at": "2026-07-17T11:59:00+00:00",
                },
                "viability": {"status": "unknown", "detail": "not run", "checked_at": None},
            },
            "info",
        ),
        (
            "device.lifecycle_incident",
            {
                "device_id": "<device-id>",
                "device_name": "Device unhealthy-transition",
                "event_type": "lifecycle_auto_stopped",
                "label": "Auto-Stopped",
                "summary_state": "recoverable",
                "reason": "Device health checks failed",
                "detail": "Manager stopped the device automatically after a lifecycle failure",
                "source": "device_checks",
                "run_id": None,
                "run_name": None,
            },
            "warning",
        ),
    ]
    assert [(row.kind, row.source, row.action, row.reason, row.backoff_until, "<timestamp>") for row in history] == [
        (
            "failure",
            "device_checks",
            "failure_observed",
            "Device health checks failed",
            None,
            "<timestamp>",
        ),
        (
            "action",
            "device_checks",
            "auto_stop_commissioned",
            "Device health checks failed",
            None,
            "<timestamp>",
        ),
        (
            "action",
            "device_checks",
            "auto_stopped",
            "Device health checks failed",
            None,
            "<timestamp>",
        ),
    ]
    assert isinstance(device.failure_episode_id, uuid.UUID)
    assert (
        device.device_checks_healthy,
        device.device_checks_summary,
        device.device_checks_checked_at,
        device.device_checks_observation_revision,
        "<failure-episode-id>",
    ) == (False, "Device health checks failed", _OBSERVED_AT, _REVISION, "<failure-episode-id>")
    assert (
        node.desired_state,
        node.desired_port,
        node.accepting_new_sessions,
        node.stop_pending,
        node.restart_requested_at,
    ) == (AppiumDesiredState.stopped, None, False, True, None)
    assert (
        device.device_checks_fold_applied_revision,
        device.device_checks_fold_boot_id,
        device.device_checks_fold_section_sequence,
    ) == (_REVISION, _BOOT_ID, _SECTION_SEQUENCE)
    first_delivery_counts = (len(device_events), len(system_events))
    assert first_delivery_counts == (5, 3)

    assert await service.fold_host_devices(db_session, device.host_id, section, boot_id=_BOOT_ID) is True
    await settle_after_commit_tasks()
    redelivery_counts = (
        await db_session.scalar(
            select(func.count()).select_from(DeviceEvent).where(DeviceEvent.device_id == device.id)
        ),
        await db_session.scalar(
            select(func.count())
            .select_from(SystemEvent)
            .where(SystemEvent.data.contains({"device_id": str(device.id)}))
        ),
    )
    assert redelivery_counts == first_delivery_counts


async def test_unhealthy_fold_rolls_back_every_artifact_when_lifecycle_fails(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    unhealthy_fold: tuple[ConnectivityService, Device, AppiumNode, dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, device, node, section = unhealthy_fold
    device_id = device.id
    host_id = device.host_id
    restart_watermark = _OBSERVED_AT - timedelta(seconds=30)
    node.restart_requested_at = restart_watermark
    run = await create_reserved_run(db_session, name="rollback-active-run", devices=[device])
    run_id = run.id
    original = LifecyclePolicyActionsService.record_auto_stopped_incident

    async def fail_after_mutations(
        self: LifecyclePolicyActionsService,
        db: AsyncSession,
        target: Device,
        *,
        run: TestRun | None,
        reason: str,
        source: str,
        detail: str,
    ) -> None:
        await original(self, db, target, run=run, reason=reason, source=source, detail=detail)
        raise RuntimeError("sentinel lifecycle failure")

    monkeypatch.setattr(LifecyclePolicyActionsService, "record_auto_stopped_incident", fail_after_mutations)

    assert await service.fold_host_devices(db_session, host_id, section, boot_id=_BOOT_ID) is False
    await settle_after_commit_tasks()

    async with db_session_maker() as verify:
        rolled_back = await verify.get(Device, device_id)
        rolled_back_node = (
            await verify.execute(select(AppiumNode).where(AppiumNode.device_id == device_id))
        ).scalar_one()
        rolled_back_run = await verify.get(TestRun, run_id)
        rolled_back_reservation = (
            await verify.execute(select(DeviceReservation).where(DeviceReservation.device_id == device_id))
        ).scalar_one()
        assert rolled_back is not None
        assert rolled_back_run is not None
        assert (
            rolled_back.device_checks_healthy,
            rolled_back.device_checks_summary,
            rolled_back.device_checks_checked_at,
            rolled_back.device_checks_observation_revision,
            rolled_back.failure_episode_id,
        ) == (True, "Healthy", _OBSERVED_AT - timedelta(minutes=1), 1, None)
        assert (
            rolled_back_node.desired_state,
            rolled_back_node.desired_port,
            rolled_back_node.accepting_new_sessions,
            rolled_back_node.stop_pending,
            rolled_back_node.restart_requested_at,
        ) == (AppiumDesiredState.running, 4723, True, False, restart_watermark)
        assert rolled_back.operational_state_last_emitted == DeviceOperationalState.available
        assert rolled_back.lifecycle_policy_state == {}
        assert rolled_back_run.state == RunState.active
        assert (
            rolled_back_reservation.excluded,
            rolled_back_reservation.exclusion_kind,
            rolled_back_reservation.exclusion_reason,
            rolled_back_reservation.excluded_at,
            rolled_back_reservation.excluded_until,
        ) == (False, None, None, None, None)
        assert (
            rolled_back.device_checks_fold_applied_revision,
            rolled_back.device_checks_fold_boot_id,
            rolled_back.device_checks_fold_section_sequence,
        ) == (0, None, None)
        assert (
            await verify.scalar(
                select(func.count())
                .select_from(DeviceRemediationLogEntry)
                .where(DeviceRemediationLogEntry.device_id == device_id)
            )
            == 0
        )
        assert (
            await verify.scalar(select(func.count()).select_from(DeviceEvent).where(DeviceEvent.device_id == device_id))
            == 0
        )
        assert (
            await verify.scalar(
                select(func.count())
                .select_from(SystemEvent)
                .where(SystemEvent.data.contains({"device_id": str(device_id)}))
            )
            == 0
        )

    monkeypatch.setattr(LifecyclePolicyActionsService, "record_auto_stopped_incident", original)
    assert await service.fold_host_devices(db_session, host_id, section, boot_id=_BOOT_ID) is True
    await settle_after_commit_tasks()

    async with db_session_maker() as verify:
        applied = await verify.get(Device, device_id)
        applied_node = (await verify.execute(select(AppiumNode).where(AppiumNode.device_id == device_id))).scalar_one()
        applied_run = await verify.get(TestRun, run_id)
        applied_reservation = (
            await verify.execute(select(DeviceReservation).where(DeviceReservation.device_id == device_id))
        ).scalar_one()
        assert applied is not None
        assert applied_run is not None
        assert (
            applied.device_checks_fold_applied_revision,
            applied.device_checks_fold_boot_id,
            applied.device_checks_fold_section_sequence,
        ) == (_REVISION, _BOOT_ID, _SECTION_SEQUENCE)
        assert (applied_node.desired_state, applied_node.restart_requested_at) == (AppiumDesiredState.stopped, None)
        assert applied_run.state == RunState.active
        assert (
            applied_reservation.excluded,
            applied_reservation.exclusion_kind,
            applied_reservation.exclusion_reason,
            applied_reservation.excluded_until,
        ) == (True, "exclusion", "Device health checks failed", None)
        first_device_events = list(
            (
                await verify.execute(
                    select(DeviceEvent.event_type)
                    .where(DeviceEvent.device_id == device_id)
                    .order_by(DeviceEvent.created_at, DeviceEvent.id)
                )
            ).scalars()
        )
        first_system_events = list(
            (
                await verify.execute(
                    select(SystemEvent.type)
                    .where(SystemEvent.data.contains({"device_id": str(device_id)}))
                    .order_by(SystemEvent.id)
                )
            ).scalars()
        )
        first_history = list(
            (
                await verify.execute(
                    select(DeviceRemediationLogEntry.action)
                    .where(DeviceRemediationLogEntry.device_id == device_id)
                    .order_by(DeviceRemediationLogEntry.at, DeviceRemediationLogEntry.id)
                )
            ).scalars()
        )
    assert [event_type.value for event_type in first_device_events] == [
        "desired_state_changed",
        "desired_state_changed",
        "desired_state_changed",
        "desired_state_changed",
        "desired_state_changed",
        "lifecycle_run_excluded",
        "health_check_fail",
        "desired_state_changed",
        "lifecycle_auto_stopped",
    ]
    assert first_system_events == [
        "device.operational_state_changed",
        "device.health_changed",
        "device.lifecycle_incident",
        "device.lifecycle_incident",
    ]
    assert first_history == ["failure_observed", "auto_stop_commissioned", "auto_stopped"]

    assert await service.fold_host_devices(db_session, host_id, section, boot_id=_BOOT_ID) is True
    await settle_after_commit_tasks()
    async with db_session_maker() as verify:
        redelivered_node = (
            await verify.execute(select(AppiumNode).where(AppiumNode.device_id == device_id))
        ).scalar_one()
        redelivered_reservation = (
            await verify.execute(select(DeviceReservation).where(DeviceReservation.device_id == device_id))
        ).scalar_one()
        assert await verify.scalar(
            select(func.count()).select_from(DeviceEvent).where(DeviceEvent.device_id == device_id)
        ) == len(first_device_events)
        assert await verify.scalar(
            select(func.count())
            .select_from(SystemEvent)
            .where(SystemEvent.data.contains({"device_id": str(device_id)}))
        ) == len(first_system_events)
        assert await verify.scalar(
            select(func.count())
            .select_from(DeviceRemediationLogEntry)
            .where(DeviceRemediationLogEntry.device_id == device_id)
        ) == len(first_history)
        assert (redelivered_node.desired_state, redelivered_node.restart_requested_at) == (
            AppiumDesiredState.stopped,
            None,
        )
        assert (
            redelivered_reservation.excluded,
            redelivered_reservation.exclusion_kind,
            redelivered_reservation.exclusion_reason,
        ) == (True, "exclusion", "Device health checks failed")


async def test_unhealthy_fold_failure_keeps_prior_device_and_retries_remaining_devices(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    unhealthy_fold: tuple[ConnectivityService, Device, AppiumNode, dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, seeded_device, _seeded_node, section = unhealthy_fold
    devices = [seeded_device]
    for suffix in ("a", "b"):
        device = await create_device(
            db_session,
            host_id=seeded_device.host_id,
            name=f"unhealthy-transition-{suffix}",
            operational_state=DeviceOperationalState.available,
        )
        device.device_checks_healthy = True
        device.device_checks_summary = "Healthy"
        device.device_checks_checked_at = _OBSERVED_AT - timedelta(minutes=1)
        device.device_checks_observation_revision = 1
        db_session.add(
            AppiumNode(
                device_id=device.id,
                port=4723,
                desired_state=AppiumDesiredState.running,
                desired_port=4723,
                pid=1000,
                active_connection_target=device.identity_value,
                health_running=True,
                last_health_checked_at=_OBSERVED_AT - timedelta(minutes=1),
                last_observed_at=_OBSERVED_AT - timedelta(minutes=1),
            )
        )
        devices.append(device)
    await db_session.commit()
    ordered = sorted(devices, key=lambda item: item.id)
    first_id, middle_id, final_id = (item.id for item in ordered)
    host_id = seeded_device.host_id
    section["devices"] = [
        {
            "device_id": str(item.id),
            "probe_status": "observed",
            "presence": "present",
            "health": {"healthy": False, "checks": []},
            "lifecycle_state": {"status": "unsupported", "value": None},
        }
        for item in ordered
    ]
    original = LifecyclePolicyActionsService.record_auto_stopped_incident

    async def fail_middle(
        self: LifecyclePolicyActionsService,
        db: AsyncSession,
        target: Device,
        *,
        run: TestRun | None,
        reason: str,
        source: str,
        detail: str,
    ) -> None:
        await original(self, db, target, run=run, reason=reason, source=source, detail=detail)
        if target.id == middle_id:
            raise RuntimeError("sentinel middle-device failure")

    monkeypatch.setattr(LifecyclePolicyActionsService, "record_auto_stopped_incident", fail_middle)
    assert await service.fold_host_devices(db_session, host_id, section, boot_id=_BOOT_ID) is False
    await settle_after_commit_tasks()

    async with db_session_maker() as verify:
        after_failure = {
            item.id: item
            for item in (
                await verify.execute(select(Device).where(Device.id.in_((first_id, middle_id, final_id))))
            ).scalars()
        }
        event_counts_after_failure = {
            device_id: await verify.scalar(
                select(func.count()).select_from(DeviceEvent).where(DeviceEvent.device_id == device_id)
            )
            for device_id in (first_id, middle_id, final_id)
        }
        system_event_counts_after_failure = {
            device_id: await verify.scalar(
                select(func.count())
                .select_from(SystemEvent)
                .where(SystemEvent.data.contains({"device_id": str(device_id)}))
            )
            for device_id in (first_id, middle_id, final_id)
        }
        history_counts_after_failure = {
            device_id: await verify.scalar(
                select(func.count())
                .select_from(DeviceRemediationLogEntry)
                .where(DeviceRemediationLogEntry.device_id == device_id)
            )
            for device_id in (first_id, middle_id, final_id)
        }
        nodes_after_failure = {
            node.device_id: node
            for node in (
                await verify.execute(
                    select(AppiumNode).where(AppiumNode.device_id.in_((first_id, middle_id, final_id)))
                )
            ).scalars()
        }
    assert event_counts_after_failure == {first_id: 5, middle_id: 0, final_id: 5}
    assert system_event_counts_after_failure == {first_id: 3, middle_id: 0, final_id: 3}
    assert history_counts_after_failure == {first_id: 3, middle_id: 0, final_id: 3}
    assert after_failure[first_id].device_checks_fold_applied_revision == _REVISION
    assert after_failure[middle_id].device_checks_fold_applied_revision == 0
    assert after_failure[middle_id].device_checks_healthy is True
    assert nodes_after_failure[middle_id].desired_state == AppiumDesiredState.running
    assert after_failure[final_id].device_checks_fold_applied_revision == _REVISION

    monkeypatch.setattr(LifecyclePolicyActionsService, "record_auto_stopped_incident", original)
    assert await service.fold_host_devices(db_session, host_id, section, boot_id=_BOOT_ID) is True
    await settle_after_commit_tasks()

    async with db_session_maker() as verify:
        completed = {
            item.id: item
            for item in (
                await verify.execute(select(Device).where(Device.id.in_((first_id, middle_id, final_id))))
            ).scalars()
        }
        completed_event_counts = {
            device_id: await verify.scalar(
                select(func.count()).select_from(DeviceEvent).where(DeviceEvent.device_id == device_id)
            )
            for device_id in (first_id, middle_id, final_id)
        }
        completed_system_event_counts = {
            device_id: await verify.scalar(
                select(func.count())
                .select_from(SystemEvent)
                .where(SystemEvent.data.contains({"device_id": str(device_id)}))
            )
            for device_id in (first_id, middle_id, final_id)
        }
        completed_history_counts = {
            device_id: await verify.scalar(
                select(func.count())
                .select_from(DeviceRemediationLogEntry)
                .where(DeviceRemediationLogEntry.device_id == device_id)
            )
            for device_id in (first_id, middle_id, final_id)
        }
    assert completed_event_counts == {first_id: 5, middle_id: 5, final_id: 5}
    assert completed_system_event_counts == {first_id: 3, middle_id: 3, final_id: 3}
    assert completed_history_counts == {first_id: 3, middle_id: 3, final_id: 3}
    assert all(item.device_checks_fold_applied_revision == _REVISION for item in completed.values())


async def test_unhealthy_fold_uses_one_commit_and_no_general_device_relock(
    db_session: AsyncSession,
    unhealthy_fold: tuple[ConnectivityService, Device, AppiumNode, dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, device, node, section = unhealthy_fold
    install_async_session_callsite_profiler(monkeypatch)
    commit_tap = CommitTap()
    lock_device_spy = AsyncMock(wraps=device_locking.lock_device)
    monkeypatch.setattr(device_locking, "lock_device", lock_device_spy)
    engine = db_session.bind.sync_engine
    event.listen(engine, "commit", commit_tap)
    try:
        assert await service.fold_host_devices(db_session, device.host_id, section, boot_id=_BOOT_ID) is True
        await settle_after_commit_tasks()
    finally:
        event.remove(engine, "commit", commit_tap)

    assert commit_tap.source_count == 1
    lock_device_spy.assert_not_awaited()
    device_events = list(
        (
            await db_session.execute(
                select(DeviceEvent.event_type)
                .where(DeviceEvent.device_id == device.id)
                .order_by(DeviceEvent.created_at, DeviceEvent.id)
            )
        ).scalars()
    )
    system_event_types = list(
        (
            await db_session.execute(
                select(SystemEvent.type)
                .where(SystemEvent.data.contains({"device_id": str(device.id)}))
                .order_by(SystemEvent.id)
            )
        ).scalars()
    )
    history = list(
        (
            await db_session.execute(
                select(DeviceRemediationLogEntry.action)
                .where(DeviceRemediationLogEntry.device_id == device.id)
                .order_by(DeviceRemediationLogEntry.at, DeviceRemediationLogEntry.id)
            )
        ).scalars()
    )
    await db_session.refresh(device)
    await db_session.refresh(node)

    assert [event_type.value for event_type in device_events] == [
        "desired_state_changed",
        "desired_state_changed",
        "health_check_fail",
        "desired_state_changed",
        "lifecycle_auto_stopped",
    ]
    assert system_event_types == [
        "device.operational_state_changed",
        "device.health_changed",
        "device.lifecycle_incident",
    ]
    assert history == ["failure_observed", "auto_stop_commissioned", "auto_stopped"]
    assert (
        device.device_checks_healthy,
        device.device_checks_summary,
        device.device_checks_checked_at,
        device.device_checks_observation_revision,
        device.device_checks_fold_applied_revision,
        device.device_checks_fold_boot_id,
        device.device_checks_fold_section_sequence,
    ) == (False, "Device health checks failed", _OBSERVED_AT, _REVISION, _REVISION, _BOOT_ID, _SECTION_SEQUENCE)
    assert (
        node.desired_state,
        node.desired_port,
        node.accepting_new_sessions,
        node.stop_pending,
        node.restart_requested_at,
    ) == (AppiumDesiredState.stopped, None, False, True, None)
