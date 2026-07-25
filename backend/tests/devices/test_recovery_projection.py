"""The projection must answer identically to attempt_auto_recovery's gates."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from sqlalchemy import select

from app.core.timeutil import now_utc
from app.devices import locking as device_locking
from app.devices.models import DeviceIntent
from app.devices.services.decision import parse_command
from app.devices.services.intent import IntentService
from app.devices.services.intent_reconciler import gather_decision_facts
from app.devices.services.intent_types import CommandKind, IntentRegistration
from app.devices.services.lifecycle_policy_state import (
    CLIENT_SESSION_RUNNING_SUPPRESSION_REASON,
    MAINTENANCE_HOLD_SUPPRESSION_REASON,
    set_maintenance_reason,
)
from app.devices.services.readiness import is_ready_for_use_async
from app.devices.services.recovery_projection import (
    RecoveryBlockKind,
    recovery_availability,
    recovery_availability_from_facts,
)
from app.lifecycle.services import remediation_log
from app.sessions.live_session_predicate import device_has_live_session
from app.sessions.models import Session, SessionStatus
from tests.fakes import FakeSettingsReader
from tests.helpers import create_device

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.devices.models import Device
    from app.hosts.models import Host

pytestmark = pytest.mark.usefixtures("seeded_driver_packs")


async def test_clean_device_allows_recovery(db_session: AsyncSession, db_host: Host) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="clean")
    result = await recovery_availability(db_session, device)
    assert (result.allowed, result.kind) == (True, None)


async def test_review_required_blocks_first(db_session: AsyncSession, db_host: Host) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="shelved")
    device.review_required = True  # tests may write directly; contract scan covers app/ only
    device.review_reason = "shelved by test"
    await db_session.commit()
    result = await recovery_availability(db_session, device)
    assert result.allowed is False
    assert result.kind is RecoveryBlockKind.review
    assert result.reason == "shelved by test"


async def test_operator_recovery_deny_blocks(db_session: AsyncSession, db_host: Host) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="op-deny")
    await IntentService(db_session).register_intents(
        device_id=device.id,
        intents=[
            IntentRegistration(
                source=f"operator:stop:recovery:{device.id}",
                kind=CommandKind.operator_recovery_deny,
                payload={"allowed": False, "reason": "Operator stopped the node"},
            )
        ],
    )
    await db_session.commit()
    result = await recovery_availability(db_session, device)
    assert (result.allowed, result.kind) == (False, RecoveryBlockKind.operator)
    assert result.reason == "Operator stopped the node"


async def test_maintenance_blocks_with_constant(db_session: AsyncSession, db_host: Host) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="maint")
    locked = await device_locking.lock_device(db_session, device.id)
    set_maintenance_reason(locked, "operator hold")
    await db_session.commit()
    result = await recovery_availability(db_session, device)
    assert (result.allowed, result.kind) == (False, RecoveryBlockKind.maintenance)
    assert result.reason == MAINTENANCE_HOLD_SUPPRESSION_REASON


async def test_not_ready_blocks(db_session: AsyncSession, db_host: Host) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="unverified", verified=False)
    result = await recovery_availability(db_session, device)
    assert (result.allowed, result.kind) == (False, RecoveryBlockKind.not_ready)


async def test_deferred_stop_blocks(db_session: AsyncSession, db_host: Host) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="stop-pending")
    await remediation_log.append_action(
        db_session,
        device.id,
        source="device_checks",
        action=remediation_log.ACTION_AUTO_STOP_DEFERRED,
        reason="probe failed",
    )
    db_session.add(Session(session_id="sess-deferred-projection", device_id=device.id, status=SessionStatus.running))
    await db_session.commit()
    result = await recovery_availability(db_session, device)
    assert (result.allowed, result.kind) == (False, RecoveryBlockKind.deferred_stop)


async def test_live_session_blocks(db_session: AsyncSession, db_host: Host) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="session")
    db_session.add(Session(session_id="sess-proj-1", device_id=device.id, status=SessionStatus.running))
    await db_session.commit()
    result = await recovery_availability(db_session, device)
    assert (result.allowed, result.kind) == (False, RecoveryBlockKind.session)
    assert result.reason == CLIENT_SESSION_RUNNING_SUPPRESSION_REASON


async def test_backoff_window_blocks(db_session: AsyncSession, db_host: Host) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="backoff")
    await remediation_log.append_attempt(
        db_session,
        device.id,
        source="node_health",
        reason="backoff",
        settings=FakeSettingsReader(
            {
                "general.lifecycle_recovery_backoff_base_sec": 600,
                "general.lifecycle_recovery_backoff_max_sec": 600,
            }
        ),
    )
    await db_session.commit()
    result = await recovery_availability(db_session, device)
    assert (result.allowed, result.kind) == (False, RecoveryBlockKind.backoff)


async def _seed_operator_deny(db_session: AsyncSession, device: Device) -> None:
    await IntentService(db_session).register_intents(
        device_id=device.id,
        intents=[
            IntentRegistration(
                source=f"operator:stop:recovery:{device.id}",
                kind=CommandKind.operator_recovery_deny,
                payload={"allowed": False, "reason": "Operator stopped the node"},
            )
        ],
    )


async def _seed_live_session(db_session: AsyncSession, device: Device) -> None:
    db_session.add(Session(session_id="sess-parity-live", device_id=device.id, status=SessionStatus.running))


async def _seed_deferred_stop(db_session: AsyncSession, device: Device) -> None:
    await remediation_log.append_action(
        db_session,
        device.id,
        source="device_checks",
        action=remediation_log.ACTION_AUTO_STOP_DEFERRED,
        reason="probe failed",
    )
    db_session.add(Session(session_id="sess-parity-deferred", device_id=device.id, status=SessionStatus.running))


async def _seed_backoff(db_session: AsyncSession, device: Device) -> None:
    await remediation_log.append_attempt(
        db_session,
        device.id,
        source="node_health",
        reason="backoff",
        settings=FakeSettingsReader(
            {
                "general.lifecycle_recovery_backoff_base_sec": 600,
                "general.lifecycle_recovery_backoff_max_sec": 600,
            }
        ),
    )


@pytest.mark.parametrize(
    "seed",
    [
        pytest.param(_seed_operator_deny, id="operator-deny"),
        pytest.param(_seed_live_session, id="live-session"),
        pytest.param(_seed_deferred_stop, id="deferred-stop"),
        pytest.param(_seed_backoff, id="backoff"),
    ],
)
async def test_recovery_availability_from_facts_matches_async(
    db_session: AsyncSession,
    db_host: Host,
    seed: Callable[[AsyncSession, Device], Awaitable[None]],
) -> None:
    """The pure ladder must return byte-for-byte what the async wrapper does."""
    device = await create_device(db_session, host_id=db_host.id, name="parity")
    await seed(db_session, device)
    await db_session.commit()

    now = now_utc()
    ladder = await remediation_log.load_ladder(db_session, device.id)
    intents = (
        (await db_session.execute(select(DeviceIntent).where(DeviceIntent.device_id == device.id))).scalars().all()
    )
    facts = await gather_decision_facts(db_session, device, now, ladder=ladder)
    ready = await is_ready_for_use_async(db_session, device)
    live_session = await device_has_live_session(db_session, device.id)

    availability = recovery_availability_from_facts(
        device,
        commands=[command for intent in intents if (command := parse_command(intent, now)) is not None],
        facts=facts,
        ladder=ladder,
        live_session=live_session,
        ready=ready,
        now=now,
    )
    assert availability == await recovery_availability(db_session, device, now=now, ready=ready)
