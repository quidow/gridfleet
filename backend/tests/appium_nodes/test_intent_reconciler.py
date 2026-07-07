from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from unittest.mock import ANY, AsyncMock, Mock

import pytest
from sqlalchemy import select

from app.agent_comm.models import AgentReconfigureOutbox
from app.appium_nodes.models import AppiumDesiredState, AppiumNode
from app.core.errors import AgentUnreachableError
from app.core.leader.advisory import LeadershipLost
from app.devices.models import DeviceIntent, DeviceIntentDirty, DeviceOperationalState, DeviceReservation
from app.devices.services import state_write_guard
from app.devices.services.intent import IntentService
from app.devices.services.intent_reconciler import (
    _reconcile_all_devices_once,
    _reconcile_dirty_devices,
    _reconcile_expired_intents,
    _stage_agent_reconfigure,
    reconcile_device,
    run_device_intent_reconciler_once,
)
from app.devices.services.intent_types import GRID_ROUTING, NODE_PROCESS, IntentRegistration
from app.sessions.models import Session, SessionStatus
from tests.fakes import FakeSettingsReader
from tests.fakes.review import build_review_service
from tests.helpers import create_device, create_reserved_run
from tests.helpers import test_event_bus as event_bus

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.hosts.models import Host


async def _seed_node(db_session: AsyncSession, device_id: object, *, generation: int = 0) -> AppiumNode:
    with state_write_guard.bypass():
        node = AppiumNode(
            device_id=device_id,
            port=4723,
            desired_state=AppiumDesiredState.stopped,
            generation=generation,
        )
    db_session.add(node)
    await db_session.commit()
    return node


async def test_baseline_eligible_device_derives_running(db_session: AsyncSession, db_host: Host) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="baseline")
    await _seed_node(db_session, device.id)

    await reconcile_device(db_session, device.id, publisher=event_bus)
    await db_session.commit()

    node = (await db_session.execute(select(AppiumNode).where(AppiumNode.device_id == device.id))).scalar_one()
    assert node.desired_state == AppiumDesiredState.running
    assert node.accepting_new_sessions is True
    assert node.generation == 1


async def test_review_required_device_gets_no_baseline_node(db_session: AsyncSession, db_host: Host) -> None:
    """F-G1: a shelved device (review_required) must not be baseline-started.

    Live finding 2026-06-05: after an update-mode verify failure shelved a
    device, baseline:idle kept desired_state=running and the hub slot stayed
    UP/free for >=180s (S10/G3).
    """
    device = await create_device(db_session, host_id=db_host.id, name="shelved")
    device.review_required = True
    await db_session.commit()
    node = await _seed_node(db_session, device.id)

    await reconcile_device(db_session, device.id, publisher=event_bus)

    await db_session.refresh(node)
    assert node.desired_state == AppiumDesiredState.stopped
    assert node.accepting_new_sessions is False


async def test_cooldown_intents_derive_metadata_reservation_and_recovery(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="cooldown")
    await _seed_node(db_session, device.id)
    run = await create_reserved_run(db_session, name="cooldown run", devices=[device])
    reservation = (
        await db_session.execute(select(DeviceReservation).where(DeviceReservation.device_id == device.id))
    ).scalar_one()
    reservation.excluded = True
    reservation.exclusion_reason = "Device in cooldown"
    reservation.excluded_at = datetime.now(UTC)
    reservation.excluded_until = datetime.now(UTC) + timedelta(minutes=5)
    await db_session.commit()

    await reconcile_device(db_session, device.id, publisher=event_bus)
    await db_session.commit()

    node = (await db_session.execute(select(AppiumNode).where(AppiumNode.device_id == device.id))).scalar_one()
    await db_session.refresh(reservation)
    assert node.desired_state == AppiumDesiredState.running
    assert node.accepting_new_sessions is False
    assert node.desired_grid_run_id == run.id
    assert reservation.excluded is True


