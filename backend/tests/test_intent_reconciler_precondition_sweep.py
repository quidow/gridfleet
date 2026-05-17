from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import select

from app.appium_nodes.models import AppiumDesiredState, AppiumNode
from app.devices.models import DeviceIntent
from app.devices.services.intent import IntentService
from app.devices.services.intent_preconditions import reconcile_unsatisfied_preconditions
from app.devices.services.intent_types import NODE_PROCESS, IntentRegistration
from app.runs.models import RunState
from tests.helpers import create_device, create_reserved_run

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.hosts.models import Host


async def _seed_node(db_session: AsyncSession, device_id: uuid.UUID) -> AppiumNode:
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

    await _reconcile_expired_intents(db_session)
    await db_session.commit()

    remaining = (
        (await db_session.execute(select(DeviceIntent).where(DeviceIntent.device_id == device.id))).scalars().all()
    )
    assert remaining == []


@pytest.mark.db
async def test_sweep_reconciles_affected_device_only(
    db_session: AsyncSession, db_host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.devices.services import intent_preconditions as module

    reconciled: list[uuid.UUID] = []

    async def fake_reconcile(db: AsyncSession, device_id: uuid.UUID) -> None:
        reconciled.append(device_id)

    async def fake_deliver(db: AsyncSession, device_id: uuid.UUID, *, limit: int = 5) -> None:
        return None

    monkeypatch.setattr(module, "_reconcile_device", fake_reconcile)
    monkeypatch.setattr(module, "deliver_agent_reconfigures", fake_deliver)

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

    await reconcile_unsatisfied_preconditions(db_session)
    await db_session.commit()
    assert reconciled == [device_a.id]


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
    from app.runs import service_lifecycle_release
    from app.runs.models import TestRun

    captured: list[IntentRegistration] = []

    async def fake_register(
        db: AsyncSession,
        *,
        device_id: uuid.UUID,
        intents: list[IntentRegistration],
        reason: str,
    ) -> None:
        captured.extend(intents)

    async def fake_revoke(
        db: AsyncSession,
        *,
        device_id: uuid.UUID,
        sources: list[str],
        reason: str,
    ) -> None:
        return None

    monkeypatch.setattr(service_lifecycle_release, "register_intents_and_reconcile", fake_register)
    monkeypatch.setattr(service_lifecycle_release, "revoke_intents_and_reconcile", fake_revoke)

    device = await create_device(db_session, host_id=db_host.id, name="forced-reg")
    run = await create_reserved_run(db_session, name="forced-reg-run", devices=[device])
    refreshed_run = await db_session.get(TestRun, run.id)
    assert refreshed_run is not None

    await service_lifecycle_release._clear_desired_grid_run_id_for_run(
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
        db: AsyncSession,
        *,
        device_id: uuid.UUID,
        intents: list[IntentRegistration],
        reason: str,
    ) -> None:
        captured.extend(intents)

    monkeypatch.setattr(service_allocator, "register_intents_and_reconcile", fake_register)

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
async def test_maintenance_intents_carry_device_hold_precondition(db_session: AsyncSession, db_host: Host) -> None:
    from app.devices.services.maintenance import _maintenance_intents

    device = await create_device(db_session, host_id=db_host.id, name="maint-prec")
    intents = _maintenance_intents(device.id)
    expected = {
        "kind": "device_hold",
        "device_id": str(device.id),
        "hold": "maintenance",
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
    from app.appium_nodes.services import node_health

    captured: list[IntentRegistration] = []

    async def fake_register(
        db: AsyncSession,
        *,
        device_id: uuid.UUID,
        intents: list[IntentRegistration],
        reason: str,
    ) -> None:
        captured.extend(intents)

    monkeypatch.setattr(node_health, "register_intents_and_reconcile", fake_register)

    device = await create_device(db_session, host_id=db_host.id, name="autorec-prec")
    await _seed_node(db_session, device.id)
    await node_health._attempt_node_restart(db_session, device=device)

    node_intent = next(intent for intent in captured if intent.source == f"auto_recovery:node:{device.id}")
    recovery_intent = next(intent for intent in captured if intent.source == f"auto_recovery:recovery:{device.id}")
    expected = {"kind": "node_running", "device_id": str(device.id), "expected": False}
    assert node_intent.precondition == expected
    assert recovery_intent.precondition == expected
