"""Phase 1 — the two-axis write-ordering guard and observation idempotency.

Both moved folds (node health, device health) share one rule: a writer applies
its verdict only when its revision is strictly greater than the axis's stored
revision. A synchronous higher-authority writer (restart ingest, host-offline
cascade, lifecycle crash, create-failure) passes no revision and draws a fresh
one at write time, so it always out-ranks a stale fold observation whose (lower)
revision was drawn earlier at ingest.
"""

from typing import TYPE_CHECKING

from app.core.observation_revision import next_observation_revision
from app.core.timeutil import parse_iso
from app.devices.services.health import DeviceHealthService
from tests.helpers import drain_handlers, seed_host_and_device, seed_host_and_running_node
from tests.helpers import test_event_bus as event_bus

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


# --------------------------------------------------------------------------- #
# Node axis: health_running / health_state
# --------------------------------------------------------------------------- #


async def test_node_axis_synchronous_racer_beats_stale_fold(db_session: AsyncSession) -> None:
    _host, device, node = await seed_host_and_running_node(db_session, identity="node-race")
    svc = DeviceHealthService(publisher=event_bus)

    # A fold observation is stamped with an ingest revision drawn now...
    fold_revision = await next_observation_revision(db_session)

    # ...but a synchronous racer (restart ingest / create-failure) writes first
    # with a fresh, strictly-greater revision.
    await svc.apply_node_state_transition(
        db_session, device, health_running=False, health_state="error", mark_offline=False
    )
    await db_session.commit()
    await db_session.refresh(node)
    assert node.health_running is False
    assert node.health_observation_revision > fold_revision
    racer_revision = node.health_observation_revision

    # The now-stale fold observation must not revive the node.
    await svc.apply_node_state_transition(
        db_session, device, health_running=True, health_state=None, mark_offline=False, revision=fold_revision
    )
    await db_session.commit()
    await db_session.refresh(node)
    assert node.health_running is False
    assert node.health_observation_revision == racer_revision


async def test_node_axis_equal_revision_skips_and_greater_applies(db_session: AsyncSession) -> None:
    _host, device, node = await seed_host_and_running_node(db_session, identity="node-eq")
    svc = DeviceHealthService(publisher=event_bus)

    await svc.apply_node_state_transition(
        db_session, device, health_running=True, health_state=None, mark_offline=False, revision=100
    )
    await db_session.commit()
    await db_session.refresh(node)
    assert node.health_running is True
    assert node.health_observation_revision == 100

    # Equal revision = already applied → skip even though the verdict differs.
    await svc.apply_node_state_transition(
        db_session, device, health_running=False, health_state="error", mark_offline=False, revision=100
    )
    await db_session.commit()
    await db_session.refresh(node)
    assert node.health_running is True

    # Strictly greater → applies.
    await svc.apply_node_state_transition(
        db_session, device, health_running=False, health_state="error", mark_offline=False, revision=101
    )
    await db_session.commit()
    await db_session.refresh(node)
    assert node.health_running is False
    assert node.health_observation_revision == 101


async def test_node_axis_persists_observed_at_and_is_replay_safe(db_session: AsyncSession) -> None:
    _host, device, node = await seed_host_and_running_node(db_session, identity="node-idem")
    svc = DeviceHealthService(publisher=event_bus)
    observed = parse_iso("2026-07-14T10:00:00+00:00")

    await svc.apply_node_state_transition(
        db_session,
        device,
        health_running=True,
        health_state=None,
        mark_offline=False,
        revision=200,
        observed_at=observed,
    )
    await db_session.commit()
    await db_session.refresh(node)
    assert node.last_health_checked_at == observed
    assert node.health_observation_revision == 200

    # Replay of the same generation is a no-op: equal revision skips, so the
    # persisted checked-at stamp does not drift.
    await svc.apply_node_state_transition(
        db_session,
        device,
        health_running=False,
        health_state="error",
        mark_offline=False,
        revision=200,
        observed_at=parse_iso("2026-07-14T10:05:00+00:00"),
    )
    await db_session.commit()
    await db_session.refresh(node)
    assert node.health_running is True
    assert node.last_health_checked_at == observed


# --------------------------------------------------------------------------- #
# Device axis: device_checks_healthy / summary / checked_at
# --------------------------------------------------------------------------- #


async def test_device_axis_synchronous_racer_beats_stale_fold(db_session: AsyncSession) -> None:
    _host, device = await seed_host_and_device(db_session, identity="dev-race")
    svc = DeviceHealthService(publisher=event_bus)

    fold_revision = await next_observation_revision(db_session)

    # Host-offline cascade / lifecycle crash draws a fresh revision and marks down.
    await svc.update_device_checks(db_session, device, healthy=False, summary="Host offline")
    await db_session.commit()
    await db_session.refresh(device)
    assert device.device_checks_healthy is False
    assert device.device_checks_observation_revision > fold_revision
    racer_revision = device.device_checks_observation_revision

    # A stale device_health fold observation must not revive the device.
    await svc.update_device_checks(db_session, device, healthy=True, summary="Healthy", revision=fold_revision)
    await db_session.commit()
    await db_session.refresh(device)
    assert device.device_checks_healthy is False
    assert device.device_checks_observation_revision == racer_revision


async def test_device_axis_persists_observed_at_and_is_replay_safe(db_session: AsyncSession) -> None:
    _host, device = await seed_host_and_device(db_session, identity="dev-idem")
    svc = DeviceHealthService(publisher=event_bus)
    observed = parse_iso("2026-07-14T10:00:00+00:00")

    await svc.update_device_checks(
        db_session, device, healthy=False, summary="Disconnected", revision=300, observed_at=observed
    )
    await db_session.commit()
    await db_session.refresh(device)
    assert device.device_checks_healthy is False
    assert device.device_checks_summary == "Disconnected"
    assert device.device_checks_checked_at == observed

    # Applying the same observation twice (equal revision) writes identical facts
    # and queues no second event.
    await drain_handlers(event_bus)
    _, baseline_total = await event_bus.get_recent_events_persisted(limit=50)

    await svc.update_device_checks(
        db_session,
        device,
        healthy=True,
        summary="Healthy",
        revision=300,
        observed_at=parse_iso("2026-07-14T10:05:00+00:00"),
    )
    await db_session.commit()
    await db_session.refresh(device)
    assert device.device_checks_healthy is False
    assert device.device_checks_summary == "Disconnected"
    assert device.device_checks_checked_at == observed

    await drain_handlers(event_bus)
    _, replay_total = await event_bus.get_recent_events_persisted(limit=50)
    assert replay_total == baseline_total
