from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock

from sqlalchemy import select

from app.agent_comm.models import AgentReconfigureOutbox
from app.agent_comm.reconfigure_delivery import (
    MAX_DELIVERY_ATTEMPTS,
    InlineReconfigureDeliveryFailedError,
    _record_delivery_failure,
    deliver_agent_reconfigures,
    deliver_pending_agent_reconfigures,
)
from app.appium_nodes.models import AppiumDesiredState, AppiumNode
from app.core.errors import AgentResponseError
from app.hosts.models import Host, HostStatus, OSType
from tests.fakes import FakeSettingsReader
from tests.helpers import create_device
from tests.helpers import test_event_bus as event_bus

if TYPE_CHECKING:
    import pytest
    from sqlalchemy.ext.asyncio import AsyncSession

SETTINGS = FakeSettingsReader()
CIRCUIT_BREAKER = Mock()
POOL = Mock()


async def test_delivery_forwards_agent_auth_pool(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reconfigure delivery must forward the agent BasicAuth pool to the agent
    call. Without it the request is unauthenticated and the agent rejects it
    when the auth gate is enabled, so the node is never reconfigured."""
    device = await create_device(db_session, host_id=db_host.id, name="auth-pool")
    node = AppiumNode(
        device_id=device.id,
        port=4723,
        desired_state=AppiumDesiredState.running,
        desired_port=4723,
        generation=4,
    )
    row = AgentReconfigureOutbox(
        device_id=device.id,
        port=4723,
        accepting_new_sessions=True,
        stop_pending=False,
        reconciled_generation=4,
    )
    db_session.add_all([node, row])
    await db_session.commit()
    reconfigure = AsyncMock(return_value={"port": 4723})
    monkeypatch.setattr("app.agent_comm.reconfigure_delivery.agent_operations.agent_appium_reconfigure", reconfigure)

    await deliver_agent_reconfigures(
        db_session, device.id, settings=SETTINGS, circuit_breaker=CIRCUIT_BREAKER, publisher=event_bus, pool=POOL
    )

    assert reconfigure.await_args.kwargs["pool"] is POOL


async def test_stale_outbox_row_is_marked_delivered_without_agent_call(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="stale-outbox")
    node = AppiumNode(
        device_id=device.id,
        port=4723,
        desired_state=AppiumDesiredState.running,
        desired_port=4723,
        generation=3,
    )
    row = AgentReconfigureOutbox(
        device_id=device.id,
        port=4723,
        accepting_new_sessions=False,
        stop_pending=True,
        reconciled_generation=2,
    )
    db_session.add_all([node, row])
    await db_session.commit()
    reconfigure = AsyncMock()
    monkeypatch.setattr("app.agent_comm.reconfigure_delivery.agent_operations.agent_appium_reconfigure", reconfigure)

    await deliver_agent_reconfigures(
        db_session, device.id, settings=SETTINGS, circuit_breaker=CIRCUIT_BREAKER, publisher=event_bus
    )

    stored = (await db_session.execute(select(AgentReconfigureOutbox))).scalar_one()
    assert stored.delivered_at is not None
    reconfigure.assert_not_awaited()


async def test_outbox_row_sends_when_generation_matches(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="fresh-outbox")
    node = AppiumNode(
        device_id=device.id,
        port=4723,
        desired_state=AppiumDesiredState.running,
        desired_port=4723,
        generation=4,
    )
    row = AgentReconfigureOutbox(
        device_id=device.id,
        port=4723,
        accepting_new_sessions=False,
        stop_pending=True,
        reconciled_generation=4,
    )
    db_session.add_all([node, row])
    await db_session.commit()
    reconfigure = AsyncMock(return_value={"port": 4723})
    monkeypatch.setattr("app.agent_comm.reconfigure_delivery.agent_operations.agent_appium_reconfigure", reconfigure)

    await deliver_agent_reconfigures(
        db_session, device.id, settings=SETTINGS, circuit_breaker=CIRCUIT_BREAKER, publisher=event_bus
    )

    stored = (await db_session.execute(select(AgentReconfigureOutbox))).scalar_one()
    assert stored.delivered_at is not None
    reconfigure.assert_awaited_once_with(
        db_host.ip,
        db_host.agent_port,
        port=4723,
        accepting_new_sessions=False,
        stop_pending=True,
        grid_run_id=None,
        timeout=10,
        settings=SETTINGS,
        pool=None,
        circuit_breaker=CIRCUIT_BREAKER,
    )


async def test_outbox_row_sends_when_generation_behind_but_config_still_current(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pending row must still be delivered when ``node.generation`` advanced
    past its ``reconciled_generation`` due to an unrelated field change (recovery
    flags, desired_port) that did not alter the agent-visible desired config.
    Generation lagging only means "stale" when the row's payload no longer
    matches the node's desired config; otherwise skipping it silently strands
    the reconfigure and the node never learns its run id."""
    run_id = uuid.uuid4()
    device = await create_device(db_session, host_id=db_host.id, name="behind-but-current")
    node = AppiumNode(
        device_id=device.id,
        port=4723,
        desired_state=AppiumDesiredState.running,
        desired_port=4723,
        desired_grid_run_id=run_id,
        accepting_new_sessions=True,
        stop_pending=False,
        generation=3,
    )
    row = AgentReconfigureOutbox(
        device_id=device.id,
        port=4723,
        accepting_new_sessions=True,
        stop_pending=False,
        grid_run_id=run_id,
        reconciled_generation=2,
    )
    db_session.add_all([node, row])
    await db_session.commit()
    reconfigure = AsyncMock(return_value={"port": 4723})
    monkeypatch.setattr("app.agent_comm.reconfigure_delivery.agent_operations.agent_appium_reconfigure", reconfigure)

    await deliver_agent_reconfigures(
        db_session, device.id, settings=SETTINGS, circuit_breaker=CIRCUIT_BREAKER, publisher=event_bus
    )

    stored = (await db_session.execute(select(AgentReconfigureOutbox))).scalar_one()
    assert stored.delivered_at is not None
    reconfigure.assert_awaited_once_with(
        db_host.ip,
        db_host.agent_port,
        port=4723,
        accepting_new_sessions=True,
        stop_pending=False,
        grid_run_id=run_id,
        timeout=10,
        settings=SETTINGS,
        pool=None,
        circuit_breaker=CIRCUIT_BREAKER,
    )


async def test_outbox_delivery_failure_increments_attempts(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.core.errors import AgentUnreachableError

    device = await create_device(db_session, host_id=db_host.id, name="failed-outbox")
    node = AppiumNode(
        device_id=device.id,
        port=4723,
        desired_state=AppiumDesiredState.running,
        desired_port=4723,
        generation=1,
    )
    row = AgentReconfigureOutbox(
        device_id=device.id,
        port=4723,
        accepting_new_sessions=True,
        stop_pending=False,
        reconciled_generation=1,
        created_at=datetime.now(UTC),
    )
    db_session.add_all([node, row])
    await db_session.commit()
    monkeypatch.setattr(
        "app.agent_comm.reconfigure_delivery.agent_operations.agent_appium_reconfigure",
        AsyncMock(side_effect=AgentUnreachableError(db_host.ip, "offline")),
    )

    await deliver_agent_reconfigures(
        db_session, device.id, settings=SETTINGS, circuit_breaker=CIRCUIT_BREAKER, publisher=event_bus
    )

    stored = (await db_session.execute(select(AgentReconfigureOutbox))).scalar_one()
    assert stored.delivered_at is None
    assert stored.delivery_attempts == 1


async def test_outbox_delivery_failure_raises_when_raise_on_failure_true(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Inline callers (cooldown HTTP handler) must learn about delivery
    failures so the response can be a non-2xx. Without this signal the
    testkit treats the cooldown as effective and the next session lands on
    the device that was supposed to be drained.
    """
    import pytest as _pytest

    from app.core.errors import AgentUnreachableError

    device = await create_device(db_session, host_id=db_host.id, name="inline-failed-outbox")
    node = AppiumNode(
        device_id=device.id,
        port=4723,
        desired_state=AppiumDesiredState.running,
        desired_port=4723,
        generation=1,
    )
    row = AgentReconfigureOutbox(
        device_id=device.id,
        port=4723,
        accepting_new_sessions=False,
        stop_pending=True,
        reconciled_generation=1,
        created_at=datetime.now(UTC),
    )
    db_session.add_all([node, row])
    await db_session.commit()
    monkeypatch.setattr(
        "app.agent_comm.reconfigure_delivery.agent_operations.agent_appium_reconfigure",
        AsyncMock(side_effect=AgentUnreachableError(db_host.ip, "offline")),
    )

    with _pytest.raises(InlineReconfigureDeliveryFailedError):
        await deliver_agent_reconfigures(
            db_session,
            device.id,
            raise_on_failure=True,
            settings=SETTINGS,
            circuit_breaker=CIRCUIT_BREAKER,
            publisher=event_bus,
        )

    stored = (await db_session.execute(select(AgentReconfigureOutbox))).scalar_one()
    # Failure is still recorded on the row so the background retry loop
    # can pick up where the inline call left off — surfacing the exception
    # must not skip the bookkeeping.
    assert stored.delivered_at is None
    assert stored.delivery_attempts == 1


async def test_outbox_delivery_failure_swallowed_by_default(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Background callers (delivery loop, intent reconciler, etc.) keep the
    legacy behavior: failures are recorded and retried, never raised. Only
    the explicit ``raise_on_failure=True`` path propagates the exception.
    """
    from app.core.errors import AgentUnreachableError

    device = await create_device(db_session, host_id=db_host.id, name="bg-failed-outbox")
    node = AppiumNode(
        device_id=device.id,
        port=4723,
        desired_state=AppiumDesiredState.running,
        desired_port=4723,
        generation=1,
    )
    row = AgentReconfigureOutbox(
        device_id=device.id,
        port=4723,
        accepting_new_sessions=False,
        stop_pending=True,
        reconciled_generation=1,
        created_at=datetime.now(UTC),
    )
    db_session.add_all([node, row])
    await db_session.commit()
    monkeypatch.setattr(
        "app.agent_comm.reconfigure_delivery.agent_operations.agent_appium_reconfigure",
        AsyncMock(side_effect=AgentUnreachableError(db_host.ip, "offline")),
    )

    # Must not raise — default behavior swallows for the loop callers.
    await deliver_agent_reconfigures(
        db_session, device.id, settings=SETTINGS, circuit_breaker=CIRCUIT_BREAKER, publisher=event_bus
    )


async def test_delivery_marks_older_duplicate_generation_rows_delivered(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="duplicate-outbox")
    node = AppiumNode(
        device_id=device.id,
        port=4723,
        desired_state=AppiumDesiredState.running,
        desired_port=4723,
        generation=7,
    )
    older = AgentReconfigureOutbox(
        device_id=device.id,
        port=4723,
        accepting_new_sessions=False,
        stop_pending=True,
        reconciled_generation=7,
        created_at=datetime.now(UTC) - timedelta(seconds=5),
    )
    newest = AgentReconfigureOutbox(
        device_id=device.id,
        port=4723,
        accepting_new_sessions=True,
        stop_pending=False,
        reconciled_generation=7,
        created_at=datetime.now(UTC),
    )
    db_session.add_all([node, older, newest])
    await db_session.commit()
    reconfigure = AsyncMock(return_value={"port": 4723})
    monkeypatch.setattr("app.agent_comm.reconfigure_delivery.agent_operations.agent_appium_reconfigure", reconfigure)

    await deliver_agent_reconfigures(
        db_session, device.id, settings=SETTINGS, circuit_breaker=CIRCUIT_BREAKER, publisher=event_bus
    )

    await db_session.refresh(older)
    await db_session.refresh(newest)
    assert older.delivered_at is not None
    assert newest.delivered_at is not None
    reconfigure.assert_awaited_once_with(
        db_host.ip,
        db_host.agent_port,
        port=4723,
        accepting_new_sessions=True,
        stop_pending=False,
        grid_run_id=None,
        timeout=10,
        settings=SETTINGS,
        pool=None,
        circuit_breaker=CIRCUIT_BREAKER,
    )


async def test_delivery_processes_at_most_one_batch_per_device(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="limited-outbox")
    node = AppiumNode(
        device_id=device.id,
        port=4723,
        desired_state=AppiumDesiredState.running,
        desired_port=4723,
        generation=1,
    )
    now = datetime.now(UTC)
    rows = [
        AgentReconfigureOutbox(
            device_id=device.id,
            port=4723,
            accepting_new_sessions=bool(index % 2),
            stop_pending=False,
            reconciled_generation=index + 1,
            created_at=now + timedelta(seconds=index),
        )
        for index in range(6)
    ]
    db_session.add_all([node, *rows])
    await db_session.commit()
    reconfigure = AsyncMock(return_value={"port": 4723})
    monkeypatch.setattr("app.agent_comm.reconfigure_delivery.agent_operations.agent_appium_reconfigure", reconfigure)

    await deliver_agent_reconfigures(
        db_session, device.id, settings=SETTINGS, circuit_breaker=CIRCUIT_BREAKER, publisher=event_bus
    )

    delivered = (
        (
            await db_session.execute(
                select(AgentReconfigureOutbox).where(AgentReconfigureOutbox.delivered_at.is_not(None))
            )
        )
        .scalars()
        .all()
    )
    pending = (
        (await db_session.execute(select(AgentReconfigureOutbox).where(AgentReconfigureOutbox.delivered_at.is_(None))))
        .scalars()
        .all()
    )
    assert len(delivered) == 5
    assert len(pending) == 1
    assert reconfigure.await_count == 5


async def test_deliver_pending_skips_pull_host_devices(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stale outbox row for a pull-capable host must not be delivered as a
    legacy reconfigure by the batch loop, and must not even trigger a poke —
    ``deliver_pending_agent_reconfigures`` filters these devices out entirely."""
    pull_host = Host(
        hostname=f"pull-host-{uuid.uuid4().hex[:8]}",
        ip="10.0.0.251",
        os_type=OSType.linux,
        agent_port=5100,
        status=HostStatus.online,
        capabilities={"node_desired_pull": True},
    )
    db_session.add(pull_host)
    await db_session.flush()

    legacy_device = await create_device(db_session, host_id=db_host.id, name="pending-legacy")
    legacy_node = AppiumNode(
        device_id=legacy_device.id,
        port=4723,
        desired_state=AppiumDesiredState.running,
        desired_port=4723,
        generation=1,
    )
    legacy_row = AgentReconfigureOutbox(
        device_id=legacy_device.id,
        port=4723,
        accepting_new_sessions=True,
        stop_pending=False,
        reconciled_generation=1,
    )
    pull_device = await create_device(db_session, host_id=pull_host.id, name="pending-pull")
    pull_node = AppiumNode(
        device_id=pull_device.id,
        port=4724,
        desired_state=AppiumDesiredState.running,
        desired_port=4724,
        generation=1,
    )
    stale_pull_row = AgentReconfigureOutbox(
        device_id=pull_device.id,
        port=4724,
        accepting_new_sessions=True,
        stop_pending=False,
        reconciled_generation=1,
    )
    db_session.add_all([legacy_node, legacy_row, pull_node, stale_pull_row])
    await db_session.commit()
    reconfigure = AsyncMock(return_value={"port": 4723})
    monkeypatch.setattr("app.agent_comm.reconfigure_delivery.agent_operations.agent_appium_reconfigure", reconfigure)
    poke = AsyncMock()
    monkeypatch.setattr("app.agent_comm.reconfigure_delivery.agent_operations.agent_nodes_refresh", poke)

    await deliver_pending_agent_reconfigures(
        db_session, settings=SETTINGS, circuit_breaker=CIRCUIT_BREAKER, publisher=event_bus
    )

    reconfigure.assert_awaited_once()
    poke.assert_not_awaited()
    await db_session.refresh(legacy_row)
    await db_session.refresh(stale_pull_row)
    assert legacy_row.delivered_at is not None
    assert stale_pull_row.delivered_at is None


async def test_delivery_abandons_row_after_max_attempts(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.core.errors import AgentUnreachableError

    device = await create_device(db_session, host_id=db_host.id, name="abandoned-outbox")
    node = AppiumNode(
        device_id=device.id,
        port=4723,
        desired_state=AppiumDesiredState.running,
        desired_port=4723,
        generation=1,
    )
    row = AgentReconfigureOutbox(
        device_id=device.id,
        port=4723,
        accepting_new_sessions=True,
        stop_pending=False,
        reconciled_generation=1,
        delivery_attempts=MAX_DELIVERY_ATTEMPTS - 1,
    )
    db_session.add_all([node, row])
    await db_session.commit()
    reconfigure = AsyncMock(side_effect=AgentUnreachableError(db_host.ip, "offline"))
    monkeypatch.setattr("app.agent_comm.reconfigure_delivery.agent_operations.agent_appium_reconfigure", reconfigure)

    await deliver_agent_reconfigures(
        db_session, device.id, settings=SETTINGS, circuit_breaker=CIRCUIT_BREAKER, publisher=event_bus
    )
    await deliver_agent_reconfigures(
        db_session, device.id, settings=SETTINGS, circuit_breaker=CIRCUIT_BREAKER, publisher=event_bus
    )

    await db_session.refresh(row)
    assert row.delivered_at is None
    assert row.abandoned_at is not None
    assert row.delivery_attempts == MAX_DELIVERY_ATTEMPTS
    assert reconfigure.await_count == 1


def test_delivery_failure_uses_specific_abandonment_reason() -> None:
    row = AgentReconfigureOutbox(
        port=4723,
        accepting_new_sessions=True,
        stop_pending=False,
        reconciled_generation=1,
        delivery_attempts=MAX_DELIVERY_ATTEMPTS - 1,
    )

    _record_delivery_failure(row, abandoned_reason="host missing")

    assert row.abandoned_at is not None
    assert row.abandoned_reason == "host missing"
    assert row.delivery_attempts == MAX_DELIVERY_ATTEMPTS


async def test_404_no_process_clears_stale_observed_state_and_consumes_row(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """N11 (2026-06-07): the reconfigure route's only 404 is DEVICE_NOT_FOUND — the
    agent authoritatively reports no managed process on the port. A node row still
    claiming a pid there is stale (the process died outside a reconciler-issued stop,
    e.g. a maintenance graceful drain) and would otherwise persist for up to one
    appium_reconciler interval (30s), retiring start intents via their node_running
    precondition and pointing probes at a dead port. Delivery must clear the stale
    observation immediately and consume the undeliverable row."""
    device = await create_device(db_session, host_id=db_host.id, name="stale-404")
    node = AppiumNode(
        device_id=device.id,
        port=4725,
        desired_state=AppiumDesiredState.running,
        desired_port=4725,
        pid=4242,
        active_connection_target=device.connection_target,
        generation=4,
    )
    row = AgentReconfigureOutbox(
        device_id=device.id,
        port=4725,
        accepting_new_sessions=True,
        stop_pending=False,
        grid_run_id=None,
        reconciled_generation=4,
    )
    db_session.add_all([node, row])
    await db_session.commit()
    monkeypatch.setattr(
        "app.agent_comm.reconfigure_delivery.agent_operations.agent_appium_reconfigure",
        AsyncMock(
            side_effect=AgentResponseError(
                db_host.ip, "Agent reconfigure Appium node failed (HTTP 404)", http_status=404
            )
        ),
    )

    await deliver_agent_reconfigures(
        db_session, device.id, settings=SETTINGS, circuit_breaker=CIRCUIT_BREAKER, publisher=event_bus
    )

    await db_session.refresh(node)
    assert node.pid is None, "stale pid must be cleared on agent-reported absence"
    assert node.active_connection_target is None
    stored = (await db_session.execute(select(AgentReconfigureOutbox))).scalar_one()
    assert stored.delivered_at is not None, "undeliverable row must be consumed, not retried"
    assert stored.delivery_attempts == 0, "absence is not a delivery failure"


async def test_404_no_process_on_moved_port_consumes_row_without_clearing(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 404 for an outbox row whose port the node has since left says nothing about
    the node's current process — consume the row but leave the observation alone."""
    device = await create_device(db_session, host_id=db_host.id, name="moved-404")
    node = AppiumNode(
        device_id=device.id,
        port=4726,  # node moved; the outbox row below still targets 4725
        desired_state=AppiumDesiredState.running,
        desired_port=4726,
        pid=4242,
        active_connection_target=device.connection_target,
        generation=4,
    )
    row = AgentReconfigureOutbox(
        device_id=device.id,
        port=4725,
        accepting_new_sessions=True,
        stop_pending=False,
        grid_run_id=None,
        reconciled_generation=4,
    )
    db_session.add_all([node, row])
    await db_session.commit()
    monkeypatch.setattr(
        "app.agent_comm.reconfigure_delivery.agent_operations.agent_appium_reconfigure",
        AsyncMock(
            side_effect=AgentResponseError(
                db_host.ip, "Agent reconfigure Appium node failed (HTTP 404)", http_status=404
            )
        ),
    )

    await deliver_agent_reconfigures(
        db_session, device.id, settings=SETTINGS, circuit_breaker=CIRCUIT_BREAKER, publisher=event_bus
    )

    await db_session.refresh(node)
    assert node.pid == 4242, "a 404 for a stale port must not clear the live observation"
    stored = (await db_session.execute(select(AgentReconfigureOutbox))).scalar_one()
    assert stored.delivered_at is not None


async def test_404_mark_node_stopped_failure_records_delivery_failure(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BUG-1: if mark_node_stopped raises in the 404 path, the row must be recorded
    as a delivery failure (attempts incremented, not consumed) so it can eventually
    be abandoned — never left at attempts=0 to re-select and re-call the agent forever."""
    device = await create_device(db_session, host_id=db_host.id, name="stopfail-404")
    node = AppiumNode(
        device_id=device.id,
        port=4725,
        desired_state=AppiumDesiredState.running,
        desired_port=4725,
        pid=4242,
        active_connection_target=device.connection_target,
        generation=4,
    )
    row = AgentReconfigureOutbox(
        device_id=device.id,
        port=4725,
        accepting_new_sessions=True,
        stop_pending=False,
        grid_run_id=None,
        reconciled_generation=4,
    )
    db_session.add_all([node, row])
    await db_session.commit()
    monkeypatch.setattr(
        "app.agent_comm.reconfigure_delivery.agent_operations.agent_appium_reconfigure",
        AsyncMock(
            side_effect=AgentResponseError(
                db_host.ip, "Agent reconfigure Appium node failed (HTTP 404)", http_status=404
            )
        ),
    )
    monkeypatch.setattr(
        "app.appium_nodes.services.reconciler_agent.mark_node_stopped",
        AsyncMock(side_effect=RuntimeError("deadlock in mark_dirty_and_reconcile")),
    )

    # Must not propagate the mark_node_stopped failure out of the delivery loop.
    await deliver_agent_reconfigures(
        db_session, device.id, settings=SETTINGS, circuit_breaker=CIRCUIT_BREAKER, publisher=event_bus
    )

    stored = (await db_session.execute(select(AgentReconfigureOutbox))).scalar_one()
    assert stored.delivery_attempts == 1, "mark_node_stopped failure must count as a delivery attempt"
    assert stored.delivered_at is None, "a row whose cleanup failed must stay retryable, not be consumed"


async def test_pull_host_pokes_instead_of_delivering_and_leaves_row_untouched(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A host advertising ``node_desired_pull`` gets a fire-and-forget poke instead
    of the outbox scan/delivery. A pre-upgrade stale row (8b should never stage new
    ones for a pull host) must be left undelivered — 8c drops the table."""
    db_host.capabilities = {"node_desired_pull": True}
    await db_session.commit()
    device = await create_device(db_session, host_id=db_host.id, name="pull-poke")
    node = AppiumNode(
        device_id=device.id,
        port=4723,
        desired_state=AppiumDesiredState.running,
        desired_port=4723,
        generation=4,
    )
    row = AgentReconfigureOutbox(
        device_id=device.id,
        port=4723,
        accepting_new_sessions=True,
        stop_pending=False,
        reconciled_generation=4,
    )
    db_session.add_all([node, row])
    await db_session.commit()
    reconfigure = AsyncMock()
    monkeypatch.setattr("app.agent_comm.reconfigure_delivery.agent_operations.agent_appium_reconfigure", reconfigure)
    poke = AsyncMock()
    monkeypatch.setattr("app.agent_comm.reconfigure_delivery.agent_operations.agent_nodes_refresh", poke)

    await deliver_agent_reconfigures(
        db_session, device.id, settings=SETTINGS, circuit_breaker=CIRCUIT_BREAKER, publisher=event_bus, pool=POOL
    )

    reconfigure.assert_not_awaited()
    poke.assert_awaited_once_with(
        db_host.ip, db_host.agent_port, settings=SETTINGS, pool=POOL, circuit_breaker=CIRCUIT_BREAKER
    )
    stored = (await db_session.execute(select(AgentReconfigureOutbox))).scalar_one()
    assert stored.delivered_at is None
    assert stored.delivery_attempts == 0


async def test_pull_host_poke_failure_is_swallowed(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A poke failure (agent unreachable) must be logged, not propagated —
    the reconcile/delivery caller must not be affected."""
    from app.core.errors import AgentUnreachableError

    db_host.capabilities = {"node_desired_pull": True}
    await db_session.commit()
    device = await create_device(db_session, host_id=db_host.id, name="pull-poke-fail")
    poke = AsyncMock(side_effect=AgentUnreachableError(db_host.ip, "offline"))
    monkeypatch.setattr("app.agent_comm.reconfigure_delivery.agent_operations.agent_nodes_refresh", poke)

    # Must not raise.
    await deliver_agent_reconfigures(
        db_session, device.id, settings=SETTINGS, circuit_breaker=CIRCUIT_BREAKER, publisher=event_bus
    )

    poke.assert_awaited_once()


async def test_non_404_response_error_keeps_failure_path(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the 404 absence signal is consumed; other agent errors stay retryable."""
    device = await create_device(db_session, host_id=db_host.id, name="err-500")
    node = AppiumNode(
        device_id=device.id,
        port=4725,
        desired_state=AppiumDesiredState.running,
        desired_port=4725,
        pid=4242,
        active_connection_target=device.connection_target,
        generation=4,
    )
    row = AgentReconfigureOutbox(
        device_id=device.id,
        port=4725,
        accepting_new_sessions=True,
        stop_pending=False,
        grid_run_id=None,
        reconciled_generation=4,
    )
    db_session.add_all([node, row])
    await db_session.commit()
    monkeypatch.setattr(
        "app.agent_comm.reconfigure_delivery.agent_operations.agent_appium_reconfigure",
        AsyncMock(
            side_effect=AgentResponseError(
                db_host.ip, "Agent reconfigure Appium node failed (HTTP 500)", http_status=500
            )
        ),
    )

    await deliver_agent_reconfigures(
        db_session, device.id, settings=SETTINGS, circuit_breaker=CIRCUIT_BREAKER, publisher=event_bus
    )

    await db_session.refresh(node)
    assert node.pid == 4242
    stored = (await db_session.execute(select(AgentReconfigureOutbox))).scalar_one()
    assert stored.delivered_at is None
    assert stored.delivery_attempts == 1
