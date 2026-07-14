"""Phase 2 — the StatusFoldLoop folds node_health off the request path."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock

import pytest

from app.appium_nodes.models import AppiumNode
from app.appium_nodes.services.node_health import NodeHealthService
from app.appium_nodes.services.status_fold_loop import FOLD_SECTION, StatusFoldLoop
from app.core.leader import state_store as control_plane_state_store
from app.core.observation_revision import next_observation_revision
from app.core.timeutil import now_utc
from app.devices.services.health import DeviceHealthService
from app.hosts.models import Host
from app.hosts.service_status_push import HOST_STATUS_NAMESPACE
from tests.fakes import FakeSettingsReader
from tests.helpers import seed_host_and_running_node
from tests.helpers import test_event_bus as event_bus

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

pytestmark = pytest.mark.usefixtures("seeded_driver_packs")


def _service() -> NodeHealthService:
    return NodeHealthService(
        publisher=event_bus,
        settings=FakeSettingsReader({"general.node_fail_window_sec": 60, "appium_reconciler.restart_window_sec": 300}),
        recovery_control=AsyncMock(),
        health=DeviceHealthService(publisher=event_bus),
        incidents=AsyncMock(),
    )


def _loop(node_health: NodeHealthService, session_factory: async_sessionmaker[AsyncSession]) -> StatusFoldLoop:
    return StatusFoldLoop(node_health=node_health, session_factory=session_factory)


async def _store_node_health_snapshot(
    db_session: AsyncSession, host_id: uuid.UUID, *, revision: int, nodes: list[dict[str, Any]]
) -> None:
    await control_plane_state_store.set_value(
        db_session,
        HOST_STATUS_NAMESPACE,
        str(host_id),
        {
            "received_at": now_utc().isoformat(),
            "payload": {
                "node_health": {
                    "reported_at": now_utc().isoformat(),
                    "nodes": nodes,
                    "observation_revision": revision,
                }
            },
        },
    )
    await db_session.commit()


def _entry(port: int, pid: int, target: str, *, running: bool) -> dict[str, Any]:
    return {
        "port": port,
        "pid": pid,
        "connection_target": target,
        "running": running,
        "observed_at": now_utc().isoformat(),
    }


async def test_loop_folds_pushed_node_health_and_advances_watermark(
    db_session: AsyncSession, db_session_maker: async_sessionmaker[AsyncSession]
) -> None:
    _host, device, node = await seed_host_and_running_node(db_session, identity="fold-loop")
    host_id, node_id = device.host_id, node.id
    revision = await next_observation_revision(db_session)
    await _store_node_health_snapshot(
        db_session,
        host_id,
        revision=revision,
        nodes=[_entry(node.port, node.pid, node.active_connection_target, running=False)],
    )

    # The loop is the only thing that folds node_health now — no in-process signal.
    await _loop(_service(), db_session_maker)._run_cycle(db_session)

    # The loop committed in its own sessions; drop this session's cached rows.
    db_session.expire_all()
    folded_node = await db_session.get(AppiumNode, node_id)
    assert folded_node is not None
    assert folded_node.health_state == "error"
    assert folded_node.health_failing_since is not None
    host = await db_session.get(Host, host_id)
    assert host is not None
    assert host.observation_applied.get(FOLD_SECTION) == revision


async def test_loop_skips_when_revision_not_advanced(
    db_session: AsyncSession, db_session_maker: async_sessionmaker[AsyncSession]
) -> None:
    _host, device, node = await seed_host_and_running_node(db_session, identity="fold-skip")
    revision = await next_observation_revision(db_session)
    # Pre-mark the section as already applied at this revision.
    host = await db_session.get(Host, device.host_id)
    assert host is not None
    host.observation_applied = {FOLD_SECTION: revision}
    await db_session.commit()

    await _store_node_health_snapshot(
        db_session,
        device.host_id,
        revision=revision,
        nodes=[_entry(node.port, node.pid, node.active_connection_target, running=False)],
    )

    fold = AsyncMock(return_value=True)
    node_health = _service()
    node_health.fold_host_nodes = fold  # type: ignore[method-assign]
    await _loop(node_health, db_session_maker)._run_cycle(db_session)

    fold.assert_not_awaited()


async def test_loop_retryable_node_holds_watermark(
    db_session: AsyncSession, db_session_maker: async_sessionmaker[AsyncSession]
) -> None:
    _host, device, node = await seed_host_and_running_node(db_session, identity="fold-retry")
    host_id = device.host_id
    revision = await next_observation_revision(db_session)
    await _store_node_health_snapshot(
        db_session,
        host_id,
        revision=revision,
        nodes=[_entry(node.port, node.pid, node.active_connection_target, running=False)],
    )

    node_health = _service()
    # A node that raises mid-write makes the host section unsettled (returns False).
    node_health.fold_host_nodes = AsyncMock(return_value=False)  # type: ignore[method-assign]
    await _loop(node_health, db_session_maker)._run_cycle(db_session)

    db_session.expire_all()
    host = await db_session.get(Host, host_id)
    assert host is not None
    assert host.observation_applied.get(FOLD_SECTION) is None  # watermark not advanced


async def test_loop_contains_per_host_failure(
    db_session: AsyncSession, db_session_maker: async_sessionmaker[AsyncSession]
) -> None:
    _h1, d1, n1 = await seed_host_and_running_node(db_session, identity="fold-good")
    _h2, d2, n2 = await seed_host_and_running_node(db_session, identity="fold-bad")
    h1_id, h2_id, n1_id = d1.host_id, d2.host_id, n1.id
    r1 = await next_observation_revision(db_session)
    r2 = await next_observation_revision(db_session)
    await _store_node_health_snapshot(
        db_session, h1_id, revision=r1, nodes=[_entry(n1.port, n1.pid, n1.active_connection_target, running=False)]
    )
    await _store_node_health_snapshot(
        db_session, h2_id, revision=r2, nodes=[_entry(n2.port, n2.pid, n2.active_connection_target, running=False)]
    )

    real = _service().fold_host_nodes
    node_health = _service()

    async def flaky(db: AsyncSession, host_id: uuid.UUID, section: dict[str, Any]) -> bool:
        if host_id == h2_id:
            raise RuntimeError("boom")
        return await real(db, host_id, section)

    node_health.fold_host_nodes = flaky  # type: ignore[method-assign]
    await _loop(node_health, db_session_maker)._run_cycle(db_session)

    db_session.expire_all()
    folded = await db_session.get(AppiumNode, n1_id)
    assert folded is not None
    assert folded.health_state == "error"  # good host folded despite the bad host raising
    host1 = await db_session.get(Host, h1_id)
    assert host1 is not None
    assert host1.observation_applied.get(FOLD_SECTION) == r1
    host2 = await db_session.get(Host, h2_id)
    assert host2 is not None
    assert host2.observation_applied.get(FOLD_SECTION) is None
