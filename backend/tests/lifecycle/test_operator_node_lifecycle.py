"""Regression tests for unified operator-driven Appium node lifecycle writes."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import select

from app.appium_nodes.exceptions import NodeManagerError
from app.appium_nodes.models import AppiumDesiredState, AppiumNode, AppiumNodeResourceClaim
from app.appium_nodes.services import resource_service
from app.devices.models import DeviceIntent
from app.devices.services.intent_reconciler import _gc_expired_intents, reconcile_device
from app.lifecycle.services.operator_node import OperatorNodeLifecycleService, operator_stop_active
from tests.fakes import FakeSettingsReader, build_review_service
from tests.helpers import create_device
from tests.helpers import test_event_bus as event_bus

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.devices.models import Device
    from app.hosts.models import Host

pytestmark = pytest.mark.usefixtures("seeded_driver_packs")


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


class _FakeDevice:
    """Minimal device stub — only ``id`` is required by the helpers under test."""

    def __init__(self, device_id: uuid.UUID) -> None:
        self.id = device_id


class _FakeSettings:
    """Stub for ``settings_service`` — returns hard-coded values for known keys."""

    def get(self, key: str) -> object:
        if key == "appium_reconciler.restart_window_sec":
            return 120
        raise KeyError(key)


async def test_stale_operator_start_intent_does_not_force_old_desired_port(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    """A stale operator:start payload port must never reach AppiumNode.desired_port.

    Repro for the Roku flip observed on 2026-05-18 (pre-PR-#301 row shape) and the
    FireTV 4724<->4725 churn storm of 2026-06-07 (current-shape intent gone stale
    after a fallback start moved the node): the applier pins the live node.port, so
    the snapshot in the payload is audit-only and the flip cannot recur — with NO
    operator action required.
    """
    device = await create_device(db_session, host_id=db_host.id, name="roku-flip-repro", verified=True)
    node = AppiumNode(
        device_id=device.id,
        port=4725,
        desired_state=AppiumDesiredState.running,
        desired_port=4725,
        pid=27765,
        active_connection_target=device.connection_target,
    )
    db_session.add(node)
    await db_session.flush()
    device.appium_node = node

    stale_requested_at = datetime.now(UTC) - timedelta(days=2)
    stale_intent = DeviceIntent(
        device_id=device.id,
        source=f"operator:start:{device.id}",
        kind="operator:start",
        payload={
            "action": "start",
            "priority": 20,
            "desired_port": 4724,
            "restart_requested_at": stale_requested_at.isoformat(),
        },
        expires_at=None,
        created_at=stale_requested_at - timedelta(minutes=2),
        updated_at=stale_requested_at - timedelta(minutes=2),
    )
    db_session.add(stale_intent)
    await db_session.commit()

    # Run reconcile WITHOUT any operator action — the applier pins the live
    # node.port, so the stale payload's 4724 must never be written even though the
    # precondition/expires_at sweeps both skip this row (NULL columns, pre-#301
    # shape). Before the 2026-06-07 applier fix this reconcile mis-asserted 4724.
    await reconcile_device(db_session, device.id, publisher=event_bus)
    await db_session.refresh(node)
    assert node.desired_port == 4725, (
        f"the stale payload port must be ignored in favor of live node.port; got {node.desired_port}"
    )

    # An operator Restart through the unified path still refreshes the intent row
    # (fresh restart_requested_at + expires_at) and
    # keeps desired_port on the live port.
    await OperatorNodeLifecycleService(
        review=build_review_service(), settings=FakeSettingsReader({}), publisher=event_bus
    ).request_restart(db_session, device, caller="operator_restart", reason="operator restart")
    await db_session.refresh(node)

    assert node.desired_port == 4725, (
        f"after a unified-path restart, desired_port should match the running port; got {node.desired_port}"
    )

    intent = (
        await db_session.execute(
            select(DeviceIntent).where(
                DeviceIntent.device_id == device.id,
                DeviceIntent.source == f"operator:start:{device.id}",
            )
        )
    ).scalar_one()
    assert intent.payload.get("restart_requested_at") != stale_requested_at.isoformat(), (
        "stale restart_requested_at must be replaced by the fresh restart"
    )
    assert intent.expires_at is not None, "fresh restart must set expires_at"
    assert intent.expires_at > datetime.now(UTC), "fresh expires_at must be in the future"


def test_operator_restart_intent_sets_expires_at(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """operator_restart_intent must set a watermark and TTL bounded by window_sec."""
    from app.lifecycle.services import operator_node as mod
    from app.lifecycle.services.operator_node import operator_restart_intent

    fixed_now = datetime(2026, 5, 19, 12, 0, 0, tzinfo=UTC)
    monkeypatch.setattr(mod, "now_utc", lambda: fixed_now)

    device_id = uuid.uuid4()
    device = _FakeDevice(device_id)

    intent = operator_restart_intent(device, settings=FakeSettingsReader({"appium_reconciler.restart_window_sec": 120}))  # type: ignore[arg-type]

    expected_deadline = fixed_now + timedelta(seconds=120)

    assert intent.expires_at is not None
    assert intent.expires_at == expected_deadline
    assert intent.payload["restart_requested_at"] == fixed_now.isoformat()


async def test_gc_expired_intents_deletes_expired_restart_intent(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    """_gc_expired_intents must delete DeviceIntent rows whose expires_at
    has passed, even when expires_at is explicitly set (as opposed to the Task 1
    regression where expires_at was NULL).
    """
    device = await create_device(db_session, host_id=db_host.id, name="gc-expired-restart", verified=True)

    expired_intent = DeviceIntent(
        device_id=device.id,
        source=f"operator:start:{device.id}",
        kind="operator:start",
        payload={
            "action": "start",
            "priority": 20,
            "desired_port": 4725,
            "restart_requested_at": (datetime.now(UTC) - timedelta(minutes=5)).isoformat(),
        },
        expires_at=datetime.now(UTC) - timedelta(minutes=5),
        created_at=datetime.now(UTC) - timedelta(minutes=10),
        updated_at=datetime.now(UTC) - timedelta(minutes=10),
    )
    db_session.add(expired_intent)
    await db_session.commit()

    await _gc_expired_intents(db_session)

    remaining = (
        (await db_session.execute(select(DeviceIntent).where(DeviceIntent.device_id == device.id))).scalars().all()
    )
    assert remaining == [], (
        f"expected no intents after GC sweep, found {len(remaining)}: {[r.source for r in remaining]}"
    )


async def test_two_consecutive_request_restarts_refresh_intent_payload(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    """Each operator restart must produce a fresh watermark + expires_at.

    Pre-PR-#301, a stale operator:start intent payload could re-assert old
    restart_requested_at/desired_port indefinitely. The unified path overwrites the
    full payload on every restart.
    """
    device = await create_device(db_session, host_id=db_host.id, name="rr-refresh", verified=True)
    node = AppiumNode(
        device_id=device.id,
        port=4725,
        desired_state=AppiumDesiredState.running,
        desired_port=4725,
        pid=27765,
        active_connection_target=device.connection_target,
    )
    db_session.add(node)
    await db_session.flush()
    device.appium_node = node
    # observed_running on AppiumNode is a hybrid/derived flag; verify the
    # fixture is constructed so the model treats the node as running.
    assert node.observed_running, "test fixture must seed an observed-running node"

    svc = OperatorNodeLifecycleService(
        review=build_review_service(), settings=FakeSettingsReader({}), publisher=event_bus
    )
    await svc.request_restart(db_session, device, caller="operator_restart", reason="first")
    intent_first = (
        await db_session.execute(
            select(DeviceIntent).where(
                DeviceIntent.device_id == device.id,
                DeviceIntent.source == f"operator:start:{device.id}",
            )
        )
    ).scalar_one()
    first_watermark = intent_first.payload["restart_requested_at"]
    first_deadline = intent_first.expires_at

    await svc.request_restart(db_session, device, caller="operator_restart", reason="second")
    # Use populate_existing so the query bypasses the SQLAlchemy identity-map
    # cache and reloads the upserted payload from the DB.
    intent_second = (
        await db_session.execute(
            select(DeviceIntent)
            .where(
                DeviceIntent.device_id == device.id,
                DeviceIntent.source == f"operator:start:{device.id}",
            )
            .execution_options(populate_existing=True)
        )
    ).scalar_one()

    assert intent_second.payload["restart_requested_at"] != first_watermark, "watermark must refresh on each restart"
    assert intent_second.expires_at is not None
    assert first_deadline is not None
    assert intent_second.expires_at > first_deadline, "expires_at must move forward on each restart"


def test_operator_stop_intents_and_sources_include_recovery_deny() -> None:
    """Operator stop must register an operator_recovery_deny command (so ``recovery_allowed``
    flips False and auto-recovery suppresses instead of spinning a doomed start —
    N13), and ``operator_stop_sources`` must list that source so an operator start
    revokes it.
    """
    from app.devices.services.intent_types import CommandKind
    from app.lifecycle.services.operator_node import operator_stop_intents, operator_stop_sources

    device_id = uuid.uuid4()
    recovery_intents = [
        intent for intent in operator_stop_intents(device_id) if intent.kind is CommandKind.operator_recovery_deny
    ]
    assert len(recovery_intents) == 1, "operator stop must register exactly one recovery-deny intent"
    deny = recovery_intents[0]
    assert deny.payload == {"allowed": False, "reason": "Operator stopped the node"}
    assert deny.source in operator_stop_sources(device_id), (
        "the recovery-deny source must be revocable by the operator-start path"
    )


async def test_operator_stop_denies_recovery_and_operator_start_restores_it(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    """Operator ``node/stop`` is sticky and must deny auto-recovery: the recovery
    availability projection reports blocked (operator kind) so
    ``attempt_auto_recovery`` stands down instead of registering a prio-20 start it
    can never make win (N13). An explicit operator start lifts the deny.
    """
    from app.devices.services.recovery_projection import RecoveryBlockKind, recovery_availability

    device = await create_device(db_session, host_id=db_host.id, name="op-stop-denies-recovery", verified=True)
    node = AppiumNode(
        device_id=device.id,
        port=4725,
        desired_state=AppiumDesiredState.running,
        desired_port=4725,
        pid=27765,
        active_connection_target=device.connection_target,
    )
    db_session.add(node)
    await db_session.flush()
    device.appium_node = node
    assert (await recovery_availability(db_session, device)).allowed is True, (
        "baseline: a running device allows recovery"
    )

    svc = OperatorNodeLifecycleService(
        review=build_review_service(), settings=FakeSettingsReader({}), publisher=event_bus
    )
    await svc.request_stop(db_session, device, reason="operator stop")
    await db_session.commit()
    await db_session.refresh(device)
    denied = await recovery_availability(db_session, device)
    assert denied.allowed is False, "operator stop must deny auto-recovery (sticky stop)"
    assert denied.kind is RecoveryBlockKind.operator

    await svc.request_start(db_session, device, caller="operator_route", reason="operator start")
    await db_session.commit()
    await db_session.refresh(device)
    assert (await recovery_availability(db_session, device)).allowed is True, "operator start must re-allow recovery"


async def test_operator_stop_active_tracks_sticky_stop(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    """``operator_stop_active`` is the gate a re-verify checks to avoid silently
    reviving an operator-stopped device (N13b): True only while the sticky stop holds,
    and lifted by an operator start."""
    device = await create_device(db_session, host_id=db_host.id, name="op-stop-active", verified=True)
    node = AppiumNode(
        device_id=device.id,
        port=4726,
        desired_state=AppiumDesiredState.running,
        desired_port=4726,
        pid=27800,
        active_connection_target=device.connection_target,
    )
    db_session.add(node)
    await db_session.flush()
    device.appium_node = node
    assert await operator_stop_active(db_session, device.id) is False, "baseline: no operator stop"

    svc = OperatorNodeLifecycleService(
        review=build_review_service(), settings=FakeSettingsReader({}), publisher=event_bus
    )
    await svc.request_stop(db_session, device, reason="operator stop")
    await db_session.commit()
    assert await operator_stop_active(db_session, device.id) is True, "operator stop is active"

    await svc.request_start(db_session, device, caller="operator_route", reason="operator start")
    await db_session.commit()
    assert await operator_stop_active(db_session, device.id) is False, "operator start lifts the stop"


async def test_operator_start_supersedes_blocking_stop_directive(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    """An explicit operator start supersedes the derived stop episode."""
    from app.lifecycle.services import remediation_log

    device = await create_device(db_session, host_id=db_host.id, name="op-start-unblock", verified=True)
    node = AppiumNode(
        device_id=device.id,
        port=4725,
        desired_state=AppiumDesiredState.stopped,
    )
    db_session.add(node)
    await db_session.flush()
    device.appium_node = node

    await remediation_log.append_action(
        db_session,
        device.id,
        source="health_check_fail",
        action=remediation_log.ACTION_AUTO_STOP_COMMISSIONED,
        reason="stale stop",
    )
    await db_session.commit()

    svc = OperatorNodeLifecycleService(
        review=build_review_service(), settings=FakeSettingsReader({}), publisher=event_bus
    )
    await svc.request_start(db_session, device, caller="operator_route", reason="operator start")
    await db_session.commit()

    ladder = await remediation_log.load_ladder(db_session, device.id)
    assert ladder.node_directive is None
    assert ladder.last_action == "operator_started"
    await db_session.refresh(node)
    assert node.desired_state == AppiumDesiredState.running


async def test_request_start_pins_existing_node_port(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    """Operator start on a device with an existing AppiumNode row must pin the
    node's current port — NOT re-run candidate_ports, which re-offers the lowest
    free port (4723) during the pid-NULL gap and reallocates the node, inducing
    the two-supervisor 4723<->4725 oscillation (thrash fix #1).
    """
    device = await create_device(db_session, host_id=db_host.id, name="pin-existing-port", verified=True)
    # Node sits on 4725 with pid NULL and desired_state=stopped, so candidate_ports
    # would offer 4723 first (lowest free in the default 4723..4823 range).
    node = AppiumNode(
        device_id=device.id,
        port=4725,
        desired_state=AppiumDesiredState.stopped,
        desired_port=None,
        pid=None,
    )
    db_session.add(node)
    await db_session.flush()
    device.appium_node = node

    svc = OperatorNodeLifecycleService(
        review=build_review_service(), settings=FakeSettingsReader({}), publisher=event_bus
    )
    await svc.request_start(db_session, device, caller="operator_route", reason="operator start")
    await db_session.commit()
    await db_session.refresh(node)

    assert node.desired_port == 4725, (
        f"operator start must pin the existing node port (4725), not reallocate to a lower free port; "
        f"got {node.desired_port}"
    )


async def test_request_start_first_allocation_uses_candidate_ports(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    """First-time start (no AppiumNode row) must allocate via candidate_ports —
    the lowest free port in the configured range (4723)."""
    device = await create_device(db_session, host_id=db_host.id, name="first-alloc", verified=True)
    # Prime the relationship to a known-empty state so request_start's
    # ``device.appium_node`` read is the in-session value (no lazy IO).
    device.appium_node = None

    svc = OperatorNodeLifecycleService(
        review=build_review_service(), settings=FakeSettingsReader({}), publisher=event_bus
    )
    node = await svc.request_start(db_session, device, caller="operator_route", reason="operator start")
    await db_session.commit()
    await db_session.refresh(node)

    assert node.port == 4723, f"first allocation must use candidate_ports()[0]=4723; got {node.port}"
    assert node.desired_port == 4723


async def test_request_restart_moved_port_node_converges_without_oscillation(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    """Restart of a node whose agent-side process moved ports (the kill-window
    respawn) must converge on the node's own port — write_desired_state is called
    with node.port, not a reallocated low port — so the desired/observed ports do
    not oscillate.
    """
    device = await create_device(db_session, host_id=db_host.id, name="restart-moved-port", verified=True)
    # Node is observed running on 4725 (agent respawned here after a kill -9 on
    # 4723). candidate_ports would still re-offer 4723 first.
    node = AppiumNode(
        device_id=device.id,
        port=4725,
        desired_state=AppiumDesiredState.running,
        desired_port=4725,
        pid=27765,
        active_connection_target=device.connection_target,
    )
    db_session.add(node)
    await db_session.flush()
    device.appium_node = node

    svc = OperatorNodeLifecycleService(
        review=build_review_service(),
        settings=FakeSettingsReader({"appium_reconciler.restart_window_sec": 120}),
        publisher=event_bus,
    )
    await svc.request_restart(db_session, device, caller="operator_restart", reason="operator restart")
    await db_session.commit()
    await db_session.refresh(node)

    assert node.desired_port == 4725, (
        f"restart must converge on the running port (4725), not reallocate; got {node.desired_port}"
    )


# ---------------------------------------------------------------------------
# Parallel-resource reservation at request_start (8c item-1 regression)
# ---------------------------------------------------------------------------


async def _request_start(db_session: AsyncSession, device: Device) -> AppiumNode:
    # request_start reads device.appium_node as a plain attribute; a device fresh off
    # create_device() has it unloaded, and a bare lazy-load outside a greenlet context
    # raises MissingGreenlet. Prime it explicitly (same pattern used elsewhere in this
    # suite, e.g. tests/devices/test_device_health.py).
    await db_session.refresh(device, attribute_names=["appium_node"])
    return await OperatorNodeLifecycleService(
        review=build_review_service(), settings=FakeSettingsReader({}), publisher=event_bus
    ).request_start(db_session, device, caller="operator_route", reason="test start")


async def test_request_start_reserves_distinct_parallel_ports_for_host_neighbors(
    db_session: AsyncSession, db_host: Host
) -> None:
    dev_a = await create_device(db_session, host_id=db_host.id, name="par-ports-a", verified=True)
    dev_b = await create_device(db_session, host_id=db_host.id, name="par-ports-b", verified=True)

    node_a = await _request_start(db_session, dev_a)
    node_b = await _request_start(db_session, dev_b)

    claims = await resource_service.get_port_claims_for_nodes(db_session, node_ids=[node_a.id, node_b.id])
    assert claims[node_a.id]["appium:systemPort"] == 8200
    assert claims[node_b.id]["appium:systemPort"] == 8201
    assert claims[node_a.id]["appium:mjpegServerPort"] != claims[node_b.id]["appium:mjpegServerPort"]


async def test_request_start_reuses_existing_claims_on_restart(db_session: AsyncSession, db_host: Host) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="par-ports-reuse", verified=True)

    node = await _request_start(db_session, device)
    first = await resource_service.get_port_claims_for_nodes(db_session, node_ids=[node.id])
    node_again = await _request_start(db_session, device)
    second = await resource_service.get_port_claims_for_nodes(db_session, node_ids=[node_again.id])

    assert node_again.id == node.id
    assert second == first


async def test_request_start_drops_claims_no_longer_declared_by_the_pack(
    db_session: AsyncSession, db_host: Host
) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="par-ports-stale", verified=True)
    node = await _request_start(db_session, device)
    db_session.add(
        AppiumNodeResourceClaim(host_id=db_host.id, capability_key="appium:retiredPort", port=7777, node_id=node.id)
    )
    await db_session.flush()

    await _request_start(db_session, device)

    claims = (await resource_service.get_port_claims_for_nodes(db_session, node_ids=[node.id])).get(node.id, {})
    assert "appium:retiredPort" not in claims
    assert "appium:systemPort" in claims


async def test_request_start_assigns_derived_data_path_for_xcuitest(db_session: AsyncSession, db_host: Host) -> None:
    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="par-ports-ios",
        verified=True,
        pack_id="appium-xcuitest",
        platform_id="ios",
        identity_scheme="apple_udid",
        identity_scope="global",
    )

    node = await _request_start(db_session, device)

    caps = await resource_service.get_capabilities(db_session, node_id=node.id)
    assert caps["appium:wdaLocalPort"] == 8100
    assert str(caps["appium:derivedDataPath"]).startswith("/tmp/gridfleet/derived-data/")


async def test_request_start_maps_pool_exhaustion_to_node_manager_error(
    db_session: AsyncSession, db_host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="par-ports-full", verified=True)

    async def _exhausted(*args: object, **kwargs: object) -> int:
        raise resource_service.PoolExhaustedError("no free port")

    monkeypatch.setattr(resource_service, "reserve", _exhausted)

    with pytest.raises(NodeManagerError):
        await _request_start(db_session, device)
