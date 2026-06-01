from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import select

from app.appium_nodes.models import AppiumDesiredState, AppiumNode
from app.devices.models import DeviceIntent
from app.devices.services import state_write_guard
from app.devices.services.intent import IntentService
from app.devices.services.intent_preconditions import reconcile_unsatisfied_preconditions
from app.devices.services.intent_reconciler import reconcile_device
from app.devices.services.intent_types import NODE_PROCESS, IntentRegistration
from app.runs.models import RunState
from tests.fakes import FakeSettingsReader
from tests.helpers import create_device, create_reserved_run

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.hosts.models import Host


async def _seed_node(db_session: AsyncSession, device_id: uuid.UUID) -> AppiumNode:
    with state_write_guard.bypass():
        node = AppiumNode(
            device_id=device_id,
            port=4723,
            grid_url="http://grid:4444",
            desired_state=AppiumDesiredState.stopped,
        )
    db_session.add(node)
    await db_session.commit()
    return node


@pytest.mark.db
async def test_sweep_deletes_intent_when_run_terminal(db_session: AsyncSession, db_host: Host) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="sweep-term")
    await _seed_node(db_session, device.id)
    run = await create_reserved_run(db_session, name="sweep-run", devices=[device])
    await IntentService(db_session).register_intents(
        device_id=device.id,
        reason="cooldown",
        intents=[
            IntentRegistration(
                source=f"cooldown:node:{run.id}",
                axis=NODE_PROCESS,
                run_id=run.id,
                payload={"action": "stop", "stop_mode": "defer", "priority": 70},
                precondition={"kind": "run_active", "run_id": str(run.id)},
            )
        ],
    )
    run.state = RunState.completed
    await db_session.commit()

    await reconcile_unsatisfied_preconditions(db_session)
    await db_session.commit()

    rows = (await db_session.execute(select(DeviceIntent).where(DeviceIntent.device_id == device.id))).scalars().all()
    assert rows == []


@pytest.mark.db
async def test_sweep_leaves_satisfied_intents_intact(db_session: AsyncSession, db_host: Host) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="sweep-keep")
    await _seed_node(db_session, device.id)
    run = await create_reserved_run(db_session, name="sweep-keep-run", devices=[device])
    run.state = RunState.active
    await IntentService(db_session).register_intents(
        device_id=device.id,
        reason="cooldown",
        intents=[
            IntentRegistration(
                source=f"cooldown:node:{run.id}",
                axis=NODE_PROCESS,
                run_id=run.id,
                payload={"action": "stop", "stop_mode": "defer", "priority": 70},
                precondition={"kind": "run_active", "run_id": str(run.id)},
            )
        ],
    )
    await db_session.commit()

    await reconcile_unsatisfied_preconditions(db_session)
    await db_session.commit()

    rows = (await db_session.execute(select(DeviceIntent).where(DeviceIntent.device_id == device.id))).scalars().all()
    assert len(rows) == 1


@pytest.mark.db
async def test_sweep_is_noop_when_no_precondition_intents(db_session: AsyncSession, db_host: Host) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="sweep-noop")
    await _seed_node(db_session, device.id)
    await IntentService(db_session).register_intents(
        device_id=device.id,
        reason="plain",
        intents=[
            IntentRegistration(
                source="baseline:start",
                axis=NODE_PROCESS,
                payload={"action": "start", "priority": 10},
                precondition=None,
            )
        ],
    )
    await db_session.commit()

    await reconcile_unsatisfied_preconditions(db_session)
    await db_session.commit()

    rows = (await db_session.execute(select(DeviceIntent).where(DeviceIntent.device_id == device.id))).scalars().all()
    assert len(rows) == 1


@pytest.mark.db
async def test_sweep_and_expires_at_are_independent_paths(db_session: AsyncSession, db_host: Host) -> None:
    from datetime import timedelta

    from app.devices.services.intent_reconciler import _reconcile_expired_intents

    device = await create_device(db_session, host_id=db_host.id, name="sweep-orthogonal")
    await _seed_node(db_session, device.id)
    run = await create_reserved_run(db_session, name="sweep-orthogonal-run", devices=[device])
    run.state = RunState.active
    await IntentService(db_session).register_intents(
        device_id=device.id,
        reason="hybrid",
        intents=[
            IntentRegistration(
                source=f"cooldown:node:{run.id}",
                axis=NODE_PROCESS,
                run_id=run.id,
                expires_at=datetime.now(UTC) - timedelta(seconds=1),
                payload={"action": "stop", "stop_mode": "defer", "priority": 70},
                precondition={"kind": "run_active", "run_id": str(run.id)},
            )
        ],
    )
    await db_session.commit()

    await _reconcile_expired_intents(db_session, settings=FakeSettingsReader(), circuit_breaker=Mock())
    await db_session.commit()

    remaining = (
        (await db_session.execute(select(DeviceIntent).where(DeviceIntent.device_id == device.id))).scalars().all()
    )
    assert remaining == []