async def test_expired_intents_are_deleted_and_reconciled(db_session: AsyncSession, db_host: Host) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="expired")
    await _seed_node(db_session, device.id)
    service = IntentService(db_session)
    await service.register_intents(
        device_id=device.id,
        intents=[
            IntentRegistration(
                source="expired",
                axis=GRID_ROUTING,
                payload={"accepting_new_sessions": False, "priority": 90},
                expires_at=datetime.now(UTC) - timedelta(seconds=1),
            ),
        ],
    )
    await db_session.commit()

    await _reconcile_expired_intents(
        db_session, settings=FakeSettingsReader(), circuit_breaker=Mock(), publisher=event_bus
    )

    intents = (
        (await db_session.execute(select(DeviceIntent).where(DeviceIntent.device_id == device.id))).scalars().all()
    )
    node = (await db_session.execute(select(AppiumNode).where(AppiumNode.device_id == device.id))).scalar_one()
    assert intents == []
    assert node.accepting_new_sessions is True


async def test_expired_running_metadata_change_is_delivered(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="expired-delivery")
    node = await _seed_node(db_session, device.id, generation=4)
    with state_write_guard.bypass():
        node.desired_state = AppiumDesiredState.running
    with state_write_guard.bypass():
        node.desired_port = 4723
    with state_write_guard.bypass():
        node.port = 4723
    with state_write_guard.bypass():
        node.pid = 1234
    with state_write_guard.bypass():
        node.active_connection_target = device.connection_target
    node.accepting_new_sessions = False
    await db_session.commit()
    service = IntentService(db_session)
    await service.register_intents(
        device_id=device.id,
        intents=[
            IntentRegistration(
                source="expired:grid:block",
                axis=GRID_ROUTING,
                payload={"accepting_new_sessions": False, "priority": 90},
                expires_at=datetime.now(UTC) - timedelta(seconds=1),
            ),
        ],
    )
    await db_session.commit()
    reconfigure = AsyncMock()
    monkeypatch.setattr("app.agent_comm.operations.agent_appium_reconfigure", reconfigure)

    settings = FakeSettingsReader()
    await _reconcile_expired_intents(db_session, settings=settings, circuit_breaker=Mock(), publisher=event_bus)

    reconfigure.assert_awaited_once_with(
        db_host.ip,
        db_host.agent_port,
        port=4723,
        accepting_new_sessions=True,
        stop_pending=False,
        grid_run_id=None,
        timeout=10,
        settings=settings,
        pool=None,
        circuit_breaker=ANY,
    )
    outbox = (await db_session.execute(select(AgentReconfigureOutbox))).scalar_one()
    assert outbox.delivered_at is not None


