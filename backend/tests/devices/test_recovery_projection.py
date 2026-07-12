"""The projection must answer identically to attempt_auto_recovery's gates."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from app.devices import locking as device_locking
from app.devices.services.intent import IntentService
from app.devices.services.intent_types import CommandKind, IntentRegistration
from app.devices.services.lifecycle_policy_state import (
    CLIENT_SESSION_RUNNING_SUPPRESSION_REASON,
    MAINTENANCE_HOLD_SUPPRESSION_REASON,
    set_maintenance_reason,
    write_state,
)
from app.devices.services.lifecycle_policy_state import state as policy_state
from app.devices.services.recovery_projection import (
    RecoveryBlockKind,
    recovery_availability,
)
from app.lifecycle.services import remediation_log
from app.sessions.models import Session, SessionStatus
from tests.fakes import FakeSettingsReader
from tests.helpers import create_device

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

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
    locked = await device_locking.lock_device(db_session, device.id)
    state = policy_state(locked)
    state["deferred_stop"] = True
    write_state(locked, state)
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