@pytest.mark.db
async def test_sweep_returns_affected_device_only(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    device_a = await create_device(db_session, host_id=db_host.id, name="sweep-a")
    device_b = await create_device(db_session, host_id=db_host.id, name="sweep-b")
    await _seed_node(db_session, device_a.id)
    await _seed_node(db_session, device_b.id)
    run = await create_reserved_run(db_session, name="run-a", devices=[device_a])
    await IntentService(db_session).register_intents(
        device_id=device_a.id,
        reason="cooldown",
        intents=[
            IntentRegistration(
                source=f"cooldown:node:{run.id}",
                axis=NODE_PROCESS,
                run_id=run.id,
                payload={"action": "stop", "stop_mode": "defer", "priority": 70},
                precondition={"kind": "run_active", "run_id": str(run.id)},
            )
        ],
    )
    run.state = RunState.completed
    await db_session.commit()

    affected = await reconcile_unsatisfied_preconditions(db_session)
    await db_session.commit()
    assert affected == {device_a.id}


@pytest.mark.db
async def test_sweep_emits_desired_state_changed_event(db_session: AsyncSession, db_host: Host) -> None:
    from app.devices.models import DeviceEvent, DeviceEventType

    device = await create_device(db_session, host_id=db_host.id, name="sweep-event")
    await _seed_node(db_session, device.id)
    run = await create_reserved_run(db_session, name="sweep-event-run", devices=[device])
    await IntentService(db_session).register_intents(
        device_id=device.id,
        reason="cooldown",
        intents=[
            IntentRegistration(
                source=f"cooldown:node:{run.id}",
                axis=NODE_PROCESS,
                run_id=run.id,
                payload={"action": "stop", "priority": 70},
                precondition={"kind": "run_active", "run_id": str(run.id)},
            )
        ],
    )
    run.state = RunState.completed
    await db_session.commit()

    await reconcile_unsatisfied_preconditions(db_session)
    await db_session.commit()

    events = (
        (
            await db_session.execute(
                select(DeviceEvent)
                .where(DeviceEvent.device_id == device.id)
                .where(DeviceEvent.event_type == DeviceEventType.desired_state_changed)
            )
        )
        .scalars()
        .all()
    )
    details = [event.details for event in events]
    assert any(
        detail is not None
        and detail.get("caller") == "intent_reconciler"
        and detail.get("reason") == "precondition_unsatisfied"
        and detail.get("intent_source") == f"cooldown:node:{run.id}"
        and detail.get("precondition_kind") == "run_active"
        for detail in details
    )


@pytest.mark.db
async def test_cooldown_intents_carry_run_active_precondition(db_session: AsyncSession, db_host: Host) -> None:
    from uuid import uuid4

    from app.runs.service_lifecycle_failures import _cooldown_intents

    run_id = uuid4()
    intents = _cooldown_intents(
        run_id=run_id,
        reason="flaky",
        count=1,
        expires_at=datetime.now(UTC),
    )
    sources = {intent.source for intent in intents}
    assert sources == {
        f"cooldown:node:{run_id}",
        f"cooldown:grid:{run_id}",
        f"cooldown:reservation:{run_id}",
        f"cooldown:recovery:{run_id}",
    }
    for intent in intents:
        assert intent.precondition == {"kind": "run_active", "run_id": str(run_id)}


@pytest.mark.db
async def test_forced_release_registers_run_active_precondition(
    db_session: AsyncSession, db_host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.runs.models import TestRun
    from app.runs.service_lifecycle_release import RunReleaseService
    from tests.fakes import FakeSettingsReader

    captured: list[IntentRegistration] = []

    async def fake_register(
        _self: object,
        *,
        device_id: uuid.UUID,
        intents: list[IntentRegistration],
        reason: str,
    ) -> None:
        captured.extend(intents)

    async def fake_revoke(
        _self: object,
        *,
        device_id: uuid.UUID,
        sources: list[str],
        reason: str,
    ) -> None:
        return None

    monkeypatch.setattr(IntentService, "register_intents_and_reconcile", fake_register)
    monkeypatch.setattr(IntentService, "revoke_intents_and_reconcile", fake_revoke)

    device = await create_device(db_session, host_id=db_host.id, name="forced-reg")
    run = await create_reserved_run(db_session, name="forced-reg-run", devices=[device])
    refreshed_run = await db_session.get(TestRun, run.id)
    assert refreshed_run is not None

    from unittest.mock import AsyncMock as _AsyncMock

    _pub = _AsyncMock()
    _release_svc = RunReleaseService(
        publisher=_pub,
        settings=FakeSettingsReader({}),
        grid=_AsyncMock(),
        deferred_stop=_AsyncMock(),
    )
    await _release_svc.clear_desired_grid_run_id_for_run(
        db_session,
        run=refreshed_run,
        caller="run_force_release",
        reason="test",
    )
    forced = [intent for intent in captured if intent.source.startswith("forced_release:")]
    assert len(forced) == 1
    assert forced[0].precondition == {"kind": "run_active", "run_id": str(run.id)}


@pytest.mark.db
async def test_allocator_registers_reservation_active_precondition(
    db_session: AsyncSession, db_host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.runs import service_allocator

    captured: list[IntentRegistration] = []

    async def fake_register(
        _self: object,
        *,
        device_id: uuid.UUID,
        intents: list[IntentRegistration],
        reason: str,
    ) -> None:
        captured.extend(intents)

    monkeypatch.setattr(IntentService, "register_intents_and_reconcile", fake_register)

    device = await create_device(db_session, host_id=db_host.id, name="alloc-reg")
    run = await create_reserved_run(db_session, name="alloc-reg-run", devices=[device])
    await db_session.commit()

    await service_allocator._register_run_grid_intent(
        db_session,
        run=run,
        device_id=device.id,
    )

    run_routing = [intent for intent in captured if intent.source == f"run:{run.id}"]
    assert len(run_routing) == 1
    assert run_routing[0].precondition == {
        "kind": "reservation_active",
        "run_id": str(run.id),
        "device_id": str(device.id),
    }


@pytest.mark.db
async def test_maintenance_intents_carry_maintenance_active_precondition(
    db_session: AsyncSession, db_host: Host
) -> None:
    from app.devices.services.maintenance import _maintenance_intents

    device = await create_device(db_session, host_id=db_host.id, name="maint-prec")
    intents = _maintenance_intents(device.id)
    expected = {
        "kind": "maintenance_active",
        "device_id": str(device.id),
    }
    sources = {intent.source for intent in intents}
    assert sources == {
        f"maintenance:node:{device.id}",
        f"maintenance:grid:{device.id}",
        f"maintenance:recovery:{device.id}",
    }
    for intent in intents:
        assert intent.precondition == expected


@pytest.mark.db
async def test_node_health_registers_node_running_precondition(
    db_session: AsyncSession, db_host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: list[IntentRegistration] = []

    async def fake_register(
        _self: object,
        *,
        device_id: uuid.UUID,
        intents: list[IntentRegistration],
        reason: str,
    ) -> None:
        captured.extend(intents)

    monkeypatch.setattr(IntentService, "register_intents_and_reconcile", fake_register)

    from unittest.mock import Mock

    from app.appium_nodes.services.node_health import NodeHealthService

    device = await create_device(db_session, host_id=db_host.id, name="autorec-prec")
    await _seed_node(db_session, device.id)
    svc = NodeHealthService(
        publisher=Mock(),
        settings=FakeSettingsReader({}),
        pool=Mock(),
        circuit_breaker=Mock(),
        grid=Mock(),
        recovery_control=AsyncMock(),
        health=AsyncMock(),
    )
    await svc._attempt_node_restart(db_session, device=device)

    node_intent = next(intent for intent in captured if intent.source == f"auto_recovery:node:{device.id}")
    recovery_intent = next(intent for intent in captured if intent.source == f"auto_recovery:recovery:{device.id}")
    expected = {"kind": "node_running", "device_id": str(device.id), "expected": False}
    assert node_intent.precondition == expected
    assert recovery_intent.precondition == expected


@pytest.mark.db
async def test_sweep_deletes_operator_start_intent_when_node_observed_running(
    db_session: AsyncSession, db_host: Host
) -> None:
    """End-to-end: once a node reaches observed_running=True, the operator:start
    intent's node_running(expected=False) precondition flips to unsatisfied, and
    reconcile_unsatisfied_preconditions deletes the row. This is the PR #301
    sweep mechanism that retires stale restart intents in production.
    """
    device = await create_device(db_session, host_id=db_host.id, name="op-start-prec-sweep")
    with state_write_guard.bypass():
        node = AppiumNode(
            device_id=device.id,
            port=4723,
            grid_url="http://grid:4444",
            desired_state=AppiumDesiredState.running,
            pid=12345,
            active_connection_target="dev-1",
        )
    db_session.add(node)
    await db_session.commit()
    assert node.observed_running, "fixture must seed an observed-running node"

    await IntentService(db_session).register_intents(
        device_id=device.id,
        reason="operator start",
        intents=[
            IntentRegistration(
                source=f"operator:start:{device.id}",
                axis=NODE_PROCESS,
                payload={"action": "start", "priority": 20},
                precondition={"kind": "node_running", "device_id": str(device.id), "expected": False},
            )
        ],
    )
    await db_session.commit()

    await reconcile_unsatisfied_preconditions(db_session)
    await db_session.commit()

    rows = (await db_session.execute(select(DeviceIntent).where(DeviceIntent.device_id == device.id))).scalars().all()
    assert rows == [], (
        f"node_running(expected=False) precondition must be unsatisfied while observed_running=True; "
        f"sweep should delete the intent. Remaining: {[r.source for r in rows]}"
    )


@pytest.mark.db
async def test_verification_node_not_stopped_when_operator_start_intent_swept(
    db_session: AsyncSession, db_host: Host
) -> None:
    """Regression: during initial-device verification the operator:start intent
    is registered against an UNVERIFIED device (verified_at IS NULL). Once the
    node reaches observed_running=True the precondition sweep deletes the
    operator:start row; reconcile_device must NOT then derive
    ``desired_state=stopped`` and kill the verification node mid session-probe.

    The fix registers a standing ``verification:{device_id}`` node_process
    intent in ``run_probe`` before ``start_node``. It has no auto-retire
    precondition and outlives ``operator:start`` so that ``evaluate_node_process``
    still finds an active "start" intent after the sweep — keeping
    ``desired_state=running`` for the entire probe window.
    """
    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="verify-probe-race",
        verified=False,
    )
    assert device.verified_at is None, "fixture precondition: device must be unverified"

    with state_write_guard.bypass():
        node = AppiumNode(
            device_id=device.id,
            port=4723,
            grid_url="http://grid:4444",
            desired_state=AppiumDesiredState.running,
            desired_port=4723,
            pid=12345,
            active_connection_target="dev-1",
        )
    db_session.add(node)
    await db_session.commit()
    assert node.observed_running, "fixture must seed an observed-running verification node"

    # Reproduce the production intent layout that ``run_probe`` now creates:
    # the existing auto-retiring operator:start row AND the standing
    # verification:{device_id} guard.
    await IntentService(db_session).register_intents(
        device_id=device.id,
        reason="verification start",
        intents=[
            IntentRegistration(
                source=f"operator:start:{device.id}",
                axis=NODE_PROCESS,
                payload={"action": "start", "priority": 20, "desired_port": 4723},
                precondition={"kind": "node_running", "device_id": str(device.id), "expected": False},
            ),
            IntentRegistration(
                source=f"verification:{device.id}",
                axis=NODE_PROCESS,
                payload={"action": "start", "priority": 20},
            ),
        ],
    )
    await db_session.commit()

    # Simulate one device_intent_reconciler tick mid-probe.
    await reconcile_unsatisfied_preconditions(db_session)
    await db_session.commit()
    await reconcile_device(db_session, device.id)
    await db_session.commit()

    intents_remaining = (
        (await db_session.execute(select(DeviceIntent).where(DeviceIntent.device_id == device.id))).scalars().all()
    )
    sources = {intent.source for intent in intents_remaining}
    assert f"operator:start:{device.id}" not in sources, (
        "operator:start precondition must still be swept on observed_running"
    )
    assert f"verification:{device.id}" in sources, "verification:{device_id} standing intent must survive the sweep"

    refreshed_node = (
        await db_session.execute(select(AppiumNode).where(AppiumNode.device_id == device.id))
    ).scalar_one()
    assert refreshed_node.desired_state == AppiumDesiredState.running, (
        "Verification node must stay desired=running while probe is in flight; "
        f"got desired_state={refreshed_node.desired_state!r}. This is the race that "
        "kills slow-probe verifications."
    )