async def test_reconciler_once_forwards_agent_auth_pool(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reconciler loop must thread its agent BasicAuth pool through to the
    delivery call; without it reconfigures are unauthenticated and rejected when
    the auth gate is enabled."""
    pool = Mock()
    device = await create_device(db_session, host_id=db_host.id, name="reconciler-pool")
    node = await _seed_node(db_session, device.id, generation=4)
    with state_write_guard.bypass():
        node.desired_state = AppiumDesiredState.running
        node.desired_port = 4723
        node.port = 4723
        node.pid = 1234
        node.active_connection_target = device.connection_target
    db_session.add(
        AgentReconfigureOutbox(
            device_id=device.id,
            port=4723,
            accepting_new_sessions=True,
            stop_pending=False,
            reconciled_generation=4,
        )
    )
    await db_session.commit()
    reconfigure = AsyncMock(return_value={"port": 4723})
    monkeypatch.setattr("app.agent_comm.operations.agent_appium_reconfigure", reconfigure)
    monkeypatch.setattr("app.devices.services.intent_reconciler.assert_current_leader", AsyncMock())

    await run_device_intent_reconciler_once(
        db_session,
        cycle=1,
        settings=FakeSettingsReader({}),
        circuit_breaker=Mock(),
        publisher=event_bus,
        pool=pool,
    )

    assert reconfigure.await_count >= 1
    assert all(call.kwargs.get("pool") is pool for call in reconfigure.await_args_list)


async def test_pending_reconfigure_from_expired_last_intent_is_retried(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="expired-retry")
    node = await _seed_node(db_session, device.id, generation=4)
    with state_write_guard.bypass():
        node.desired_state = AppiumDesiredState.running
    with state_write_guard.bypass():
        node.desired_port = 4723
    with state_write_guard.bypass():
        node.port = 4723
    with state_write_guard.bypass():
        node.pid = 1234
    with state_write_guard.bypass():
        node.active_connection_target = device.connection_target
    node.accepting_new_sessions = False
    await db_session.commit()
    service = IntentService(db_session)
    [intent] = await service.register_intents(
        device_id=device.id,
        intents=[
            IntentRegistration(
                source="expired:grid:block",
                axis=GRID_ROUTING,
                payload={"accepting_new_sessions": False, "priority": 90},
                expires_at=datetime.now(UTC) + timedelta(minutes=5),
            ),
        ],
    )
    await db_session.commit()
    await _reconcile_dirty_devices(
        db_session, limit=10, settings=FakeSettingsReader(), circuit_breaker=Mock(), publisher=event_bus
    )
    intent.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await db_session.commit()
    reconfigure = AsyncMock(side_effect=[AgentUnreachableError(db_host.ip, "offline"), {"port": 4723}])
    monkeypatch.setattr("app.agent_comm.operations.agent_appium_reconfigure", reconfigure)
    monkeypatch.setattr("app.devices.services.intent_reconciler.assert_current_leader", AsyncMock())

    await _reconcile_expired_intents(
        db_session, settings=FakeSettingsReader(), circuit_breaker=Mock(), publisher=event_bus
    )

    outbox = (await db_session.execute(select(AgentReconfigureOutbox))).scalar_one()
    dirty_rows = (await db_session.execute(select(DeviceIntentDirty))).scalars().all()
    intents = (
        (await db_session.execute(select(DeviceIntent).where(DeviceIntent.device_id == device.id))).scalars().all()
    )
    assert outbox.delivered_at is None
    assert outbox.delivery_attempts == 1
    assert dirty_rows == []
    assert intents == []

    await run_device_intent_reconciler_once(
        db_session, cycle=1, settings=FakeSettingsReader({}), circuit_breaker=Mock(), publisher=event_bus
    )

    await db_session.refresh(outbox)
    assert outbox.delivered_at is not None
    assert outbox.delivery_attempts == 1
    assert reconfigure.await_count == 2


async def test_graceful_stop_stages_agent_drain_before_convergence_can_stop(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="graceful")
    node = await _seed_node(db_session, device.id, generation=2)
    with state_write_guard.bypass():
        node.desired_state = AppiumDesiredState.running
    with state_write_guard.bypass():
        node.desired_port = 4723
    with state_write_guard.bypass():
        node.port = 4723
    with state_write_guard.bypass():
        node.pid = 1234
    with state_write_guard.bypass():
        node.active_connection_target = device.connection_target
    await db_session.commit()
    service = IntentService(db_session)
    await service.register_intents(
        device_id=device.id,
        intents=[
            IntentRegistration(
                source="maintenance:node",
                axis=NODE_PROCESS,
                payload={"action": "stop", "stop_mode": "graceful", "priority": 80},
            ),
        ],
    )
    await db_session.commit()

    await reconcile_device(db_session, device.id, publisher=event_bus)
    await db_session.commit()

    await db_session.refresh(node)
    outbox = (await db_session.execute(select(AgentReconfigureOutbox))).scalar_one()
    assert node.desired_state == AppiumDesiredState.stopped
    assert node.stop_pending is True
    assert node.accepting_new_sessions is False
    assert outbox.port == 4723
    assert outbox.stop_pending is True
    assert outbox.accepting_new_sessions is False


async def test_hard_stop_on_idle_device_stages_agent_drain(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    """A hard stop of an idle device must stage a drain reconfigure (N7).

    ``operational_state`` derives to ``offline`` synchronously from
    ``desired_state=stopped`` (``stop_in_flight``), but the relay keeps running
    and registered to the hub until the appium reconciler tears it down. Without
    a drain pushed to the agent, the hub keeps routing and a direct/free session
    lands on the now-offline device → ``busy`` (the ``session_on_non_available``
    gating violation). The hard-stop path must stage the same drain the graceful
    path does, even though ``desired_state=stopped`` and ``stop_pending=False``.
    """
    device = await create_device(db_session, host_id=db_host.id, name="hard-stop")
    node = await _seed_node(db_session, device.id, generation=2)
    with state_write_guard.bypass():
        node.desired_state = AppiumDesiredState.running
    with state_write_guard.bypass():
        node.desired_port = 4723
    with state_write_guard.bypass():
        node.port = 4723
    with state_write_guard.bypass():
        node.pid = 1234
    with state_write_guard.bypass():
        node.active_connection_target = device.connection_target
    await db_session.commit()
    service = IntentService(db_session)
    await service.register_intents(
        device_id=device.id,
        intents=[
            IntentRegistration(
                source="operator:node",
                axis=NODE_PROCESS,
                payload={"action": "stop", "stop_mode": "hard", "priority": 90},
            ),
        ],
    )
    await db_session.commit()

    await reconcile_device(db_session, device.id, publisher=event_bus)
    await db_session.commit()

    await db_session.refresh(node)
    assert node.desired_state == AppiumDesiredState.stopped
    assert node.stop_pending is False
    assert node.accepting_new_sessions is False
    outbox = (await db_session.execute(select(AgentReconfigureOutbox))).scalar_one()
    assert outbox.port == 4723
    assert outbox.accepting_new_sessions is False


async def test_graceful_stop_holds_node_running_while_session_active(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    """A graceful stop intent must not flip ``desired_state=stopped`` while a
    client session is running on the device.

    The convergence layer's ``stop_pending`` no-op currently guards the kill,
    but it is the only safety net. This test pins the contract that the
    intent reconciler itself holds the soft-stop while a session is active —
    so any caller (heartbeat-driven crash, future code path, evaluator change)
    that forgets to set ``stop_pending`` cannot terminate the relay mid-session.
    """
    device = await create_device(db_session, host_id=db_host.id, name="graceful-session")
    node = await _seed_node(db_session, device.id, generation=2)
    with state_write_guard.bypass():
        node.desired_state = AppiumDesiredState.running
    with state_write_guard.bypass():
        node.desired_port = 4723
    with state_write_guard.bypass():
        node.port = 4723
    with state_write_guard.bypass():
        node.pid = 1234
    with state_write_guard.bypass():
        node.active_connection_target = device.connection_target
    db_session.add(Session(session_id="active-sess-1", device_id=device.id, status=SessionStatus.running))
    await db_session.commit()
    service = IntentService(db_session)
    await service.register_intents(
        device_id=device.id,
        intents=[
            IntentRegistration(
                source=f"health_failure:node:{device.id}",
                axis=NODE_PROCESS,
                payload={"action": "stop", "stop_mode": "graceful", "priority": 60},
            ),
        ],
    )
    await db_session.commit()

    await reconcile_device(db_session, device.id, publisher=event_bus)
    await db_session.commit()

    await db_session.refresh(node)
    assert node.desired_state == AppiumDesiredState.running
    assert node.stop_pending is True
    assert node.accepting_new_sessions is False


async def test_graceful_stop_holds_node_running_while_session_pending(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    """A ``pending`` session (the allocate->confirm window) must defer a graceful stop
    the same way a ``running`` session does (F1). Killing the Appium process mid-create
    fails the in-flight session creation.
    """
    device = await create_device(db_session, host_id=db_host.id, name="graceful-pending")
    node = await _seed_node(db_session, device.id, generation=2)
    with state_write_guard.bypass():
        node.desired_state = AppiumDesiredState.running
    with state_write_guard.bypass():
        node.desired_port = 4723
    with state_write_guard.bypass():
        node.port = 4723
    with state_write_guard.bypass():
        node.pid = 1234
    with state_write_guard.bypass():
        node.active_connection_target = device.connection_target
    db_session.add(Session(session_id="alloc-pending-1", device_id=device.id, status=SessionStatus.pending))
    await db_session.commit()
    service = IntentService(db_session)
    await service.register_intents(
        device_id=device.id,
        intents=[
            IntentRegistration(
                source=f"health_failure:node:{device.id}",
                axis=NODE_PROCESS,
                payload={"action": "stop", "stop_mode": "graceful", "priority": 60},
            ),
        ],
    )
    await db_session.commit()

    await reconcile_device(db_session, device.id, publisher=event_bus)
    await db_session.commit()

    await db_session.refresh(node)
    assert node.desired_state == AppiumDesiredState.running
    assert node.stop_pending is True
    assert node.accepting_new_sessions is False


async def test_graceful_stop_applies_once_session_ends(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    """After the held session ends, the next reconcile must apply the held
    graceful stop and flip ``desired_state=stopped`` with ``stop_pending=True``.
    """
    device = await create_device(db_session, host_id=db_host.id, name="graceful-end")
    node = await _seed_node(db_session, device.id, generation=2)
    with state_write_guard.bypass():
        node.desired_state = AppiumDesiredState.running
    with state_write_guard.bypass():
        node.desired_port = 4723
    with state_write_guard.bypass():
        node.port = 4723
    with state_write_guard.bypass():
        node.pid = 1234
    with state_write_guard.bypass():
        node.active_connection_target = device.connection_target
    session = Session(session_id="ending-sess-1", device_id=device.id, status=SessionStatus.running)
    db_session.add(session)
    await db_session.commit()
    service = IntentService(db_session)
    await service.register_intents(
        device_id=device.id,
        intents=[
            IntentRegistration(
                source=f"health_failure:node:{device.id}",
                axis=NODE_PROCESS,
                payload={"action": "stop", "stop_mode": "graceful", "priority": 60},
            ),
        ],
    )
    await db_session.commit()

    await reconcile_device(db_session, device.id, publisher=event_bus)
    await db_session.commit()
    await db_session.refresh(node)
    assert node.desired_state == AppiumDesiredState.running

    session.status = SessionStatus.passed
    session.ended_at = datetime.now(UTC)
    await db_session.commit()
    await reconcile_device(db_session, device.id, publisher=event_bus)
    await db_session.commit()

    await db_session.refresh(node)
    assert node.desired_state == AppiumDesiredState.stopped
    assert node.stop_pending is True
    assert node.accepting_new_sessions is False


async def test_metadata_only_running_change_stages_outbox(db_session: AsyncSession, db_host: Host) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="metadata")
    node = await _seed_node(db_session, device.id, generation=7)
    with state_write_guard.bypass():
        node.desired_state = AppiumDesiredState.running
    with state_write_guard.bypass():
        node.desired_port = 4723
    await db_session.commit()
    service = IntentService(db_session)
    await service.register_intents(
        device_id=device.id,
        intents=[
            IntentRegistration(
                source="grid:block",
                axis=GRID_ROUTING,
                payload={"accepting_new_sessions": False, "priority": 80},
            ),
        ],
    )
    await db_session.commit()

    await reconcile_device(db_session, device.id, publisher=event_bus)
    await db_session.commit()

    outbox = (await db_session.execute(select(AgentReconfigureOutbox))).scalar_one()
    assert outbox.reconciled_generation == 8
    assert outbox.accepting_new_sessions is False


async def test_stage_agent_reconfigure_dedupes_identical_undelivered_generation(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="stage-dedupe")
    node = await _seed_node(db_session, device.id, generation=3)
    with state_write_guard.bypass():
        node.port = 4723
    node.accepting_new_sessions = False
    node.stop_pending = True
    await db_session.commit()

    await _stage_agent_reconfigure(db_session, node)
    await _stage_agent_reconfigure(db_session, node)
    await db_session.commit()

    rows = (await db_session.execute(select(AgentReconfigureOutbox))).scalars().all()
    assert len(rows) == 1
    assert rows[0].reconciled_generation == 3
    assert rows[0].accepting_new_sessions is False
    assert rows[0].stop_pending is True


async def test_dirty_generation_not_deleted_when_incremented_during_reconcile(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="dirty")
    await _seed_node(db_session, device.id)
    dirty = DeviceIntentDirty(device_id=device.id, generation=1)
    db_session.add(dirty)
    await db_session.commit()

    async def fake_reconcile(
        db: AsyncSession, device_id: object, *, publisher: object = None, packs: object = None
    ) -> None:
        row = await db.get(DeviceIntentDirty, device_id)
        assert row is not None
        row.generation += 1
        await db.flush()

    monkeypatch.setattr("app.devices.services.intent_reconciler.reconcile_device", fake_reconcile)

    await _reconcile_dirty_devices(
        db_session, limit=10, settings=FakeSettingsReader(), circuit_breaker=Mock(), publisher=event_bus
    )
    await db_session.commit()

    assert await db_session.get(DeviceIntentDirty, device.id) is not None


async def test_full_scan_reconciles_each_intent_device(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = await create_device(db_session, host_id=db_host.id, name="full-scan-a")
    second = await create_device(db_session, host_id=db_host.id, name="full-scan-b")
    db_session.add_all(
        [
            DeviceIntent(device_id=first.id, source="test-a", axis=NODE_PROCESS, payload={}),
            DeviceIntent(device_id=second.id, source="test-b", axis=NODE_PROCESS, payload={}),
        ]
    )
    await db_session.commit()
    reconciled: list[object] = []
    deliver = AsyncMock()

    async def fake_reconcile(
        _db: AsyncSession, device_id: object, *, publisher: object = None, packs: object = None
    ) -> None:
        reconciled.append(device_id)

    monkeypatch.setattr("app.devices.services.intent_reconciler.reconcile_device", fake_reconcile)
    monkeypatch.setattr("app.devices.services.intent_reconciler.deliver_agent_reconfigures", deliver)

    await _reconcile_all_devices_once(
        db_session, settings=FakeSettingsReader(), circuit_breaker=Mock(), publisher=event_bus
    )

    assert set(reconciled) == {first.id, second.id}
    assert deliver.await_count == 2


async def test_full_scan_recovers_non_available_device_without_intents(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A device stranded in a non-``available`` state with NO backing intent — the
    state a worker crash can leave between the bare ``verifying`` push and lease
    registration (spec §14.5: every state must be backed by a durable fact). The
    authoritative full scan must re-derive it even though it has no DeviceIntent
    rows, while a steady-state ``available`` device with no intents stays skipped.
    """
    orphan = await create_device(db_session, host_id=db_host.id, name="orphan-verifying")
    idle = await create_device(db_session, host_id=db_host.id, name="idle-available")
    with state_write_guard.bypass():
        orphan.operational_state = DeviceOperationalState.verifying
        idle.operational_state = DeviceOperationalState.available
    await db_session.commit()
    # Neither device has any intents — the pre-fix scan would skip both.
    assert not (await db_session.execute(select(DeviceIntent))).first()

    reconciled: list[object] = []

    async def fake_reconcile(
        _db: AsyncSession, device_id: object, *, publisher: object = None, packs: object = None
    ) -> None:
        reconciled.append(device_id)

    monkeypatch.setattr("app.devices.services.intent_reconciler.reconcile_device", fake_reconcile)
    monkeypatch.setattr("app.devices.services.intent_reconciler.deliver_agent_reconfigures", AsyncMock())

    await _reconcile_all_devices_once(
        db_session, settings=FakeSettingsReader(), circuit_breaker=Mock(), publisher=event_bus
    )

    assert orphan.id in reconciled  # re-derived despite having no intents
    assert idle.id not in reconciled  # steady-state available is still skipped


async def test_reconciler_cycle_checks_leadership_before_writes(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reconcile_expired = AsyncMock()
    monkeypatch.setattr("app.devices.services.intent_reconciler._reconcile_expired_intents", reconcile_expired)
    monkeypatch.setattr(
        "app.devices.services.intent_reconciler.assert_current_leader",
        AsyncMock(side_effect=LeadershipLost("lost")),
    )

    with pytest.raises(LeadershipLost):
        await run_device_intent_reconciler_once(
            db_session, cycle=1, settings=FakeSettingsReader({}), circuit_breaker=Mock(), publisher=event_bus
        )
    reconcile_expired.assert_not_awaited()


async def test_maintenance_signal_suppresses_baseline_idle_injection(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    """Baseline:idle must NOT be injected when the maintenance_reason signal is set,
    even if hold is NULL (i.e., the derived hold column has not yet been written)."""
    from app.devices.services.lifecycle_policy_state import set_maintenance_reason

    device = await create_device(db_session, host_id=db_host.id, name="maintenance-idle")
    node = await _seed_node(db_session, device.id)

    # Set maintenance_reason signal directly; leave device.hold as NULL.
    set_maintenance_reason(device, "test maintenance")
    await db_session.commit()

    await reconcile_device(db_session, device.id, publisher=event_bus)
    await db_session.commit()

    await db_session.refresh(node)
    # Node must remain stopped — no baseline:idle should have been injected.
    assert node.desired_state == AppiumDesiredState.stopped


@pytest.mark.parametrize(
    ("verified", "maintenance", "review"),
    [
        (False, False, False),  # unverified
        (True, True, False),  # maintenance
        (True, False, True),  # review-shelved (F-G1)
        (True, True, True),  # both withdrawal flags
    ],
)
async def test_withdrawn_device_never_gets_baseline_node(
    db_session: AsyncSession, db_host: Host, verified: bool, maintenance: bool, review: bool
) -> None:
    """Invariant: withdrawn-from-service => no baseline-started node.

    Drift-guard for device_in_service — if a new withdrawal fact is added to
    state derivation without updating the predicate, extend this matrix.
    """
    device = await create_device(db_session, host_id=db_host.id, name="withdrawn")
    if not verified:
        device.verified_at = None
    device.review_required = review
    if maintenance:
        with state_write_guard.bypass():
            device.lifecycle_policy_state = {**(device.lifecycle_policy_state or {}), "maintenance_reason": "operator"}
    await db_session.commit()
    node = await _seed_node(db_session, device.id)

    await reconcile_device(db_session, device.id, publisher=event_bus)

    await db_session.refresh(node)
    assert node.desired_state == AppiumDesiredState.stopped
    assert node.accepting_new_sessions is False


async def test_recovery_promoted_review_device_gets_no_baseline_node(db_session: AsyncSession, db_host: Host) -> None:
    """A device promoted to review_required by session_viability (recovery
    threshold) must drop out of baseline node starts — pre-fix, suppressed
    recovery left no intents and baseline:idle kept restarting the node."""
    device = await create_device(db_session, host_id=db_host.id, name="promoted")
    node = await _seed_node(db_session, device.id)

    review = build_review_service()
    marked = await review.mark_review_required(
        db_session, device, reason="Recovery probe failed", source="session_viability"
    )
    assert marked is True
    await db_session.commit()

    await reconcile_device(db_session, device.id, publisher=event_bus)

    await db_session.refresh(node)
    assert node.desired_state == AppiumDesiredState.stopped


async def test_no_intent_stop_holds_node_running_while_session_active(db_session: AsyncSession, db_host: Host) -> None:
    """Only an explicit stop_mode='hard' may flip desired_state=stopped while
    a client session is active. The no-intent stop (withdrawn device, F-G1
    gate) must defer like a graceful stop."""
    device = await create_device(db_session, host_id=db_host.id, name="busy-shelved")
    device.review_required = True
    await db_session.commit()
    node = await _seed_node(db_session, device.id)
    with state_write_guard.bypass():
        node.desired_state = AppiumDesiredState.running
    db_session.add(Session(session_id="s-1", device_id=device.id, status=SessionStatus.running))
    await db_session.commit()

    await reconcile_device(db_session, device.id, publisher=event_bus)

    await db_session.refresh(node)
    assert node.desired_state == AppiumDesiredState.running
    assert node.accepting_new_sessions is False
    assert node.stop_pending is True


async def test_start_intent_stale_payload_port_is_overridden_by_live_node_port(
    db_session: AsyncSession, db_host: Host
) -> None:
    """N11 churn co-defect (2026-06-07): a persistent start intent pins desired_port in its
    payload at registration time. A fallback start later moves the node (observation updates
    node.port and clears desired_port), after which re-applying the snapshot flips desired_port
    against the live port on every reconcile (the 4724<->4725 storm) and convergence
    force-restarts the node onto the stale port. The node row is the single source of port
    truth — the applier must pin live node.port, not the payload snapshot."""
    device = await create_device(db_session, host_id=db_host.id, name="port-pin", verified=True)
    node = await _seed_node(db_session, device.id)
    with state_write_guard.bypass():
        node.port = 4725  # fallback start moved the node here; payload below predates the move
    await db_session.commit()
    await IntentService(db_session).register_intents(
        device_id=device.id,
        intents=[
            IntentRegistration(
                source=f"operator:start:{device.id}",
                axis=NODE_PROCESS,
                payload={"action": "start", "priority": 20, "desired_port": 4723},
            )
        ],
    )

    await reconcile_device(db_session, device.id, publisher=event_bus)

    await db_session.refresh(node)
    assert node.desired_state == AppiumDesiredState.running
    assert node.desired_port == 4725, (
        f"applier must pin live node.port over the stale payload snapshot; got {node.desired_port}"
    )


async def test_full_scan_corrects_drifted_offline_device_end_to_end(
    db_session: AsyncSession,
    db_host: Host,
    seeded_driver_packs: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Safety net for transition-gating: a device with healthy facts whose
    operational_state drifted to offline (no dirty row, no observation transition)
    is re-derived off offline by the authoritative full scan."""
    device = await create_device(db_session, host_id=db_host.id, name="drifted")
    await _seed_node(db_session, device.id)
    # Make the node observed-running and the device-checks healthy so the reconciler
    # derives a non-offline state.
    node = (await db_session.execute(select(AppiumNode).where(AppiumNode.device_id == device.id))).scalar_one()
    with state_write_guard.bypass():
        node.pid = 1
        node.active_connection_target = "target"
        node.health_running = True
        device.device_checks_healthy = True
        device.operational_state = DeviceOperationalState.offline  # the drift
    await db_session.commit()

    monkeypatch.setattr("app.devices.services.intent_reconciler.deliver_agent_reconfigures", AsyncMock())

    await _reconcile_all_devices_once(
        db_session, settings=FakeSettingsReader(), circuit_breaker=Mock(), publisher=event_bus
    )
    await db_session.refresh(device)
    assert device.operational_state != DeviceOperationalState.offline  # drift corrected
