"""Phase 2 — the StatusFoldLoop folds node_health off the request path."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, Mock

import pytest

from app.appium_nodes.models import AppiumDesiredState, AppiumNode
from app.appium_nodes.services import node_health as node_health_module
from app.appium_nodes.services import status_fold_loop as status_fold_module
from app.appium_nodes.services.node_health import NodeFoldOutcome, NodeHealthService, _NodeObservation
from app.appium_nodes.services.status_fold_loop import FOLD_SECTION, FoldSection, StatusFoldLoop
from app.core.leader import state_store as control_plane_state_store
from app.core.observation_revision import next_observation_revision
from app.core.timeutil import now_utc
from app.devices.services.health import DeviceHealthService
from app.hosts.models import Host
from app.hosts.service_status_push import HOST_STATUS_NAMESPACE
from tests.fakes import FakeSettingsReader
from tests.helpers import seed_host_and_running_node, seed_host_with_devices
from tests.helpers import test_event_bus as event_bus

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.devices.models import Device

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
    return StatusFoldLoop(
        sections=(FoldSection(FOLD_SECTION, node_health.fold_host_nodes),), session_factory=session_factory
    )


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


async def test_loop_yields_remaining_hosts_after_cycle_budget(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host_keys = [str(uuid.uuid4()), str(uuid.uuid4())]
    snapshots = {key: {"payload": {}} for key in host_keys}
    monkeypatch.setattr(control_plane_state_store, "get_values", AsyncMock(return_value=snapshots))
    ticks = iter(
        (
            0.0,
            status_fold_module.STATUS_FOLD_CYCLE_BUDGET_SEC + 0.1,
            0.0,
            status_fold_module.STATUS_FOLD_CYCLE_BUDGET_SEC + 0.1,
        )
    )
    monkeypatch.setattr(status_fold_module, "perf_counter", lambda: next(ticks))
    loop = _loop(_service(), db_session_maker)
    loop._load_applied = AsyncMock(return_value={})  # type: ignore[method-assign]
    loop._fold_host = AsyncMock()  # type: ignore[method-assign]

    await loop._run_cycle(db_session)
    await loop._run_cycle(db_session)

    folded_hosts = [call.args[0] for call in loop._fold_host.await_args_list]  # type: ignore[attr-defined]
    assert folded_hosts == host_keys


async def test_loop_retryable_node_holds_watermark(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
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
    record_lag = Mock()
    monkeypatch.setattr(status_fold_module, "record_status_fold_lag", record_lag)
    await _loop(node_health, db_session_maker)._run_cycle(db_session)

    db_session.expire_all()
    host = await db_session.get(Host, host_id)
    assert host is not None
    assert host.observation_applied.get(FOLD_SECTION) is None  # watermark not advanced
    record_lag.assert_not_called()


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

    async def flaky(
        db: AsyncSession,
        host_id: uuid.UUID,
        section: dict[str, Any],
        *,
        boot_id: uuid.UUID | None = None,
        deadline: float | None = None,
    ) -> bool:
        if host_id == h2_id:
            raise RuntimeError("boom")
        return await real(db, host_id, section, boot_id=boot_id, deadline=deadline)

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


async def test_terminal_noop_receipt_prevents_peer_replay_after_partial_failure(
    db_session: AsyncSession,
) -> None:
    host, devices = await seed_host_with_devices(db_session, count=2, identity_prefix="fold-partial")
    terminal_device, retryable_device = devices
    terminal_node = AppiumNode(
        device_id=terminal_device.id,
        port=4730,
        pid=1001,
        active_connection_target=terminal_device.connection_target,
        desired_state=AppiumDesiredState.running,
        desired_port=4730,
    )
    retryable_node = AppiumNode(
        device_id=retryable_device.id,
        port=4731,
        pid=1002,
        active_connection_target=retryable_device.connection_target,
        desired_state=AppiumDesiredState.running,
        desired_port=4731,
    )
    db_session.add_all([terminal_node, retryable_node])
    await db_session.commit()
    terminal_device_id = terminal_device.id
    retryable_device_id = retryable_device.id
    terminal_node_id = terminal_node.id
    retryable_node_id = retryable_node.id
    host_id = host.id
    revision = await next_observation_revision(db_session)
    boot_id = uuid.uuid4()
    section = {
        "reported_at": now_utc().isoformat(),
        "section_sequence": 7,
        "observation_revision": revision,
        "nodes": [
            # PID mismatch is terminal for this generation: convergence owns
            # process identity, so it must get a durable receipt.
            _entry(terminal_node.port, 9999, terminal_node.active_connection_target, running=False),
            _entry(
                retryable_node.port,
                retryable_node.pid,
                retryable_node.active_connection_target,
                running=False,
            ),
        ],
    }
    service = _service()
    real_process = service._process_node_health
    calls: list[uuid.UUID] = []

    async def fail_one(
        db: AsyncSession,
        node: AppiumNode,
        device: Device,
        *,
        observation: _NodeObservation,
    ) -> NodeFoldOutcome:
        calls.append(device.id)
        if device.id == retryable_device_id:
            raise RuntimeError("retry this node")
        return await real_process(db, node, device, observation=observation)

    service._process_node_health = fail_one  # type: ignore[method-assign]

    settled = await service.fold_host_nodes(db_session, host_id, section, boot_id=boot_id)

    assert settled is False
    db_session.expire_all()
    terminal_after = await db_session.get(AppiumNode, terminal_node_id)
    retryable_after = await db_session.get(AppiumNode, retryable_node_id)
    assert terminal_after is not None
    assert retryable_after is not None
    assert terminal_after.health_fold_applied_revision == revision
    assert terminal_after.health_fold_boot_id == boot_id
    assert terminal_after.health_fold_section_sequence == 7
    assert retryable_after.health_fold_applied_revision < revision

    assert await service.fold_host_nodes(db_session, host_id, section, boot_id=boot_id) is False
    assert calls.count(terminal_device_id) == 1
    assert calls.count(retryable_device_id) == 2


async def test_node_fold_defers_remaining_devices_at_cycle_deadline(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host, devices = await seed_host_with_devices(db_session, count=2, identity_prefix="fold-budget")
    nodes = [
        AppiumNode(
            device_id=device.id,
            port=4740 + index,
            pid=2000 + index,
            active_connection_target=device.connection_target,
            desired_state=AppiumDesiredState.running,
            desired_port=4740 + index,
        )
        for index, device in enumerate(devices)
    ]
    db_session.add_all(nodes)
    await db_session.commit()
    revision = await next_observation_revision(db_session)
    section = {
        "reported_at": now_utc().isoformat(),
        "observation_revision": revision,
        "nodes": [_entry(node.port, node.pid, node.active_connection_target, running=False) for node in nodes],
    }
    ticks = iter(
        (
            0.0,
            status_fold_module.STATUS_FOLD_CYCLE_BUDGET_SEC + 0.1,
            status_fold_module.STATUS_FOLD_CYCLE_BUDGET_SEC + 0.1,
        )
    )
    monkeypatch.setattr(node_health_module, "perf_counter", lambda: next(ticks))
    service = _service()
    process = AsyncMock(return_value="terminal_noop")
    service._process_node_health = process  # type: ignore[method-assign]

    settled = await service.fold_host_nodes(
        db_session,
        host.id,
        section,
        deadline=status_fold_module.STATUS_FOLD_CYCLE_BUDGET_SEC,
    )

    assert settled is False
    assert process.await_count == 1


async def test_loop_folds_multiple_sections_with_independent_watermarks(
    db_session: AsyncSession, db_session_maker: async_sessionmaker[AsyncSession]
) -> None:
    from app.appium_nodes.services.status_fold_loop import DEVICE_FOLD_SECTION
    from app.hosts.models import HostStatus, OSType

    host_id = uuid.uuid4()
    node_rev, device_rev = 41, 42
    await control_plane_state_store.set_value(
        db_session,
        HOST_STATUS_NAMESPACE,
        str(host_id),
        {
            "received_at": now_utc().isoformat(),
            "payload": {
                FOLD_SECTION: {"reported_at": now_utc().isoformat(), "nodes": [], "observation_revision": node_rev},
                DEVICE_FOLD_SECTION: {"reported_at": now_utc().isoformat(), "observation_revision": device_rev},
            },
        },
    )
    db_session.add(
        Host(
            id=host_id,
            hostname="multi",
            ip="10.0.0.9",
            agent_port=5100,
            os_type=OSType.linux,
            status=HostStatus.online,
        )
    )
    await db_session.commit()

    node_fold = AsyncMock(return_value=True)
    device_fold = AsyncMock(return_value=True)
    loop = StatusFoldLoop(
        sections=(FoldSection(FOLD_SECTION, node_fold), FoldSection(DEVICE_FOLD_SECTION, device_fold)),
        session_factory=db_session_maker,
    )
    await loop._run_cycle(db_session)

    node_fold.assert_awaited_once()
    device_fold.assert_awaited_once()
    db_session.expire_all()
    host = await db_session.get(Host, host_id)
    assert host is not None
    assert host.observation_applied.get(FOLD_SECTION) == node_rev
    assert host.observation_applied.get(DEVICE_FOLD_SECTION) == device_rev


async def test_loop_contains_failure_per_section(
    db_session: AsyncSession, db_session_maker: async_sessionmaker[AsyncSession]
) -> None:
    from app.appium_nodes.services.status_fold_loop import DEVICE_FOLD_SECTION
    from app.hosts.models import HostStatus, OSType

    host_id = uuid.uuid4()
    node_rev, device_rev = 51, 52
    await control_plane_state_store.set_value(
        db_session,
        HOST_STATUS_NAMESPACE,
        str(host_id),
        {
            "received_at": now_utc().isoformat(),
            "payload": {
                FOLD_SECTION: {"reported_at": now_utc().isoformat(), "nodes": [], "observation_revision": node_rev},
                DEVICE_FOLD_SECTION: {
                    "reported_at": now_utc().isoformat(),
                    "devices": [],
                    "observation_revision": device_rev,
                },
            },
        },
    )
    db_session.add(
        Host(
            id=host_id,
            hostname="section-containment",
            ip="10.0.0.10",
            agent_port=5100,
            os_type=OSType.linux,
            status=HostStatus.online,
        )
    )
    await db_session.commit()

    node_fold = AsyncMock(side_effect=RuntimeError("node fold failed"))
    device_fold = AsyncMock(return_value=True)
    loop = StatusFoldLoop(
        sections=(FoldSection(FOLD_SECTION, node_fold), FoldSection(DEVICE_FOLD_SECTION, device_fold)),
        session_factory=db_session_maker,
    )

    await loop._run_cycle(db_session)

    node_fold.assert_awaited_once()
    device_fold.assert_awaited_once()
    db_session.expire_all()
    host = await db_session.get(Host, host_id)
    assert host is not None
    assert host.observation_applied.get(FOLD_SECTION) is None
    assert host.observation_applied.get(DEVICE_FOLD_SECTION) == device_rev


async def test_loop_records_oldest_unapplied_section_age(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import timedelta

    from app.appium_nodes.services.status_fold_loop import DEVICE_FOLD_SECTION

    older = now_utc() - timedelta(seconds=20)
    newer = now_utc() - timedelta(seconds=5)
    snapshots = {
        str(uuid.uuid4()): {
            "received_at": older.isoformat(),
            "payload": {FOLD_SECTION: {"reported_at": older.isoformat(), "nodes": [], "observation_revision": 3}},
        },
        str(uuid.uuid4()): {
            "received_at": newer.isoformat(),
            "payload": {DEVICE_FOLD_SECTION: {"reported_at": newer.isoformat(), "observation_revision": 4}},
        },
    }
    monkeypatch.setattr(control_plane_state_store, "get_values", AsyncMock(return_value=snapshots))
    recorded: list[float] = []
    monkeypatch.setattr(status_fold_module, "record_status_fold_oldest_unapplied", recorded.append)
    loop = StatusFoldLoop(
        sections=(
            FoldSection(FOLD_SECTION, AsyncMock(return_value=True)),
            FoldSection(DEVICE_FOLD_SECTION, AsyncMock(return_value=True)),
        ),
        session_factory=db_session_maker,
    )
    loop._load_applied = AsyncMock(return_value={})  # type: ignore[method-assign]
    loop._fold_host = AsyncMock()  # type: ignore[method-assign]
    await loop._run_cycle(db_session)

    assert recorded, "oldest-unapplied gauge was not recorded"
    assert recorded[0] >= 20.0  # the 20s-old pending section dominates


def test_production_status_fold_loop_registers_both_health_sections() -> None:
    """main.py wires the StatusFoldLoop with both the node_health and device_health
    folds, so both axes reconcile off the request path."""
    import ast
    from pathlib import Path

    main_py = Path(__file__).resolve().parents[2] / "app" / "main.py"
    tree = ast.parse(main_py.read_text(encoding="utf-8"))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "StatusFoldLoop"
    ]
    assert len(calls) == 1
    sections_kw = next(kw for kw in calls[0].keywords if kw.arg == "sections")
    assert isinstance(sections_kw.value, ast.Tuple)
    section_consts = {
        entry.args[0].id
        for entry in sections_kw.value.elts
        if isinstance(entry, ast.Call) and entry.args and isinstance(entry.args[0], ast.Name)
    }
    assert {"FOLD_SECTION", "DEVICE_FOLD_SECTION"} <= section_consts
