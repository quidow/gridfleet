from __future__ import annotations

import uuid
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

from sqlalchemy import inspect as sa_inspect
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.appium_nodes.models import AppiumDesiredState, AppiumNode
from app.appium_nodes.services import resource_service as appium_node_resource_service
from app.core.leader import state_store
from app.devices.models import Device
from app.devices.models.event import DeviceEvent, DeviceEventType
from app.devices.services.health import DeviceHealthService
from app.devices.services.link_repair import REPAIR_ATTEMPTS_NAMESPACE, REPAIR_MAX_ATTEMPTS
from app.devices.services.remediation import enqueue_device_health_remediation
from app.devices.services.remediation_job import RemediationJobService
from app.jobs import JOB_KIND_DEVICE_HEALTH_REMEDIATION
from app.jobs.models import Job
from app.jobs.statuses import JOB_STATUS_COMPLETED
from app.sessions.models import Session, SessionStatus
from tests.helpers import create_device
from tests.helpers import test_event_bus as event_bus

if TYPE_CHECKING:
    from app.hosts.models import Host


def _session_factory(db_session: AsyncSession) -> async_sessionmaker[AsyncSession]:
    assert db_session.bind is not None
    return async_sessionmaker(db_session.bind, class_=AsyncSession, expire_on_commit=False)


async def _create_failing_remediation_job(
    db_session: AsyncSession,
    host: Host,
    *,
    name: str,
    action_id: str = "reconnect",
) -> tuple[Device, uuid.UUID, uuid.UUID]:
    device = await create_device(db_session, host_id=host.id, name=name)
    failure_episode_id = uuid.uuid4()
    device.device_checks_healthy = False
    device.failure_episode_id = failure_episode_id
    await db_session.commit()
    job_id = await enqueue_device_health_remediation(
        db_session,
        device_id=device.id,
        failure_episode_id=failure_episode_id,
        action_id=action_id,
        commit=True,
    )
    assert job_id is not None
    return device, failure_episode_id, job_id


def test_device_health_remediation_schema_contract() -> None:
    assert JOB_KIND_DEVICE_HEALTH_REMEDIATION == "device_health_remediation"
    assert {
        "remediation_device_id",
        "failure_episode_id",
        "remediation_action_id",
    } <= {column.key for column in sa_inspect(Job).columns}
    assert "failure_episode_id" in {column.key for column in sa_inspect(Device).columns}


async def test_worker_self_cancels_when_device_healthy(db_session: AsyncSession, db_host: Host) -> None:
    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="healthy-remediation-device",
    )
    device.device_checks_healthy = True
    device.failure_episode_id = None
    await db_session.commit()

    old_episode_id = uuid.uuid4()
    job_id = await enqueue_device_health_remediation(
        db_session,
        device_id=device.id,
        failure_episode_id=old_episode_id,
        action_id="reconnect",
        commit=True,
    )
    assert job_id is not None

    dispatch = AsyncMock()
    with patch(
        "app.devices.services.remediation_job.link_repair.dispatch_recommended_action",
        new=dispatch,
    ):
        await RemediationJobService(
            session_factory=_session_factory(db_session),
            circuit_breaker=AsyncMock(),
            health=AsyncMock(),
        ).run_device_health_remediation_job(
            str(job_id),
            {
                "device_id": str(device.id),
                "failure_episode_id": str(old_episode_id),
                "action_id": "reconnect",
            },
        )

    dispatch.assert_not_awaited()
    db_session.expire_all()
    job = await db_session.get(Job, job_id)
    assert job is not None
    assert job.status == JOB_STATUS_COMPLETED
    assert any(word in str(job.snapshot.get("note", "")).lower() for word in ("recovered", "superseded"))


