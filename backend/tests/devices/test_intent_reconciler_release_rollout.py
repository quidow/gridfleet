from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock

from sqlalchemy import select

from app.appium_nodes.models import AppiumDesiredState, AppiumNode
from app.core.timeutil import now_utc
from app.devices.models import Device, DeviceIntent
from app.devices.services import intent_reconciler
from app.devices.services.decision import DecisionFacts
from app.devices.services.intent import IntentService
from app.devices.services.intent_types import CommandKind, IntentRegistration, release_rollout_intent_source
from app.packs.services.release_rollout import RELEASE_ROLLOUT_INTENT_TTL_SEC
from app.sessions.models import Session, SessionStatus
from tests.helpers import create_device
from tests.helpers import test_event_bus as event_bus

if TYPE_CHECKING:
    import pytest
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.hosts.models import Host


def _rollout_intent(device_id: uuid.UUID, *, target_release: str) -> IntentRegistration:
    return IntentRegistration(
        source=release_rollout_intent_source(device_id),
        kind=CommandKind.release_rollout,
        payload={"target_release": target_release},
        expires_at=now_utc() + timedelta(seconds=RELEASE_ROLLOUT_INTENT_TTL_SEC),
    )


async def _running_device(db: AsyncSession, host: Host, *, name: str) -> tuple[Device, AppiumNode]:
    device = await create_device(db, host_id=host.id, name=name)
    node = AppiumNode(
        device_id=device.id,
        port=4723,
        desired_state=AppiumDesiredState.running,
        desired_port=4723,
        observed_pack_release="old",
    )
    db.add(node)
    await db.commit()
    return device, node


async def test_rollout_drains_while_session_live(db_session: AsyncSession, db_host: Host) -> None:
    device, node = await _running_device(db_session, db_host, name="rollout-live")
    db_session.add(Session(session_id="rollout-live", device_id=device.id, status=SessionStatus.running))
    await db_session.commit()

    await IntentService(db_session).register_intents_and_reconcile(
        device_id=device.id,
        intents=[_rollout_intent(device.id, target_release="B")],
        publisher=event_bus,
    )

    row = (await db_session.execute(select(DeviceIntent).where(DeviceIntent.device_id == device.id))).scalar_one()
    assert node.desired_state == AppiumDesiredState.running
    assert node.accepting_new_sessions is False
    assert node.restart_requested_at is None
    assert row.payload.get("restart_requested_at") is None


async def test_rollout_stamps_watermark_once_when_idle(db_session: AsyncSession, db_host: Host) -> None:
    device, node = await _running_device(db_session, db_host, name="rollout-idle")

    await IntentService(db_session).register_intents_and_reconcile(
        device_id=device.id,
        intents=[_rollout_intent(device.id, target_release="B")],
        publisher=event_bus,
    )

    row = (await db_session.execute(select(DeviceIntent).where(DeviceIntent.device_id == device.id))).scalar_one()
    first = node.restart_requested_at
    assert node.accepting_new_sessions is False
    assert first is not None
    assert row.payload["restart_requested_at"] == first.isoformat()

    await IntentService(db_session).reconcile_now(device.id, publisher=event_bus)

    assert node.restart_requested_at == first
    assert row.payload["restart_requested_at"] == first.isoformat()


