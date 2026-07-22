"""Lifecycle policy orchestration intent tests."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock, patch

import pytest
from sqlalchemy import select

from app.appium_nodes.models import AppiumDesiredState, AppiumNode
from app.devices.models import DeviceEvent, DeviceEventType, DeviceRemediationLogEntry
from app.lifecycle.services import remediation_log
from app.lifecycle.services.incidents import LifecycleIncidentService
from tests.fakes import FakeSettingsReader, build_review_service
from tests.helpers import create_device
from tests.helpers import test_event_bus as event_bus

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.hosts.models import Host
pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("seeded_driver_packs")]


async def test_attempt_auto_recovery_records_recovery_start_action(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="dw-recover", verified=True)
    node = AppiumNode(
        device_id=device.id,
        port=4723,
        desired_port=None,
        pid=None,
        active_connection_target=None,
        desired_state=AppiumDesiredState.stopped,
    )
    db_session.add(node)
    await db_session.commit()

    from app.devices import locking as device_locking
    from app.devices.services.decision_snapshot import load_device_decision_snapshot
    from app.lifecycle.services.actions import LifecyclePolicyActionsService
    from app.lifecycle.services.policy import LifecyclePolicyService
    from app.runs.service_reservation import RunReservationService

    svc = LifecyclePolicyService(
        review=build_review_service(),
        publisher=Mock(),
        settings=FakeSettingsReader({}),
        actions=LifecyclePolicyActionsService(
            publisher=Mock(),
            reservation=RunReservationService(review=build_review_service()),
            incidents=LifecycleIncidentService(),
        ),
        incidents=LifecycleIncidentService(),
        viability=AsyncMock(),
        node_manager=AsyncMock(),
    )
    generation = uuid.uuid4()
    with patch("app.devices.services.intent.IntentService.reconcile_now", new=AsyncMock()):
        locked = await device_locking.lock_device_handle(db_session, device.id)
        snapshot = await load_device_decision_snapshot(db_session, locked, packs={}, now=datetime.now(UTC))
        await svc.prepare_auto_recovery_locked(
            db_session,
            locked,
            snapshot,
            generation=generation,
            source="health_recovery",
            reason="test",
            enqueue_job=False,
        )
        await db_session.commit()

    entries = (
        (
            await db_session.execute(
                select(DeviceRemediationLogEntry).where(DeviceRemediationLogEntry.device_id == device.id)
            )
        )
        .scalars()
        .all()
    )
    assert any(entry.action == remediation_log.ACTION_RECOVERY_STARTED for entry in entries)


async def test_auto_recovery_intent_falls_back_to_live_node_port(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    """Regression: live ``node.port`` wins over any port the intent payload
    might carry, so a restart on a new port does not produce a phantom stop
    on the next intent-reconciliation cycle.

    Scenario reproduces the firetv-12 flap pattern: auto_recovery registered
    when node.port was P1; agent later restarted on P2 (allocator-picked);
    intent_reconciler must drive ``node.desired_port == P2``, not P1.
    """
    from app.devices.services.intent_reconciler import reconcile_device

    device = await create_device(db_session, host_id=db_host.id, name="port-flap-repro", verified=True)
    node = AppiumNode(
        device_id=device.id,
        port=4757,
        desired_port=None,
        pid=12345,
        active_connection_target="127.0.0.1:4757",
        desired_state=AppiumDesiredState.running,
    )
    db_session.add(node)
    await db_session.flush()
    await remediation_log.append_action(
        db_session,
        device.id,
        source="recovery",
        action=remediation_log.ACTION_RECOVERY_STARTED,
    )
    await db_session.commit()

    await reconcile_device(db_session, device.id, publisher=event_bus)
    await db_session.commit()

    await db_session.refresh(node)
    assert node.desired_state == AppiumDesiredState.running
    assert node.desired_port == 4757, (
        f"intent_reconciler must drive desired_port to live node.port (4757); got {node.desired_port}"
    )


async def test_handle_node_crash_tags_desired_state_with_lifecycle_crash(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="dw-crash", verified=True)
    node = AppiumNode(
        device_id=device.id,
        port=4723,
        active_connection_target="",
        desired_state=AppiumDesiredState.running,
        desired_port=4723,
        pid=99,
    )
    db_session.add(node)
    await db_session.commit()

    from app.lifecycle.services.actions import LifecyclePolicyActionsService
    from app.runs.service_reservation import RunReservationService

    await LifecyclePolicyActionsService(
        publisher=event_bus,
        reservation=RunReservationService(review=build_review_service()),
        incidents=LifecycleIncidentService(),
    ).handle_node_crash(
        db_session,
        device,
        source="connectivity_lost",
        reason="agent disconnected",
    )

    events = (
        (
            await db_session.execute(
                select(DeviceEvent).where(
                    DeviceEvent.device_id == device.id,
                    DeviceEvent.event_type == DeviceEventType.desired_state_changed,
                )
            )
        )
        .scalars()
        .all()
    )
    assert any(
        event.details is not None
        and event.details.get("caller") == "intent_reconciler"
        and event.details.get("new_desired_state") == "stopped"
        for event in events
    )


async def test_handle_node_crash_writes_desired_stopped_when_node_already_stopped(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="dw-crash-stopped", verified=True)
    node = AppiumNode(
        device_id=device.id,
        port=4723,
        pid=None,
        active_connection_target=None,
        desired_state=AppiumDesiredState.running,
        desired_port=4723,
    )
    db_session.add(node)
    await db_session.commit()

    from app.lifecycle.services.actions import LifecyclePolicyActionsService
    from app.runs.service_reservation import RunReservationService

    await LifecyclePolicyActionsService(
        publisher=event_bus,
        reservation=RunReservationService(review=build_review_service()),
        incidents=LifecycleIncidentService(),
    ).handle_node_crash(
        db_session,
        device,
        source="health_check_fail",
        reason="probe failed",
    )

    events = (
        (
            await db_session.execute(
                select(DeviceEvent).where(
                    DeviceEvent.device_id == device.id,
                    DeviceEvent.event_type == DeviceEventType.desired_state_changed,
                )
            )
        )
        .scalars()
        .all()
    )
    desired_events = [
        event for event in events if event.details is not None and event.details.get("new_desired_state") == "stopped"
    ]
    assert len(desired_events) == 1
    assert desired_events[0].details is not None
    assert desired_events[0].details["caller"] == "intent_reconciler"
