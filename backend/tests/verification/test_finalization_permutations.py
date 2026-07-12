"""Order-independence acceptance tests for verification finalization (WS-15.3)."""

from __future__ import annotations

import itertools
from datetime import timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import select

from app.appium_nodes.models import AppiumDesiredState, AppiumNode
from app.core.timeutil import now_utc
from app.devices import locking as device_locking
from app.devices.models import DeviceIntent, DeviceOperationalState
from app.devices.services.intent import IntentService
from app.devices.services.intent_reconciler import reconcile_device
from app.devices.services.intent_types import (
    VERIFICATION_OUTCOME_FAILED,
    CommandKind,
    IntentRegistration,
    verification_intent_source,
)
from app.devices.services.state import derive_operational_state
from app.lifecycle.services import remediation_log
from app.lifecycle.services.operator_node import operator_start_source, operator_stop_intents, operator_stop_sources
from app.verification.services.execution import (
    AgentCallContext,
    VerificationExecutionService,
    _register_verification_node_intent,
    _stamp_verification_outcome,
)
from app.verification.services.job_state import new_job
from tests.fakes import FakeSettingsReader, build_review_service
from tests.helpers import create_device, settle_after_commit_tasks
from tests.helpers import test_event_bus as event_bus

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.devices.models import Device
    from app.hosts.models import Host

pytestmark = [pytest.mark.db, pytest.mark.usefixtures("seeded_driver_packs")]


async def _seed_node(db: AsyncSession, device_id: object, *, running: bool = False) -> AppiumNode:
    node = AppiumNode(device_id=device_id, port=4723, desired_state=AppiumDesiredState.stopped)
    if running:
        node.pid = 1234
        node.active_connection_target = "connected"
    db.add(node)
    await db.commit()
    return node


class _ViabilityStub:
    async def record_session_viability_result(
        self,
        db: AsyncSession,
        device: Device,
        *,
        status: str,
        checked_by: object,
    ) -> dict[str, Any]:
        del checked_by
        device.session_viability_status = status
        await db.flush()
        return {"status": status}


def _build_execution_service() -> VerificationExecutionService:
    return VerificationExecutionService(
        review=build_review_service(),
        publisher=event_bus,
        agent=AgentCallContext(settings=FakeSettingsReader({}), circuit_breaker=Mock()),
        crud=AsyncMock(),
        viability=_ViabilityStub(),
        capability=AsyncMock(),
        reconciler=AsyncMock(),
        node_manager=AsyncMock(),
    )


def _job() -> dict[str, Any]:
    return new_job("ws153-test-job")


async def test_failure_finalization_statements_permute(db_session: AsyncSession, db_host: Host) -> None:
    """WS-15.3 acceptance: the failure-path intent mutations are order-independent.

    After the durable-facts block (review_required + outcome stamp), every ordering
    of the intent mutations, with an adversarial reconcile after each statement,
    derives the same final state. The stamp makes an unrevoked lease terminal for
    both claim and command readers.
    """
    finals: set[tuple[object, ...]] = set()
    step_names = ("stop_intents", "revoke_lease", "revoke_stops", "revoke_start")
    for index, perm in enumerate(itertools.permutations(step_names)):
        device = await create_device(db_session, host_id=db_host.id, name=f"ws153-perm-{index}")
        node = await _seed_node(db_session, device.id, running=True)
        await _register_verification_node_intent(
            db_session, device, settings=FakeSettingsReader({}), publisher=event_bus
        )
        await IntentService(db_session).register_intents(
            device_id=device.id,
            intents=[
                IntentRegistration(
                    source=operator_start_source(device.id),
                    kind=CommandKind.operator_start,
                    payload={"action": "start"},
                    expires_at=now_utc() + timedelta(minutes=5),
                )
            ],
        )
        await db_session.commit()

        locked = await device_locking.lock_device(db_session, device.id)
        locked.review_required = True
        locked.review_reason = "verification failed: probe failed"
        await db_session.flush()
        await _stamp_verification_outcome(db_session, locked, outcome=VERIFICATION_OUTCOME_FAILED)

        intent_service = IntentService(db_session)
        steps: dict[str, Callable[[], Awaitable[None]]] = {
            "stop_intents": lambda service=intent_service, device_id=device.id: service.register_intents_and_reconcile(
                device_id=device_id,
                intents=operator_stop_intents(device_id),
                publisher=event_bus,
            ),
            "revoke_lease": lambda service=intent_service, device_id=device.id: service.revoke_intents_and_reconcile(
                device_id=device_id,
                sources=[verification_intent_source(device_id)],
                publisher=event_bus,
            ),
            "revoke_stops": lambda service=intent_service, device_id=device.id: service.revoke_intents_and_reconcile(
                device_id=device_id,
                sources=operator_stop_sources(device_id),
                publisher=event_bus,
            ),
            "revoke_start": lambda service=intent_service, device_id=device.id: service.revoke_intents_and_reconcile(
                device_id=device_id,
                sources=[operator_start_source(device_id)],
                publisher=event_bus,
            ),
        }
        states: list[DeviceOperationalState] = []
        for name in perm:
            await steps[name]()
            await reconcile_device(db_session, device.id, publisher=event_bus)
            states.append(await derive_operational_state(db_session, device, now=now_utc()))
        await db_session.commit()
        await db_session.refresh(node)
        await db_session.refresh(device)

        assert all(state == states[-1] for state in states), f"projection flapped under {perm}: {states}"
        finals.add(
            (
                states[-1],
                node.desired_state,
                node.stop_pending,
                node.accepting_new_sessions,
                device.operational_state_last_emitted,
                device.review_required,
            )
        )
    assert len(finals) == 1, f"orderings diverged: {finals}"


