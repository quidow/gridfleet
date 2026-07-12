"""Successor-semantics acceptance tests for the WS-15.2 pseudo-command demotion.

The three system pseudo-commands (``health_failure:node``, ``auto_recovery:node``,
``auto_recovery:recovery``) and the stored ``deferred_stop`` latch are gone; their
decisions now derive from the remediation-log node-process directive plus current
facts. Each test drives public services and pins one acceptance line from the spec
(``.superpowers/specs/2026-07-12-simplification-tranche-5.md`` §WS-15.2).
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import select

from app.appium_nodes.models import AppiumDesiredState, AppiumNode
from app.core.timeutil import now_utc
from app.devices.models import DeviceIntent, DeviceOperationalState
from app.devices.services.intent_reconciler import reconcile_device
from app.devices.services.intent_types import CommandKind
from app.devices.services.recovery_projection import RecoveryBlockKind, recovery_availability
from app.lifecycle.services import policy as lifecycle_policy_module
from app.lifecycle.services import remediation_log
from app.lifecycle.services.actions import LifecyclePolicyActionsService
from app.lifecycle.services.incidents import LifecycleIncidentService
from app.lifecycle.services.operator_node import OperatorNodeLifecycleService
from app.lifecycle.services.policy import LifecyclePolicyService
from app.lifecycle.services.remediation_log import DIRECTIVE_STOP
from app.runs.service_reservation import RunReservationService
from app.verification.services.execution import (
    _register_verification_node_intent,
    _revoke_verification_node_intent,
)
from tests.fakes import FakeSettingsReader, build_review_service
from tests.helpers import create_device
from tests.helpers import test_event_bus as event_bus

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.hosts.models import Host

pytestmark = [pytest.mark.db, pytest.mark.usefixtures("seeded_driver_packs")]


def _policy_service(*, viability: object | None = None) -> LifecyclePolicyService:
    return LifecyclePolicyService(
        review=build_review_service(),
        publisher=event_bus,
        settings=FakeSettingsReader({}),
        actions=LifecyclePolicyActionsService(
            publisher=event_bus,
            reservation=RunReservationService(review=build_review_service()),
            incidents=LifecycleIncidentService(),
        ),
        incidents=LifecycleIncidentService(),
        viability=viability if viability is not None else Mock(),
        node_manager=AsyncMock(),
    )


async def _seed_node(db_session: AsyncSession, device_id: object, *, running: bool = False) -> AppiumNode:
    node = AppiumNode(device_id=device_id, port=4723, desired_state=AppiumDesiredState.stopped)
    if running:
        node.pid = 1234
        node.active_connection_target = "connected"
    db_session.add(node)
    await db_session.commit()
    return node


async def test_verification_lease_outranks_derived_stop_without_revoke(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    """Acceptance: a mid-episode verification lease outranks the derived failure-stop
    with NO revoke — the retired revoke-before-register ritual is gone. Success
    appends a ``verification_passed`` reset that ends the episode, so baseline holds
    the verified, in-service device running with no stop flap (spec WS-15.2)."""
    device = await create_device(db_session, host_id=db_host.id, name="verify-outranks")
    node = await _seed_node(db_session, device.id, running=True)

    assert (
        await _policy_service().handle_health_failure(
            db_session, device, source="device_checks", reason="ADB not responsive"
        )
        == "stopped"
    )
    ladder = await remediation_log.load_ladder(db_session, device.id)
    assert ladder.node_directive is not None
    assert ladder.node_directive.kind == DIRECTIVE_STOP
    await db_session.refresh(node)
    assert node.desired_state == AppiumDesiredState.stopped

    # Register the verification lease (a start command) and reconcile. No revoke.
    await _register_verification_node_intent(db_session, device, settings=FakeSettingsReader({}), publisher=event_bus)
    await db_session.commit()
    await db_session.refresh(node)
    assert node.desired_state == AppiumDesiredState.running
    # The commission row is untouched — the lease suppressed the stop structurally,
    # it did not revoke anything.
    ladder = await remediation_log.load_ladder(db_session, device.id)
    assert ladder.node_directive is not None
    assert ladder.node_directive.kind == DIRECTIVE_STOP

    # Mirror _finalize_success ordering: append the verification_passed reset, then
    # revoke the lease + reconcile. The reset ends the episode; baseline sustains the
    # verified, in-service device running — no stop flap.
    await remediation_log.append_reset(db_session, device.id, source="verification", action="verification_passed")
    await _revoke_verification_node_intent(db_session, device, publisher=event_bus)
    await db_session.commit()
    await db_session.refresh(node)
    assert node.desired_state == AppiumDesiredState.running
    ladder = await remediation_log.load_ladder(db_session, device.id)
    assert ladder.node_directive is None


async def test_restart_commission_watermark_is_do_once_per_episode(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    """Acceptance: a ``restart_commissioned`` row's timestamp is the restart
    watermark; a satisfied watermark is inert (do-once), so only a fresh commission
    moves it — no TTL, no respawn storm (spec WS-15.2)."""
    device = await create_device(db_session, host_id=db_host.id, name="restart-once")
    node = await _seed_node(db_session, device.id)

    first = await remediation_log.append_action(
        db_session,
        device.id,
        source="node_health",
        action=remediation_log.ACTION_RESTART_COMMISSIONED,
        reason="Node health restart",
    )
    await db_session.commit()
    await reconcile_device(db_session, device.id, publisher=event_bus)
    await db_session.commit()
    await db_session.refresh(node)
    assert node.desired_state == AppiumDesiredState.running
    assert node.restart_requested_at == first.at

    # A reconcile with no new commission leaves the watermark unchanged (do-once).
    await reconcile_device(db_session, device.id, publisher=event_bus)
    await db_session.commit()
    await db_session.refresh(node)
    assert node.restart_requested_at == first.at

    # A later attempt + fresh commission moves the watermark forward.
    await remediation_log.append_attempt(
        db_session, device.id, source="node_health", reason="still down", settings=FakeSettingsReader({})
    )
    second = await remediation_log.append_action(
        db_session,
        device.id,
        source="node_health",
        action=remediation_log.ACTION_RESTART_COMMISSIONED,
        reason="Node health restart",
    )
    await db_session.commit()
    await reconcile_device(db_session, device.id, publisher=event_bus)
    await db_session.commit()
    await db_session.refresh(node)
    assert second.at > first.at
    assert node.restart_requested_at == second.at


async def test_failed_recovery_restops_and_backs_off(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Acceptance: a failed recovery re-stops the node (STOP directive) and arms
    backoff; ``recovery_availability`` blocks on the backoff and no stored intent
    rows of any retired kind are ever written (spec WS-15.2)."""
    monkeypatch.setattr(lifecycle_policy_module, "RECOVERY_PROBE_ATTEMPTS", 1, raising=False)
    monkeypatch.setattr(lifecycle_policy_module, "RECOVERY_PROBE_RETRY_DELAY_SEC", 0, raising=False)
    monkeypatch.setattr(lifecycle_policy_module, "RECOVERY_PROBE_JITTER_MAX_SEC", 0, raising=False)
    monkeypatch.setattr(lifecycle_policy_module, "RECOVERY_NODE_START_WAIT_TIMEOUT_SEC", 0, raising=False)

    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="failed-recovery",
        operational_state=DeviceOperationalState.offline,
    )
    await _seed_node(db_session, device.id)
    await db_session.commit()

    viability = AsyncMock()
    viability.run_session_viability_probe = AsyncMock(return_value={"status": "failed", "error": "probe failed"})

    recovered = await _policy_service(viability=viability).attempt_auto_recovery(
        db_session, device, source="device_connectivity", reason="Node went offline"
    )
    assert recovered is False

    ladder = await remediation_log.load_ladder(db_session, device.id)
    assert ladder.node_directive is not None
    assert ladder.node_directive.kind == DIRECTIVE_STOP
    assert ladder.backoff_active(now=now_utc()) is not None

    blocked = await recovery_availability(db_session, device, ready=True)
    assert blocked.allowed is False
    assert blocked.kind is RecoveryBlockKind.backoff

    intents = (await db_session.execute(select(DeviceIntent))).scalars().all()
    retired = {"health_failure:node", "auto_recovery:node", "auto_recovery:recovery"}
    assert all(row.kind not in retired for row in intents)


