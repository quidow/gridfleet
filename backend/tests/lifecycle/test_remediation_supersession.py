from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.devices.models import DeviceEvent, DeviceEventType, DeviceRemediationLogEntry
from app.devices.services.intent import IntentService
from app.devices.services.intent_types import CommandKind, IntentRegistration
from app.lifecycle.services import remediation_log
from app.lifecycle.services.actions import (
    LifecyclePolicyActionsService,
    escalate_device_remediation_failure,
    reset_reconciler_start_failure_if_needed,
)
from app.lifecycle.services.incidents import LifecycleIncidentService
from app.lifecycle.services.policy import LifecyclePolicyService
from app.runs.service_reservation import RunReservationService
from tests.fakes import FakeSettingsReader, build_review_service
from tests.helpers import create_device
from tests.helpers import test_event_bus as event_bus

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.hosts.models import Host


pytestmark = pytest.mark.db


def _settings(*, threshold: int = 5) -> FakeSettingsReader:
    return FakeSettingsReader(
        {
            "general.lifecycle_recovery_backoff_base_sec": 10,
            "general.lifecycle_recovery_backoff_max_sec": 40,
            "general.lifecycle_recovery_review_threshold": threshold,
        }
    )


def _actions() -> LifecyclePolicyActionsService:
    return LifecyclePolicyActionsService(
        publisher=event_bus,
        reservation=RunReservationService(review=build_review_service()),
        incidents=LifecycleIncidentService(),
    )


def _policy(settings: FakeSettingsReader) -> LifecyclePolicyService:
    return LifecyclePolicyService(
        review=build_review_service(),
        publisher=event_bus,
        settings=settings,
        actions=_actions(),
        incidents=LifecycleIncidentService(),
        viability=AsyncMock(),
        node_manager=AsyncMock(),
    )


async def _escalate(
    db: AsyncSession,
    device: object,
    *,
    source: str,
    reason: str,
    settings: FakeSettingsReader,
) -> None:
    await escalate_device_remediation_failure(
        db,
        device,  # type: ignore[arg-type]
        settings=settings,
        source=source,
        reason=reason,
    )


async def test_failure_reset_failure_retains_append_only_history(db_session: AsyncSession, db_host: Host) -> None:
    """A reset supersedes the ladder window without erasing its prior attempts."""
    settings = _settings()
    device = await create_device(db_session, host_id=db_host.id, name="supersession-history")

    await _escalate(db_session, device, source="node_health", reason="first", settings=settings)
    await _escalate(db_session, device, source="node_health", reason="second", settings=settings)
    assert (await remediation_log.load_ladder(db_session, device.id)).attempts == 2

    assert await _policy(settings).clear_escalation_residue_on_self_heal(db_session, device, reason="healthy again")
    await _escalate(db_session, device, source="node_health", reason="fresh failure", settings=settings)
    await db_session.commit()

    ladder = await remediation_log.load_ladder(db_session, device.id)
    rows = (await db_session.execute(select(DeviceEvent).where(DeviceEvent.device_id == device.id))).scalars().all()
    log_rows = (
        (
            await db_session.execute(
                select(DeviceRemediationLogEntry).where(DeviceRemediationLogEntry.device_id == device.id)
            )
        )
        .scalars()
        .all()
    )

    assert ladder.attempts == 1
    assert ladder.last_failure_reason == "fresh failure"
    assert len(log_rows) == 4
    assert sum(row.event_type == DeviceEventType.lifecycle_recovered for row in rows) == 1


async def test_self_heal_immediately_resets_and_emits_one_recovery_incident(
    db_session: AsyncSession, db_host: Host
) -> None:
    """Accepted S10 successor semantics have no min-age wait."""
    settings = _settings()
    device = await create_device(db_session, host_id=db_host.id, name="supersession-immediate")
    await _escalate(db_session, device, source="session_viability", reason="probe failed", settings=settings)

    svc = _policy(settings)
    assert await svc.clear_escalation_residue_on_self_heal(db_session, device, reason="self-heal") is True
    assert await svc.clear_escalation_residue_on_self_heal(db_session, device, reason="self-heal") is False
    await db_session.commit()

    events = (
        (
            await db_session.execute(
                select(DeviceEvent).where(
                    DeviceEvent.device_id == device.id,
                    DeviceEvent.event_type == DeviceEventType.lifecycle_recovered,
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(events) == 1
    assert (await remediation_log.load_ladder(db_session, device.id)).attempts == 0


async def test_review_threshold_survives_a_reset(db_session: AsyncSession, db_host: Host) -> None:
    settings = _settings(threshold=3)
    device = await create_device(db_session, host_id=db_host.id, name="supersession-threshold")
    for attempt in range(3):
        await _escalate(db_session, device, source="node_health", reason=f"failure-{attempt}", settings=settings)
    await db_session.commit()

    await db_session.refresh(device)
    assert device.review_required is True
    assert await _policy(settings).clear_escalation_residue_on_self_heal(db_session, device, reason="self-heal") is True
    await db_session.commit()
    await db_session.refresh(device)
    assert device.review_required is True
    assert (await remediation_log.load_ladder(db_session, device.id)).attempts == 0


async def test_reconciler_reset_is_conditioned_on_episode_source(db_session: AsyncSession, db_host: Host) -> None:
    settings = _settings()
    reconciler_device = await create_device(db_session, host_id=db_host.id, name="supersession-reconciler")
    await _escalate(
        db_session,
        reconciler_device,
        source="appium_reconciler",
        reason="spawn failed",
        settings=settings,
    )
    assert await reset_reconciler_start_failure_if_needed(db_session, reconciler_device) is True
    assert (await remediation_log.load_ladder(db_session, reconciler_device.id)).attempts == 0

    health_device = await create_device(db_session, host_id=db_host.id, name="supersession-health")
    await _escalate(db_session, health_device, source="node_health", reason="health failed", settings=settings)
    assert await reset_reconciler_start_failure_if_needed(db_session, health_device) is False
    assert (await remediation_log.load_ladder(db_session, health_device.id)).attempts == 1


async def test_operator_stop_keeps_self_heal_sticky(db_session: AsyncSession, db_host: Host) -> None:
    settings = _settings()
    device = await create_device(db_session, host_id=db_host.id, name="supersession-operator-sticky")
    await IntentService(db_session).register_intents(
        device_id=device.id,
        intents=[
            IntentRegistration(
                source=f"operator:stop:node:{device.id}",
                kind=CommandKind.operator_stop,
                payload={"action": "stop"},
            ),
            IntentRegistration(
                source=f"operator:stop:recovery:{device.id}",
                kind=CommandKind.operator_recovery_deny,
                payload={"allowed": False, "reason": "Operator stopped the node"},
            ),
        ],
    )
    await _escalate(db_session, device, source="node_health", reason="health failed", settings=settings)
    await db_session.commit()

    assert (
        await _policy(settings).clear_escalation_residue_on_self_heal(db_session, device, reason="self-heal") is False
    )
    assert (await remediation_log.load_ladder(db_session, device.id)).attempts == 1