async def test_finalize_success_single_edge_no_flap(
    db_session: AsyncSession,
    db_host: Host,
    event_bus_capture: list[tuple[str, dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """WS-15.3: production success finalization emits one verifying→available edge."""
    device = await create_device(db_session, host_id=db_host.id, name="ws153-success")
    node = await _seed_node(db_session, device.id, running=True)
    device.verified_at = None
    device.session_viability_status = "failed"
    await remediation_log.append_action(
        db_session,
        device.id,
        source="device_checks",
        action=remediation_log.ACTION_AUTO_STOP_COMMISSIONED,
        reason="episode in flight",
    )
    await _register_verification_node_intent(db_session, device, settings=FakeSettingsReader({}), publisher=event_bus)
    await db_session.commit()
    event_bus_capture.clear()

    monkeypatch.setattr("app.verification.services.execution.set_stage", AsyncMock())
    svc = _build_execution_service()
    context = SimpleNamespace(mode="create", save_device_id=device.id, transient_device=device, save_payload={})
    outcome = await svc._finalize_success(db_session, context, job=_job(), node=node)
    assert outcome.status == "completed"
    await settle_after_commit_tasks()

    edges = [
        (payload["old_operational_state"], payload["new_operational_state"])
        for name, payload in event_bus_capture
        if name == "device.operational_state_changed"
    ]
    assert edges == [("verifying", "available")], f"expected one clean edge, got {edges}"
    assert device.verified_at is not None
    ladder = await remediation_log.load_ladder(db_session, device.id)
    assert ladder.node_directive is None


async def test_finalize_failure_single_edge_no_flap(
    db_session: AsyncSession,
    db_host: Host,
    event_bus_capture: list[tuple[str, dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """WS-15.3: production failure finalization emits one verifying→offline edge."""
    device = await create_device(db_session, host_id=db_host.id, name="ws153-failure")
    node = await _seed_node(db_session, device.id, running=True)
    await _register_verification_node_intent(db_session, device, settings=FakeSettingsReader({}), publisher=event_bus)
    await IntentService(db_session).register_intents(
        device_id=device.id,
        intents=[
            IntentRegistration(
                source=operator_start_source(device.id),
                kind=CommandKind.operator_start,
                payload={"action": "start"},
                expires_at=now_utc() + timedelta(minutes=5),
            )
        ],
    )
    await db_session.commit()
    event_bus_capture.clear()

    monkeypatch.setattr("app.verification.services.execution.set_stage", AsyncMock())
    svc = _build_execution_service()
    context = SimpleNamespace(mode="update", save_device_id=device.id, transient_device=device, save_payload={})
    outcome = await svc._finalize_failure(
        db_session,
        context,
        error="probe failed",
        job=_job(),
        node=node,
        original_fields={"name": device.name},
    )
    assert outcome.status == "failed"
    await settle_after_commit_tasks()

    edges = [
        (payload["old_operational_state"], payload["new_operational_state"])
        for name, payload in event_bus_capture
        if name == "device.operational_state_changed"
    ]
    assert edges == [("verifying", "offline")], f"expected one clean edge, got {edges}"
    assert device.review_required is True
    remaining = (
        (await db_session.execute(select(DeviceIntent).where(DeviceIntent.device_id == device.id))).scalars().all()
    )
    assert remaining == []