async def test_worker_self_cancels_when_device_enters_maintenance(db_session: AsyncSession, db_host: Host) -> None:
    device, failure_episode_id, job_id = await _create_failing_remediation_job(
        db_session,
        db_host,
        name="maintenance-remediation-device",
    )
    device.lifecycle_policy_state = {"maintenance_reason": "operator hold"}
    await db_session.commit()

    dispatch = AsyncMock()
    with patch(
        "app.devices.services.remediation_job.link_repair.dispatch_recommended_action",
        new=dispatch,
    ):
        await RemediationJobService(
            session_factory=_session_factory(db_session),
            circuit_breaker=AsyncMock(),
            health=AsyncMock(),
        ).run_device_health_remediation_job(
            str(job_id),
            {
                "device_id": str(device.id),
                "failure_episode_id": str(failure_episode_id),
                "action_id": "reconnect",
            },
        )

    dispatch.assert_not_awaited()
    db_session.expire_all()
    job = await db_session.get(Job, job_id)
    assert job is not None
    assert job.status == JOB_STATUS_COMPLETED
    assert "maintenance" in str(job.snapshot.get("note", "")).lower()


async def test_worker_completes_when_device_no_longer_exists(db_session: AsyncSession) -> None:
    missing_device_id = uuid.uuid4()
    failure_episode_id = uuid.uuid4()
    job_id = await enqueue_device_health_remediation(
        db_session,
        device_id=missing_device_id,
        failure_episode_id=failure_episode_id,
        action_id="reconnect",
        commit=True,
    )
    assert job_id is not None

    dispatch = AsyncMock()
    with patch(
        "app.devices.services.remediation_job.link_repair.dispatch_recommended_action",
        new=dispatch,
    ):
        await RemediationJobService(
            session_factory=_session_factory(db_session),
            circuit_breaker=AsyncMock(),
            health=AsyncMock(),
        ).run_device_health_remediation_job(
            str(job_id),
            {
                "device_id": str(missing_device_id),
                "failure_episode_id": str(failure_episode_id),
                "action_id": "reconnect",
            },
        )

    dispatch.assert_not_awaited()
    db_session.expire_all()
    job = await db_session.get(Job, job_id)
    assert job is not None
    assert job.status == JOB_STATUS_COMPLETED
    assert job.snapshot.get("note") == "device no longer exists"


