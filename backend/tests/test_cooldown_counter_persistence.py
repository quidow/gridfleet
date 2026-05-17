"""Cooldown counter must persist across exclusion-clear (TTL expiry) cycles.

If the counter resets every time the cooldown:reservation intent expires,
the escalation threshold becomes unreachable for slow-burn flakes where
each timeout TTL window only sees one failure. The counter is the
authority on "how many cooldowns this reservation has seen"; it is reset
only when the reservation is released or explicitly restored.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import select

from app.appium_nodes.models import AppiumDesiredState, AppiumNode
from app.devices.models import DeviceReservation
from app.devices.services.intent import IntentService
from app.devices.services.intent_reconciler import _reconcile_device, _reconcile_expired_intents
from app.devices.services.intent_types import RESERVATION, IntentRegistration
from app.runs import service as run_service
from tests.helpers import create_device, create_reserved_run

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.hosts.models import Host


async def _seed_node(db_session: AsyncSession, device_id: object) -> AppiumNode:
    node = AppiumNode(
        device_id=device_id,
        port=4723,
        grid_url="http://grid:4444",
        desired_state=AppiumDesiredState.stopped,
    )
    db_session.add(node)
    await db_session.commit()
    return node


async def _reservation_for(db_session: AsyncSession, device_id: object) -> DeviceReservation:
    result = await db_session.execute(select(DeviceReservation).where(DeviceReservation.device_id == device_id))
    return result.scalar_one()


async def test_cooldown_counter_survives_intent_ttl_expiry(db_session: AsyncSession, db_host: Host) -> None:
    """A cooldown TTL window lapses, then a fresh cooldown lands. The
    counter must accumulate to 2 — not reset to 1 — so the escalation
    threshold is reachable on slow-burn intermittent flakes."""
    device = await create_device(db_session, host_id=db_host.id, name="counter-persists")
    await _seed_node(db_session, device.id)
    run = await create_reserved_run(db_session, name="counter-persists-run", devices=[device])

    # First cooldown.
    excluded_until_1, count_1, escalated_1, threshold = await run_service.cooldown_device(
        db_session,
        run.id,
        device.id,
        reason="probe timeout",
        ttl_seconds=60,
    )
    assert count_1 == 1
    assert not escalated_1
    assert excluded_until_1 is not None
    reservation = await _reservation_for(db_session, device.id)
    assert reservation.cooldown_count == 1
    assert reservation.excluded is True

    # Simulate the cooldown TTL elapsing: backdate the intent expires_at and
    # the entry's excluded_until, then run the reconciler's expired-intent
    # sweep. The reservation should be un-excluded, but cooldown_count must
    # stay sticky.
    past = datetime.now(UTC) - timedelta(seconds=1)
    earlier = past - timedelta(seconds=120)
    cooldown_sources = {
        f"cooldown:node:{run.id}",
        f"cooldown:grid:{run.id}",
        f"cooldown:reservation:{run.id}",
        f"cooldown:recovery:{run.id}",
    }
    # Backdate both bounds together — the computed ``excluded_window`` range
    # column requires excluded_at < excluded_until.
    reservation.excluded_at = earlier
    reservation.excluded_until = past
    await db_session.commit()

    from app.devices.models import DeviceIntent

    intents = (
        (await db_session.execute(select(DeviceIntent).where(DeviceIntent.device_id == device.id))).scalars().all()
    )
    for intent in intents:
        if intent.source in cooldown_sources:
            intent.expires_at = past
    await db_session.commit()

    await _reconcile_expired_intents(db_session)
    await db_session.commit()

    await db_session.refresh(reservation)
    assert reservation.excluded is False
    assert reservation.cooldown_count == 1, "counter must persist across exclusion clear"

    # Second cooldown lands after the first TTL elapsed. Counter accumulates.
    _, count_2, escalated_2, _ = await run_service.cooldown_device(
        db_session,
        run.id,
        device.id,
        reason="probe timeout again",
        ttl_seconds=60,
    )
    assert count_2 == 2, "second cooldown after TTL expiry must yield count=2"
    assert not escalated_2 or threshold == 2

    await db_session.refresh(reservation)
    assert reservation.cooldown_count == 2


async def test_restore_device_to_run_resets_cooldown_counter(db_session: AsyncSession, db_host: Host) -> None:
    """Operator-driven restore is the sanctioned counter-reset point.

    Restore is the cleanup path for hard-excluded reservations (escalated
    cooldowns where ``excluded_until`` is null). When it runs, it must zero
    the cooldown counter so the next reservation cycle starts fresh.
    """
    device = await create_device(db_session, host_id=db_host.id, name="restore-resets")
    await _seed_node(db_session, device.id)
    await create_reserved_run(db_session, name="restore-resets-run", devices=[device])

    # Shape the entry like a post-escalation hard exclusion: cooldown_count
    # accumulated, ``excluded_until=None`` so the restore guard treats it as
    # still excluded and proceeds with the reset.
    reservation = await _reservation_for(db_session, device.id)
    reservation.excluded = True
    reservation.exclusion_reason = "escalated"
    reservation.excluded_at = datetime.now(UTC) - timedelta(minutes=5)
    reservation.excluded_until = None
    reservation.cooldown_count = 3
    await db_session.commit()

    await run_service.restore_device_to_run(db_session, device.id)
    await db_session.refresh(reservation)
    assert reservation.excluded is False
    assert reservation.cooldown_count == 0


async def test_clear_via_reconciler_preserves_counter_under_other_exclusion(
    db_session: AsyncSession, db_host: Host
) -> None:
    """A non-cooldown reservation exclusion intent (priority below cooldown)
    should not walk the counter backwards when it becomes the winner after
    the cooldown intent expires.
    """
    device = await create_device(db_session, host_id=db_host.id, name="no-rewind")
    await _seed_node(db_session, device.id)
    run = await create_reserved_run(db_session, name="no-rewind-run", devices=[device])

    await run_service.cooldown_device(db_session, run.id, device.id, reason="flake", ttl_seconds=60)
    reservation = await _reservation_for(db_session, device.id)
    assert reservation.cooldown_count == 1

    # Health-failure-style intent without a counter value. It has lower
    # priority than cooldown by design but if it wins after the cooldown
    # intent expires, it must not zero the counter.
    service = IntentService(db_session)
    await service.register_intents(
        device_id=device.id,
        reason="seed",
        intents=[
            IntentRegistration(
                source=f"health_failure:reservation:{device.id}",
                axis=RESERVATION,
                run_id=run.id,
                payload={
                    "excluded": True,
                    "priority": 60,
                    "exclusion_reason": "later signal",
                },
            )
        ],
    )
    await db_session.commit()

    await _reconcile_device(db_session, device.id)
    await db_session.commit()

    await db_session.refresh(reservation)
    assert reservation.cooldown_count == 1