async def test_rollout_stamp_sequence_uses_live_session_state_once(monkeypatch: pytest.MonkeyPatch) -> None:
    device_id = uuid.uuid4()
    node = AppiumNode(
        device_id=device_id,
        port=4723,
        desired_state=AppiumDesiredState.running,
        desired_port=4723,
        desired_grid_run_id=None,
        accepting_new_sessions=False,
        stop_pending=False,
        restart_requested_at=None,
        observed_pack_release="old",
    )
    device = Device(id=device_id)
    device.appium_node = node
    row = DeviceIntent(
        device_id=device_id,
        source=release_rollout_intent_source(device_id),
        kind=CommandKind.release_rollout.value,
        payload={"target_release": "B"},
        expires_at=datetime(2026, 7, 18, tzinfo=UTC),
    )
    result = Mock()
    result.scalars.return_value.all.return_value = [row]
    db = AsyncMock()
    db.execute.return_value = result
    times = iter(
        (
            datetime(2026, 7, 17, 12, 0, tzinfo=UTC),
            datetime(2026, 7, 17, 12, 1, tzinfo=UTC),
            datetime(2026, 7, 17, 12, 2, tzinfo=UTC),
        )
    )
    # Live on the first reconcile (stamp deferred), idle thereafter so the
    # stamp mints on call 2 and the watermark re-validation passes on calls 2-3.
    session_check = AsyncMock(side_effect=(True, False, False, False))
    facts = DecisionFacts(
        in_maintenance=False,
        device_checks_unhealthy=False,
        in_service=True,
        reservation_run_id=None,
        cooldown_active=False,
        cooldown_reason=None,
    )
    monkeypatch.setattr(intent_reconciler, "now_utc", lambda: next(times))
    monkeypatch.setattr(intent_reconciler, "device_has_live_session", session_check)
    monkeypatch.setattr(intent_reconciler, "gather_decision_facts", AsyncMock(return_value=facts))
    monkeypatch.setattr(intent_reconciler, "emit_operational_state_transition", AsyncMock())
    monkeypatch.setattr("app.appium_nodes.services.desired_state_writer.record_event", AsyncMock())

    await intent_reconciler._reconcile_loaded_device(db, device, publisher=AsyncMock())
    assert row.payload.get("restart_requested_at") is None
    assert node.restart_requested_at is None

    await intent_reconciler._reconcile_loaded_device(db, device, publisher=AsyncMock())
    stamp = datetime(2026, 7, 17, 12, 1, tzinfo=UTC)
    assert row.payload["restart_requested_at"] == stamp.isoformat()
    assert node.restart_requested_at == stamp

    await intent_reconciler._reconcile_loaded_device(db, device, publisher=AsyncMock())
    assert row.payload["restart_requested_at"] == stamp.isoformat()
    assert node.restart_requested_at == stamp
    # Call 1: stamp gate (live). Call 2: stamp gate (idle) + watermark
    # re-validation (idle). Call 3: watermark re-validation (idle, stamp
    # already minted so the stamp gate is skipped).
    assert session_check.await_count == 4


async def test_rollout_does_not_stamp_when_node_already_converged(db_session: AsyncSession, db_host: Host) -> None:
    """Finding 5: a node that crashed and respawned on the target release must
    not be force-restarted. The stamp gate checks the node is still
    release-mismatched, not merely that an unstamped rollout intent exists."""
    device, node = await _running_device(db_session, db_host, name="rollout-converged-idle")
    node.observed_pack_release = "B"  # already on the target

    await IntentService(db_session).register_intents_and_reconcile(
        device_id=device.id,
        intents=[_rollout_intent(device.id, target_release="B")],
        publisher=event_bus,
    )

    row = (
        await db_session.execute(select(DeviceIntent).where(DeviceIntent.device_id == device.id))
    ).scalar_one_or_none()
    # Finding 6: the converged rollout intent is revoked inline, not left
    # draining the device for up to 60 s waiting on the janitor stage.
    assert row is None
    assert node.restart_requested_at is None
    assert node.accepting_new_sessions is True


async def test_rollout_does_not_stamp_while_reservation_active(db_session: AsyncSession, db_host: Host) -> None:
    """Finding 2: a reserved-but-idle device is mid-run; the rollout must defer
    until the reservation releases (mirrors pack drain's active-work check)."""
    from app.runs.models import RunState
    from tests.helpers import create_reserved_run

    device, node = await _running_device(db_session, db_host, name="rollout-reserved")
    await create_reserved_run(db_session, name="rollout-reserved-run", devices=[device], state=RunState.active)

    await IntentService(db_session).register_intents_and_reconcile(
        device_id=device.id,
        intents=[_rollout_intent(device.id, target_release="B")],
        publisher=event_bus,
    )

    row = (await db_session.execute(select(DeviceIntent).where(DeviceIntent.device_id == device.id))).scalar_one()
    assert row.payload.get("restart_requested_at") is None
    assert node.restart_requested_at is None
    # Still drains (accepting_new_sessions=False) but does not force-restart.
    assert node.accepting_new_sessions is False