async def test_worker_dispatches_and_records_repair_attempt(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    device, failure_episode_id, job_id = await _create_failing_remediation_job(
        db_session,
        db_host,
        name="dispatch-remediation-device",
    )
    device_id = device.id
    dispatch = AsyncMock(return_value={"success": True, "detail": "cured_by=forward_remove"})

    with patch(
        "app.devices.services.remediation_job.link_repair.dispatch_recommended_action",
        new=dispatch,
    ):
        await RemediationJobService(
            session_factory=_session_factory(db_session),
            circuit_breaker=AsyncMock(),
            health=AsyncMock(),
        ).run_device_health_remediation_job(
            str(job_id),
            {
                "device_id": str(device.id),
                "failure_episode_id": str(failure_episode_id),
                "action_id": "reconnect",
            },
        )

    dispatch.assert_awaited_once()
    db_session.expire_all()
    event = (
        await db_session.execute(
            select(DeviceEvent).where(
                DeviceEvent.device_id == device_id,
                DeviceEvent.event_type == DeviceEventType.repair_attempted,
            )
        )
    ).scalar_one()
    assert event.details == {
        "action": "reconnect",
        "attempt": 1,
        "success": True,
        "detail": "cured_by=forward_remove",
    }
    job = await db_session.get(Job, job_id)
    assert job is not None
    assert job.status == JOB_STATUS_COMPLETED
    assert job.snapshot.get("note") == "dispatched reconnect (success=True)"


async def test_worker_dispatch_receives_fresh_session_and_port_facts(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    device, failure_episode_id, job_id = await _create_failing_remediation_job(
        db_session,
        db_host,
        name="fresh-facts-remediation-device",
        action_id="release_forwarded_ports",
    )
    device_id = device.id
    node = AppiumNode(
        device_id=device.id,
        port=4723,
        desired_state=AppiumDesiredState.running,
        desired_port=4723,
        pid=12345,
        active_connection_target=device.connection_target,
    )
    db_session.add(node)
    await db_session.flush()
    claimed_port = await appium_node_resource_service.reserve(
        db_session,
        host_id=db_host.id,
        capability_key="appium:systemPort",
        start_port=8200,
        node_id=node.id,
    )
    sibling = await create_device(
        db_session,
        host_id=db_host.id,
        name="host-session-sibling",
    )
    db_session.add(
        Session(
            session_id="host-session-sibling-live",
            device_id=sibling.id,
            status=SessionStatus.running,
        )
    )
    await db_session.commit()
    detail = "cured_by=forward_remove:" + ("x" * 240)
    dispatch = AsyncMock(return_value={"success": True, "detail": detail})

    with patch(
        "app.devices.services.remediation_job.link_repair.dispatch_recommended_action",
        new=dispatch,
    ):
        await RemediationJobService(
            session_factory=_session_factory(db_session),
            circuit_breaker=AsyncMock(),
            health=AsyncMock(),
        ).run_device_health_remediation_job(
            str(job_id),
            {
                "device_id": str(device.id),
                "failure_episode_id": str(failure_episode_id),
                "action_id": "release_forwarded_ports",
            },
        )

    dispatch.assert_awaited_once()
    assert dispatch.await_args.kwargs["extra_args"] == {
        "has_live_session": False,
        "host_has_live_sessions": True,
        "claimed_ports": {"appium:systemPort": claimed_port},
    }
    db_session.expire_all()
    event = (
        await db_session.execute(
            select(DeviceEvent).where(
                DeviceEvent.device_id == device_id,
                DeviceEvent.event_type == DeviceEventType.repair_attempted,
            )
        )
    ).scalar_one()
    assert event.details is not None
    assert event.details["detail"] == detail[:200]


async def test_worker_budget_exhaustion_records_repair_failed(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    device, failure_episode_id, job_id = await _create_failing_remediation_job(
        db_session,
        db_host,
        name="budget-remediation-device",
    )
    device_id = device.id
    await state_store.set_value(
        db_session,
        REPAIR_ATTEMPTS_NAMESPACE,
        device.identity_value,
        REPAIR_MAX_ATTEMPTS,
    )
    await db_session.commit()
    dispatch = AsyncMock()

    with patch(
        "app.devices.services.remediation_job.link_repair.dispatch_recommended_action",
        new=dispatch,
    ):
        await RemediationJobService(
            session_factory=_session_factory(db_session),
            circuit_breaker=AsyncMock(),
            health=AsyncMock(),
        ).run_device_health_remediation_job(
            str(job_id),
            {
                "device_id": str(device.id),
                "failure_episode_id": str(failure_episode_id),
                "action_id": "reconnect",
            },
        )

    dispatch.assert_not_awaited()
    db_session.expire_all()
    event = (
        await db_session.execute(
            select(DeviceEvent).where(
                DeviceEvent.device_id == device_id,
                DeviceEvent.event_type == DeviceEventType.repair_failed,
            )
        )
    ).scalar_one()
    assert event.details == {"action": "reconnect", "reason": "attempt budget exhausted"}
    job = await db_session.get(Job, job_id)
    assert job is not None
    assert job.status == JOB_STATUS_COMPLETED
    assert job.snapshot.get("note") == "budget exhausted"


async def test_b6_redelivery_is_repeat_safe_and_new_episode_enqueues(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    device, first_episode_id, job_id = await _create_failing_remediation_job(
        db_session,
        db_host,
        name="b6-redelivery-remediation-device",
    )
    device_id = device.id
    duplicate_job_id = await enqueue_device_health_remediation(
        db_session,
        device_id=device_id,
        failure_episode_id=first_episode_id,
        action_id="reconnect",
        commit=True,
    )
    assert duplicate_job_id is None

    payload = {
        "device_id": str(device_id),
        "failure_episode_id": str(first_episode_id),
        "action_id": "reconnect",
    }
    dispatch = AsyncMock(return_value={"success": True, "detail": "already connected"})
    worker = RemediationJobService(
        session_factory=_session_factory(db_session),
        circuit_breaker=AsyncMock(),
        health=AsyncMock(),
    )

    with patch(
        "app.devices.services.remediation_job.link_repair.dispatch_recommended_action",
        new=dispatch,
    ):
        await worker.run_device_health_remediation_job(str(job_id), payload)
        await worker.run_device_health_remediation_job(str(job_id), payload)

    assert dispatch.await_count == 2
    assert [call.args[1] for call in dispatch.await_args_list] == ["reconnect", "reconnect"]
    assert [call.args[0].id for call in dispatch.await_args_list] == [device_id, device_id]
    repair_events = (
        (
            await db_session.execute(
                select(DeviceEvent)
                .where(
                    DeviceEvent.device_id == device_id,
                    DeviceEvent.event_type == DeviceEventType.repair_attempted,
                )
                .order_by(DeviceEvent.created_at, DeviceEvent.id)
            )
        )
        .scalars()
        .all()
    )
    assert [event.details for event in repair_events] == [
        {"action": "reconnect", "attempt": 1, "success": True, "detail": "already connected"},
        {"action": "reconnect", "attempt": 2, "success": True, "detail": "already connected"},
    ]
    first_job = await db_session.get(Job, job_id)
    assert first_job is not None
    assert first_job.status == JOB_STATUS_COMPLETED
    assert first_job.snapshot.get("note") == "dispatched reconnect (success=True)"

    health = DeviceHealthService(publisher=event_bus)
    assert await health.update_device_checks(db_session, device, healthy=True, summary="Healthy") is True
    await db_session.commit()
    await db_session.refresh(device)
    assert device.failure_episode_id is None

    assert await health.update_device_checks(db_session, device, healthy=False, summary="Disconnected again") is True
    await db_session.commit()
    await db_session.refresh(device)
    second_episode_id = device.failure_episode_id
    assert isinstance(second_episode_id, uuid.UUID)
    assert second_episode_id != first_episode_id

    second_job_id = await enqueue_device_health_remediation(
        db_session,
        device_id=device_id,
        failure_episode_id=second_episode_id,
        action_id="reconnect",
        commit=True,
    )
    assert isinstance(second_job_id, uuid.UUID)
    assert second_job_id != job_id
    second_job = await db_session.get(Job, second_job_id)
    assert second_job is not None
    assert second_job.failure_episode_id == second_episode_id
    assert second_job.remediation_action_id == "reconnect"


async def test_ingest_order_residual_healthy_fact_cancels_queued_remediation(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    device, failure_episode_id, job_id = await _create_failing_remediation_job(
        db_session,
        db_host,
        name="ingest-order-residual-remediation-device",
    )
    health = DeviceHealthService(publisher=event_bus)
    assert await health.update_device_checks(db_session, device, healthy=True, summary="Healthy") is True
    await db_session.commit()
    await db_session.refresh(device)
    assert device.device_checks_healthy is True
    assert device.failure_episode_id is None

    dispatch = AsyncMock(return_value={"success": True})
    with patch(
        "app.devices.services.remediation_job.link_repair.dispatch_recommended_action",
        new=dispatch,
    ):
        await RemediationJobService(
            session_factory=_session_factory(db_session),
            circuit_breaker=AsyncMock(),
            health=AsyncMock(),
        ).run_device_health_remediation_job(
            str(job_id),
            {
                "device_id": str(device.id),
                "failure_episode_id": str(failure_episode_id),
                "action_id": "reconnect",
            },
        )

    dispatch.assert_not_awaited()
    db_session.expire_all()
    job = await db_session.get(Job, job_id)
    assert job is not None
    assert job.status == JOB_STATUS_COMPLETED
    assert job.snapshot.get("note") == "device recovered or episode superseded"
