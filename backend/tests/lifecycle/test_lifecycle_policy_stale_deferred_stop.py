"""Successor semantics for remediation-log-derived deferred stops."""

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock

import pytest

from app.devices.models import DeviceOperationalState
from app.devices.services.lifecycle_policy_summary import build_lifecycle_policy
from app.devices.services.recovery_projection import RecoveryBlockKind, recovery_availability
from app.lifecycle.services import remediation_log
from app.lifecycle.services.actions import LifecyclePolicyActionsService
from app.lifecycle.services.incidents import LifecycleIncidentService
from app.lifecycle.services.policy import DeferredStopOutcome, LifecyclePolicyService
from app.runs.service_reservation import RunReservationService
from app.sessions.models import Session, SessionStatus
from tests.fakes import FakeSettingsReader, build_review_service
from tests.helpers import create_device
from tests.helpers import test_event_bus as event_bus

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.hosts.models import Host

pytestmark = pytest.mark.usefixtures("seeded_driver_packs")


def _service() -> LifecyclePolicyService:
    return LifecyclePolicyService(
        review=build_review_service(),
        publisher=event_bus,
        settings=FakeSettingsReader({}),
        actions=LifecyclePolicyActionsService(
            publisher=event_bus,
            reservation=RunReservationService(review=build_review_service()),
            incidents=LifecycleIncidentService(),
        ),
        incidents=LifecycleIncidentService(),
        viability=Mock(),
        node_manager=AsyncMock(),
    )


async def test_health_failure_derives_pending_stop_and_policy_view(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="deferred-device",
        operational_state=DeviceOperationalState.busy,
    )
    db_session.add(Session(session_id="live-session", device_id=device.id, status=SessionStatus.running))
    await db_session.commit()

    assert (
        await _service().handle_health_failure(db_session, device, source="device_checks", reason="probe failed")
        == "deferred"
    )

    ladder = await remediation_log.load_ladder(db_session, device.id)
    assert ladder.deferred_stop_pending is True
    assert ladder.deferred_stop_reason == "probe failed"
    assert ladder.deferred_stop_since is not None
    policy = await build_lifecycle_policy(db_session, device)
    assert policy["deferred_stop"] is True
    assert policy["deferred_stop_reason"] == "probe failed"


async def test_session_end_completes_derived_deferred_stop(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="deferred-session-end",
        operational_state=DeviceOperationalState.busy,
    )
    session = Session(session_id="ending-session", device_id=device.id, status=SessionStatus.running)
    db_session.add(session)
    await db_session.commit()
    assert (
        await _service().handle_health_failure(db_session, device, source="device_checks", reason="probe failed")
        == "deferred"
    )

    session.status = SessionStatus.passed
    session.ended_at = datetime.now(UTC)
    await db_session.commit()
    outcome = await _service().handle_session_finished(db_session, device)

    assert outcome is DeferredStopOutcome.AUTO_STOPPED
    ladder = await remediation_log.load_ladder(db_session, device.id)
    assert ladder.deferred_stop_pending is False
    assert ladder.last_action == remediation_log.ACTION_AUTO_STOPPED


async def test_deferred_stop_is_live_session_gated_in_recovery_projection(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="projection-gated")
    await remediation_log.append_action(
        db_session,
        device.id,
        source="device_checks",
        action=remediation_log.ACTION_AUTO_STOP_DEFERRED,
        reason="probe failed",
    )
    await db_session.commit()

    assert (await recovery_availability(db_session, device, ready=True)).allowed is True
    db_session.add(Session(session_id="projection-session", device_id=device.id, status=SessionStatus.running))
    await db_session.commit()
    blocked = await recovery_availability(db_session, device, ready=True)
    assert blocked.allowed is False
    assert blocked.kind is RecoveryBlockKind.deferred_stop


async def test_clear_pending_auto_stop_appends_action_without_explicit_action(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="clear-pending")
    await remediation_log.append_action(
        db_session,
        device.id,
        source="device_checks",
        action=remediation_log.ACTION_AUTO_STOP_DEFERRED,
        reason="probe failed",
    )
    await db_session.commit()

    cleared = await _service().clear_pending_auto_stop_on_recovery(
        db_session,
        device,
        source="node_health",
        reason="recovered",
        record_incident=False,
    )
    await db_session.commit()

    assert cleared is True
    ladder = await remediation_log.load_ladder(db_session, device.id)
    assert ladder.deferred_stop_pending is False
    assert ladder.last_action == remediation_log.ACTION_AUTO_STOP_CLEARED


async def test_reset_supersedes_pending_deferred_stop(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="reset-pending")
    await remediation_log.append_action(
        db_session,
        device.id,
        source="device_checks",
        action=remediation_log.ACTION_AUTO_STOP_DEFERRED,
        reason="probe failed",
    )
    await remediation_log.append_reset(
        db_session, device.id, source="device_checks", action="self_healed", reason="recovered"
    )

    ladder = await remediation_log.load_ladder(db_session, device.id)
    assert ladder.deferred_stop_pending is False