async def test_rollout_suppresses_dormant_stamp_when_session_live_at_write_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Finding 1: a stamp minted while idle sits dormant behind a coexisting
    start intent. When the start expires and the rollout wins carrying the
    stamp, the watermark-write re-validation re-checks the live session and
    suppresses the watermark so an in-flight session is not killed."""
    device_id = uuid.uuid4()
    node = AppiumNode(
        device_id=device_id,
        port=4723,
        desired_state=AppiumDesiredState.running,
        desired_port=4723,
        desired_grid_run_id=None,
        accepting_new_sessions=False,
        stop_pending=False,
        restart_requested_at=None,
        observed_pack_release="old",
    )
    device = Device(id=device_id)
    device.appium_node = node
    dormant_stamp = datetime(2026, 7, 17, 12, 0, tzinfo=UTC)
    row = DeviceIntent(
        device_id=device_id,
        source=release_rollout_intent_source(device_id),
        kind=CommandKind.release_rollout.value,
        payload={"target_release": "B", "restart_requested_at": dormant_stamp.isoformat()},
        expires_at=datetime(2026, 7, 18, tzinfo=UTC),
    )
    result = Mock()
    result.scalars.return_value.all.return_value = [row]
    db = AsyncMock()
    db.execute.return_value = result
    facts = DecisionFacts(
        in_maintenance=False,
        device_checks_unhealthy=False,
        in_service=True,
        reservation_run_id=None,
        cooldown_active=False,
        cooldown_reason=None,
    )
    monkeypatch.setattr(intent_reconciler, "now_utc", lambda: datetime(2026, 7, 17, 12, 5, tzinfo=UTC))
    # Live session at watermark-write time → suppress the dormant stamp.
    monkeypatch.setattr(intent_reconciler, "device_has_live_session", AsyncMock(return_value=True))
    monkeypatch.setattr(intent_reconciler, "gather_decision_facts", AsyncMock(return_value=facts))
    monkeypatch.setattr(intent_reconciler, "emit_operational_state_transition", AsyncMock())
    monkeypatch.setattr("app.appium_nodes.services.desired_state_writer.record_event", AsyncMock())

    await intent_reconciler._reconcile_loaded_device(db, device, publisher=AsyncMock())

    # The stamp remains in the payload (not cleared), but the node watermark is
    # suppressed so the agent does not force-restart into a live session.
    assert row.payload.get("restart_requested_at") == dormant_stamp.isoformat()
    assert node.restart_requested_at is None
    assert node.accepting_new_sessions is False


async def test_rollout_suppresses_dormant_stamp_when_converged_at_write_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Finding 5 at watermark-write time: a dormant stamp must not promote to a
    restart once the node has converged onto the target release (e.g. after a
    crash-restart that re-launched on the selected release)."""
    device_id = uuid.uuid4()
    node = AppiumNode(
        device_id=device_id,
        port=4723,
        desired_state=AppiumDesiredState.running,
        desired_port=4723,
        desired_grid_run_id=None,
        accepting_new_sessions=False,
        stop_pending=False,
        restart_requested_at=None,
        observed_pack_release="B",  # converged onto the target
    )
    device = Device(id=device_id)
    device.appium_node = node
    dormant_stamp = datetime(2026, 7, 17, 12, 0, tzinfo=UTC)
    row = DeviceIntent(
        device_id=device_id,
        source=release_rollout_intent_source(device_id),
        kind=CommandKind.release_rollout.value,
        payload={"target_release": "B", "restart_requested_at": dormant_stamp.isoformat()},
        expires_at=datetime(2026, 7, 18, tzinfo=UTC),
    )
    result = Mock()
    result.scalars.return_value.all.return_value = [row]
    db = AsyncMock()
    db.execute.return_value = result
    facts = DecisionFacts(
        in_maintenance=False,
        device_checks_unhealthy=False,
        in_service=True,
        reservation_run_id=None,
        cooldown_active=False,
        cooldown_reason=None,
    )
    monkeypatch.setattr(intent_reconciler, "now_utc", lambda: datetime(2026, 7, 17, 12, 5, tzinfo=UTC))
    monkeypatch.setattr(intent_reconciler, "device_has_live_session", AsyncMock(return_value=False))
    monkeypatch.setattr(intent_reconciler, "gather_decision_facts", AsyncMock(return_value=facts))
    monkeypatch.setattr(intent_reconciler, "emit_operational_state_transition", AsyncMock())
    monkeypatch.setattr(intent_reconciler, "record_event", AsyncMock())
    monkeypatch.setattr("app.appium_nodes.services.desired_state_writer.record_event", AsyncMock())

    await intent_reconciler._reconcile_loaded_device(db, device, publisher=AsyncMock())

    # Finding 6: converged rollout intent is revoked inline; the device returns
    # to the baseline accepting state, and no watermark is published.
    assert node.restart_requested_at is None
    assert node.accepting_new_sessions is True
