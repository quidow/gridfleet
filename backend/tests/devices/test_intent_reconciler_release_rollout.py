from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

from sqlalchemy import select

from app.appium_nodes.models import AppiumDesiredState, AppiumNode
from app.core.timeutil import now_utc
from app.devices.models import Device, DeviceIntent
from app.devices.services.intent import IntentService
from app.devices.services.intent_types import CommandKind, IntentRegistration, release_rollout_intent_source
from app.packs.services.release_rollout import RELEASE_ROLLOUT_INTENT_TTL_SEC
from app.sessions.models import Session, SessionStatus
from tests.helpers import create_device
from tests.helpers import test_event_bus as event_bus

if TYPE_CHECKING:
    import uuid

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


async def test_rollout_stamp_sequence_uses_live_session_state_once(db_session: AsyncSession, db_host: Host) -> None:
    """The stamp deferral is not sticky: a rollout that could not stamp while a
    session was live mints the watermark once the session ends, then holds it
    steady on subsequent reconciles (Findings 1, 5, 7 in sequence)."""
    device, node = await _running_device(db_session, db_host, name="rollout-sequence")
    session = Session(session_id="rollout-sequence", device_id=device.id, status=SessionStatus.running)
    db_session.add(session)
    await db_session.commit()

    # Reconcile 1: session live -> stamp deferred, node drains without a watermark.
    await IntentService(db_session).register_intents_and_reconcile(
        device_id=device.id,
        intents=[_rollout_intent(device.id, target_release="B")],
        publisher=event_bus,
    )
    row = (await db_session.execute(select(DeviceIntent).where(DeviceIntent.device_id == device.id))).scalar_one()
    assert row.payload.get("restart_requested_at") is None
    assert node.restart_requested_at is None
    assert node.accepting_new_sessions is False

    # Session ends -> the next reconcile mints the watermark exactly once.
    await db_session.delete(session)
    await db_session.commit()
    await IntentService(db_session).reconcile_now(device.id, publisher=event_bus)
    await db_session.refresh(row)
    stamp = node.restart_requested_at
    assert stamp is not None
    assert row.payload["restart_requested_at"] == stamp.isoformat()

    # A further reconcile holds the same watermark steady (stamped once).
    await IntentService(db_session).reconcile_now(device.id, publisher=event_bus)
    await db_session.refresh(row)
    assert node.restart_requested_at == stamp
    assert row.payload["restart_requested_at"] == stamp.isoformat()


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
    db_session: AsyncSession, db_host: Host
) -> None:
    """Finding 1: once a stamp is carried in the payload, the watermark-write
    re-validation re-checks the live session before the watermark reaches the
    node. A session that starts after the stamp was minted suppresses the node
    watermark so an in-flight session is not force-restarted, while the payload
    stamp stays intact."""
    device, node = await _running_device(db_session, db_host, name="rollout-suppress-live")

    # Idle reconcile mints the stamp onto both the payload and the node.
    await IntentService(db_session).register_intents_and_reconcile(
        device_id=device.id,
        intents=[_rollout_intent(device.id, target_release="B")],
        publisher=event_bus,
    )
    row = (await db_session.execute(select(DeviceIntent).where(DeviceIntent.device_id == device.id))).scalar_one()
    stamp = node.restart_requested_at
    assert stamp is not None
    assert row.payload["restart_requested_at"] == stamp.isoformat()

    # A session starts after the stamp was minted; the next reconcile suppresses
    # the node watermark at write time but leaves the payload stamp untouched.
    db_session.add(Session(session_id="rollout-suppress-live", device_id=device.id, status=SessionStatus.running))
    await db_session.commit()
    await IntentService(db_session).reconcile_now(device.id, publisher=event_bus)
    await db_session.refresh(row)

    assert row.payload.get("restart_requested_at") == stamp.isoformat()
    assert node.restart_requested_at is None
    assert node.accepting_new_sessions is False


async def test_rollout_suppresses_dormant_stamp_when_converged_at_write_time(
    db_session: AsyncSession, db_host: Host
) -> None:
    """Finding 5 at watermark-write time: a stamped rollout must not promote to
    a restart once the node has converged onto the target release (e.g. after a
    crash-restart that re-launched on the selected release). The intent is
    revoked inline (Finding 6) and the node returns to baseline accepting."""
    device, node = await _running_device(db_session, db_host, name="rollout-suppress-converged")

    # Idle reconcile mints the stamp onto both the payload and the node.
    await IntentService(db_session).register_intents_and_reconcile(
        device_id=device.id,
        intents=[_rollout_intent(device.id, target_release="B")],
        publisher=event_bus,
    )
    assert node.restart_requested_at is not None

    # The node converges onto the target release before the watermark applied;
    # the next reconcile revokes the rollout inline and clears the watermark.
    node.observed_pack_release = "B"
    await db_session.commit()
    await IntentService(db_session).reconcile_now(device.id, publisher=event_bus)

    row = (
        await db_session.execute(select(DeviceIntent).where(DeviceIntent.device_id == device.id))
    ).scalar_one_or_none()
    assert row is None
    assert node.restart_requested_at is None
    assert node.accepting_new_sessions is True