def test_command_kind_set_is_external_will_only() -> None:
    """Acceptance tripwire: the three system pseudo-commands are demoted to
    log-derived rungs, so ``CommandKind`` must carry external will only. Guards
    against silent re-introduction (spec WS-15.2)."""
    assert {kind.value for kind in CommandKind} == {
        "operator:stop:node",
        "operator:stop:recovery",
        "forced_release",
        "operator:start",
        "verification",
    }


async def test_operator_start_and_restart_supersede_stop_directive(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    """Acceptance: operator start and restart both supersede a live STOP directive —
    the start command suppresses the derived stop structurally and the appended reset
    ends the episode (semantic deltas (a)/(b), spec WS-15.2)."""
    # --- operator start ---
    started = await create_device(db_session, host_id=db_host.id, name="operator-start")
    start_node = await _seed_node(db_session, started.id, running=True)
    await db_session.refresh(started, ["appium_node"])
    await remediation_log.append_action(
        db_session,
        started.id,
        source="device_checks",
        action=remediation_log.ACTION_AUTO_STOP_COMMISSIONED,
        reason="node crashed",
    )
    await reconcile_device(db_session, started.id, publisher=event_bus)
    await db_session.commit()
    await db_session.refresh(start_node)
    assert start_node.desired_state == AppiumDesiredState.stopped

    operator = OperatorNodeLifecycleService(
        settings=FakeSettingsReader({}), publisher=event_bus, review=build_review_service()
    )
    await operator.request_start(db_session, started, caller="operator_route", reason="operator start")
    await db_session.commit()
    await db_session.refresh(start_node)
    assert start_node.desired_state == AppiumDesiredState.running
    ladder = await remediation_log.load_ladder(db_session, started.id)
    assert ladder.node_directive is None
    assert ladder.last_action == "operator_started"

    # --- operator restart ---
    restarted = await create_device(db_session, host_id=db_host.id, name="operator-restart")
    restart_node = await _seed_node(db_session, restarted.id, running=True)
    await db_session.refresh(restarted, ["appium_node"])
    await remediation_log.append_action(
        db_session,
        restarted.id,
        source="device_checks",
        action=remediation_log.ACTION_AUTO_STOP_COMMISSIONED,
        reason="node crashed",
    )
    await reconcile_device(db_session, restarted.id, publisher=event_bus)
    await db_session.commit()
    await db_session.refresh(restart_node)
    assert restart_node.desired_state == AppiumDesiredState.stopped

    await operator.request_restart(db_session, restarted, caller="operator_restart", reason="operator restart")
    await db_session.commit()
    await db_session.refresh(restart_node)
    assert restart_node.desired_state == AppiumDesiredState.running
    ladder = await remediation_log.load_ladder(db_session, restarted.id)
    assert ladder.node_directive is None
    assert ladder.last_action == "operator_restarted"
