"""Desired-state convergence algorithm tests."""

from __future__ import annotations

import contextlib
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import delete, event, select, text
from sqlalchemy.exc import DBAPIError

from app.appium_nodes.models import AppiumDesiredState, AppiumNode
from app.appium_nodes.services import reconciler as appium_reconciler
from app.appium_nodes.services.reconciler import ReconcilerService
from app.appium_nodes.services.reconciler_agent import NodeStartDetails
from app.appium_nodes.services.reconciler_convergence import (
    DesiredRow,
    ObservedEntry,
    _execute_action,
    decide_convergence_action,
    match_observed_entry,
)
from app.devices import locking as device_locking
from app.devices.models import Device, DeviceOperationalState
from app.events.models import SystemEvent
from tests.bench_instrumentation import QueryTap
from tests.fakes import FakeSettingsReader
from tests.helpers import create_device
from tests.helpers import test_event_bus as event_bus

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.hosts.models import Host


async def converge_host_rows(
    *,
    host_id: uuid.UUID,
    rows: list[DesiredRow],
    agent_running: list[ObservedEntry],
    now: datetime,
    write_observed: object,
    reset_start_failure: object,
    raise_errors: bool = False,
) -> None:
    """Test-local re-implementation of the deleted free function, using the same logic."""
    observed_by_target = {entry.connection_target: entry for entry in agent_running}
    observed_by_port = {entry.port: entry for entry in agent_running}
    for row in sorted(rows, key=lambda r: str(r.device_id)):
        obs = match_observed_entry(row, observed_by_target, observed_by_port)
        action = decide_convergence_action(row, observed=obs, now=now)
        try:
            await _execute_action(
                host_id=host_id,
                row=row,
                action=action,
                write_observed=write_observed,  # type: ignore[arg-type]
                reset_start_failure=reset_start_failure,  # type: ignore[arg-type]
            )
        except Exception:
            if raise_errors:
                raise


def _row(**kw: object) -> DesiredRow:
    defaults: dict[str, object] = {
        "device_id": uuid.uuid4(),
        "host_id": uuid.uuid4(),
        "node_id": uuid.uuid4(),
        "connection_target": "emulator-5554",
        "desired_state": "stopped",
        "desired_port": None,
        "port": None,
        "pid": None,
        "active_connection_target": None,
        "stop_pending": False,
    }
    defaults.update(kw)
    return DesiredRow(**defaults)  # type: ignore[arg-type]


def test_desired_running_no_token_no_observed_picks_start() -> None:
    row = _row(desired_state="running", desired_port=4723)
    action = decide_convergence_action(row, observed=None, now=datetime.now(UTC))
    assert action.kind == "start"
    assert action.port == 4723


def test_desired_running_no_token_observed_matching_picks_confirm_running() -> None:
    row = _row(
        desired_state="running",
        desired_port=4723,
        port=4723,
        pid=12345,
        active_connection_target="emulator-5554",
    )
    obs = ObservedEntry(port=4723, pid=12345, connection_target=row.connection_target)
    action = decide_convergence_action(row, observed=obs, now=datetime.now(UTC))
    assert action.kind == "confirm_running"


def test_desired_running_observed_but_db_lacks_pid_repairs_observed_state() -> None:
    row = _row(desired_state="running", desired_port=4723, port=4723)
    obs = ObservedEntry(port=4723, pid=12345, connection_target=row.connection_target)
    action = decide_convergence_action(row, observed=obs, now=datetime.now(UTC))
    assert action.kind == "db_mark_running"
    assert action.port == 4723
    assert action.pid == 12345
    assert action.active_connection_target == row.connection_target


def test_desired_running_observed_with_new_spawn_time_repairs_observed_state() -> None:
    old_spawn = datetime(2026, 7, 9, 14, 0, tzinfo=UTC)
    new_spawn = datetime(2026, 7, 9, 15, 0, tzinfo=UTC)
    row = _row(
        desired_state="running",
        desired_port=4723,
        port=4723,
        pid=12345,
        active_connection_target="emulator-5554",
        started_at=old_spawn,
    )
    obs = ObservedEntry(port=4723, pid=12345, connection_target=row.connection_target, started_at=new_spawn)

    action = decide_convergence_action(row, observed=obs, now=datetime.now(UTC))

    assert action.kind == "db_mark_running"
    assert action.started_at == new_spawn


def test_desired_running_observed_with_new_pack_release_repairs_observed_state() -> None:
    row = _row(
        desired_state="running",
        desired_port=4723,
        port=4723,
        pid=12345,
        active_connection_target="emulator-5554",
        observed_pack_release="2026.07.1",
    )
    obs = ObservedEntry(
        port=4723,
        pid=12345,
        connection_target=row.connection_target,
        pack_release="2026.07.2",
    )

    action = decide_convergence_action(row, observed=obs, now=datetime.now(UTC))

    assert action.kind == "db_mark_running"
    assert action.pack_release == "2026.07.2"


def test_desired_running_no_token_observed_port_mismatch_picks_stop_then_retry() -> None:
    row = _row(desired_state="running", desired_port=4723)
    obs = ObservedEntry(port=4999, pid=12345, connection_target=row.connection_target)
    action = decide_convergence_action(row, observed=obs, now=datetime.now(UTC))
    assert action.kind == "stop"
    assert action.port == 4999
    assert action.clear_desired_port is True


def test_orphaned_node_ports_flags_duplicates_and_unknown_targets() -> None:
    """Stray agent nodes the per-row loop cannot reach must be flagged for stop.

    The loop matches one observed entry per connection_target (last-wins), so a
    second node for the same target is left untracked; and a node for a target
    with no device on the host is never iterated at all. Both linger as orphans
    that the backend health-checks against the wrong port, flapping the device.
    """
    from app.appium_nodes.services.reconciler_convergence import orphaned_node_ports

    observed = [
        ObservedEntry(port=4723, pid=1, connection_target="dev-A"),
        ObservedEntry(port=4724, pid=2, connection_target="dev-A"),  # duplicate of dev-A
        ObservedEntry(port=4725, pid=3, connection_target="ghost"),  # no device on this host
    ]
    # last-wins primary for dev-A is 4724, so 4723 is the duplicate orphan.
    assert sorted(orphaned_node_ports(observed, known_targets={"dev-A", "dev-B"})) == [4723, 4725]


def test_orphaned_node_ports_empty_when_each_known_target_has_one_node() -> None:
    """A single node per known target is never an orphan — even a device in
    backoff (excluded from active convergence) is a *known* target and its node
    must not be reaped."""
    from app.appium_nodes.services.reconciler_convergence import orphaned_node_ports

    observed = [
        ObservedEntry(port=4723, pid=1, connection_target="dev-A"),
        ObservedEntry(port=4724, pid=2, connection_target="dev-B-in-backoff"),
    ]
    assert orphaned_node_ports(observed, known_targets={"dev-A", "dev-B-in-backoff"}) == []


def test_rows_needing_stale_clear_selects_only_db_clear_action() -> None:
    """Backoff devices (excluded from active convergence) must get only the
    DB-only stale-pid clear — never an agent start/stop, which is recovery's job."""
    from app.appium_nodes.services.reconciler_convergence import rows_needing_stale_clear

    now = datetime.now(UTC)
    stale = _row(connection_target="dev-A", desired_state="stopped", pid=999, active_connection_target="dev-A")
    clean = _row(connection_target="dev-B", desired_state="stopped")  # no pid -> no_op
    running_no_obs = _row(connection_target="dev-C", desired_state="running", desired_port=4723)  # -> start, skip
    result = rows_needing_stale_clear([stale, clean, running_no_obs], [], now=now)
    assert [r.connection_target for r in result] == ["dev-A"]


def test_rows_needing_stale_clear_skips_when_node_observed_running() -> None:
    """If the agent still reports the node, it's not stale — leave it to recovery."""
    from app.appium_nodes.services.reconciler_convergence import rows_needing_stale_clear

    now = datetime.now(UTC)
    row = _row(connection_target="dev-A", desired_state="stopped", pid=999, active_connection_target="dev-A")
    observed = [ObservedEntry(port=4723, pid=999, connection_target="dev-A")]
    assert rows_needing_stale_clear([row], observed, now=now) == []


def test_match_observed_entry_prefers_active_target_then_registered() -> None:
    """A row's node may be reported under its live target (virtual emulators
    report their ADB serial, not the registered AVD name) — match by the row's
    ``active_connection_target`` first, then the registered target."""
    by_serial = ObservedEntry(port=4724, pid=1, connection_target="emulator-5554")
    by_registered = ObservedEntry(port=4725, pid=2, connection_target="Television_1080p")

    emulator = _row(connection_target="Television_1080p", active_connection_target="emulator-5554")
    assert match_observed_entry(emulator, {"emulator-5554": by_serial}) is by_serial
    # Stale active target: fall back to the registered target.
    assert match_observed_entry(emulator, {"Television_1080p": by_registered}) is by_registered
    assert match_observed_entry(emulator, {}) is None

    real = _row(connection_target="192.168.1.254:5555", active_connection_target=None)
    entry = ObservedEntry(port=4723, pid=3, connection_target="192.168.1.254:5555")
    assert match_observed_entry(real, {"192.168.1.254:5555": entry}) is entry


def test_match_observed_entry_falls_back_to_port_when_target_unmatched() -> None:
    """An emulator reports its node under the live ADB serial, not the registered
    AVD name, so target matching misses when ``active_connection_target`` is unset
    (e.g. cleared during recovery backoff). The node's port is its stable identity —
    match on it so the observed pid/target fold instead of stranding the node with
    ``observed_running`` False forever."""
    by_serial = ObservedEntry(port=4728, pid=13247, connection_target="emulator-5554")
    observed_by_port = {by_serial.port: by_serial}

    emulator = _row(connection_target="Pixel_6", active_connection_target=None, port=4728)
    # Target maps (keyed by AVD name) miss; the port fallback catches it.
    assert match_observed_entry(emulator, {}, observed_by_port) is by_serial
    # Without the port index (legacy 2-arg call), behaviour is unchanged: no match.
    assert match_observed_entry(emulator, {}) is None
    # A registered-target match still wins over the port fallback.
    by_registered = ObservedEntry(port=4728, pid=1, connection_target="Pixel_6")
    assert match_observed_entry(emulator, {"Pixel_6": by_registered}, observed_by_port) is by_registered


def test_desired_running_emulator_folds_pid_via_port_match() -> None:
    """End-to-end of the fold: a desired-running emulator row whose observation is
    keyed by the ADB serial folds pid + active_connection_target via the port match."""
    row = _row(desired_state="running", desired_port=4728, port=4728, connection_target="Pixel_6")
    obs = ObservedEntry(port=4728, pid=13247, connection_target="emulator-5554")
    matched = match_observed_entry(row, {"emulator-5554": obs}, {4728: obs})
    action = decide_convergence_action(row, observed=matched, now=datetime.now(UTC))
    assert action.kind == "db_mark_running"
    assert action.pid == 13247
    assert action.active_connection_target == "emulator-5554"


def test_rows_needing_stale_clear_skips_node_observed_only_by_port() -> None:
    """A backed-off emulator whose node the agent still reports (matched by port,
    not target) must NOT be stale-cleared — clearing its pid/active_connection_target
    is what re-strands the node. Leave it to recovery."""
    from app.appium_nodes.services.reconciler_convergence import rows_needing_stale_clear

    now = datetime.now(UTC)
    row = _row(
        connection_target="Pixel_6",
        desired_state="stopped",
        port=4728,
        pid=999,
        active_connection_target=None,
    )
    observed = [ObservedEntry(port=4728, pid=13247, connection_target="emulator-5554")]
    assert rows_needing_stale_clear([row], observed, now=now) == []


def test_rows_needing_stale_clear_matches_node_by_active_connection_target() -> None:
    """A live emulator node reported under its ADB serial is not a stale pid —
    clearing it would desync the DB row from a node that is actually running."""
    from app.appium_nodes.services.reconciler_convergence import rows_needing_stale_clear

    now = datetime.now(UTC)
    row = _row(
        connection_target="Television_1080p",
        desired_state="stopped",
        pid=999,
        active_connection_target="emulator-5554",
    )
    observed = [ObservedEntry(port=4724, pid=999, connection_target="emulator-5554")]
    assert rows_needing_stale_clear([row], observed, now=now) == []


def test_desired_stopped_with_observed_picks_stop() -> None:
    row = _row(desired_state="stopped", port=4723, pid=1, active_connection_target="emulator-5554")
    obs = ObservedEntry(port=4723, pid=1, connection_target=row.connection_target)
    action = decide_convergence_action(row, observed=obs, now=datetime.now(UTC))
    assert action.kind == "stop"
    assert action.port == 4723


def test_desired_stopped_with_stop_pending_keeps_observed_node_for_agent_drain() -> None:
    row = _row(
        desired_state="stopped",
        port=4723,
        pid=1,
        active_connection_target="emulator-5554",
        stop_pending=True,
    )
    obs = ObservedEntry(port=4723, pid=1, connection_target=row.connection_target)

    action = decide_convergence_action(row, observed=obs, now=datetime.now(UTC))

    assert action.kind == "no_op"


def test_desired_stopped_no_observed_picks_noop_or_db_clear() -> None:
    row = _row(desired_state="stopped")
    action = decide_convergence_action(row, observed=None, now=datetime.now(UTC))
    assert action.kind == "no_op"


def test_desired_stopped_no_observed_but_db_says_running_picks_db_clear() -> None:
    row = _row(desired_state="stopped", port=4723, pid=1, active_connection_target="emulator-5554")
    action = decide_convergence_action(row, observed=None, now=datetime.now(UTC))
    assert action.kind == "db_clear_stale_running"


@pytest.mark.asyncio
async def test_converge_host_rows_resets_start_failure_when_observed_matches_db() -> None:
    # Row carries reconciler failure residue: reset must be called.
    row = _row(
        desired_state="running",
        desired_port=4723,
        port=4723,
        pid=12345,
        active_connection_target="emulator-5554",
        reconciler_failure_present=True,
    )
    observed = ObservedEntry(port=4723, pid=12345, connection_target=row.connection_target)
    reset_start_failure = AsyncMock()
    write_observed = AsyncMock()

    await converge_host_rows(
        host_id=row.host_id,
        rows=[row],
        agent_running=[observed],
        now=datetime.now(UTC),
        write_observed=write_observed,
        reset_start_failure=reset_start_failure,
    )

    reset_start_failure.assert_awaited_once_with(row=row)
    write_observed.assert_not_awaited()


@pytest.mark.asyncio
async def test_converge_host_rows_confirm_running_skips_reset_when_no_residue() -> None:
    # Row with no failure residue: confirm_running must NOT call reset_start_failure.
    row = _row(
        desired_state="running",
        desired_port=4723,
        port=4723,
        pid=12345,
        active_connection_target="emulator-5554",
        lifecycle_policy_state={},
    )
    observed = ObservedEntry(port=4723, pid=12345, connection_target=row.connection_target)
    reset_start_failure = AsyncMock()

    await converge_host_rows(
        host_id=row.host_id,
        rows=[row],
        agent_running=[observed],
        now=datetime.now(UTC),
        write_observed=AsyncMock(),
        reset_start_failure=reset_start_failure,
    )

    reset_start_failure.assert_not_awaited()


@pytest.mark.asyncio
async def test_converge_host_rows_repairs_observed_running_db_missing_pid() -> None:
    row = _row(desired_state="running", desired_port=4723, port=4723)
    observed = ObservedEntry(port=4723, pid=12345, connection_target=row.connection_target)
    write_observed = AsyncMock()

    await converge_host_rows(
        host_id=row.host_id,
        rows=[row],
        agent_running=[observed],
        now=datetime.now(UTC),
        write_observed=write_observed,
        reset_start_failure=AsyncMock(),
    )

    write_observed.assert_awaited_once_with(
        row=row,
        state="running",
        port=4723,
        pid=12345,
        details=NodeStartDetails(
            started_at=None,
            pack_release=None,
            active_connection_target=row.connection_target,
        ),
    )


@pytest.mark.asyncio
async def test_converge_host_rows_db_clear_branch() -> None:
    stale = _row(
        desired_state="stopped",
        connection_target="stale",
        port=4724,
        pid=2,
        active_connection_target="stale",
    )
    write_observed = AsyncMock()

    await converge_host_rows(
        host_id=stale.host_id,
        rows=[stale],
        agent_running=[],
        now=datetime.now(UTC),
        write_observed=write_observed,
        reset_start_failure=AsyncMock(),
    )

    write_observed.assert_awaited_once_with(
        row=stale,
        state="stopped",
        port=None,
        pid=None,
        details=NodeStartDetails(),
    )


@pytest.mark.asyncio
async def test_converge_host_rows_noop_and_raise_errors_branch() -> None:
    noop = _row(desired_state="stopped")
    await converge_host_rows(
        host_id=noop.host_id,
        rows=[noop],
        agent_running=[],
        now=datetime.now(UTC),
        write_observed=AsyncMock(),
        reset_start_failure=AsyncMock(),
    )

    # db_mark_running (a DB-only action) still surfaces its write_observed
    # failure when raise_errors=True — the loop only swallows by default.
    failing = _row(desired_state="running", desired_port=4723)
    observed = ObservedEntry(port=4723, pid=12345, connection_target=failing.connection_target)
    with pytest.raises(RuntimeError, match="write failed"):
        await converge_host_rows(
            host_id=failing.host_id,
            rows=[failing],
            agent_running=[observed],
            now=datetime.now(UTC),
            write_observed=AsyncMock(side_effect=RuntimeError("write failed")),
            reset_start_failure=AsyncMock(),
            raise_errors=True,
        )


# ---------------------------------------------------------------------------
# Boundary regressions: one aggregate Device lock per observation, peers locked
# in ascending UUID order, and per-device failure isolation.
# ---------------------------------------------------------------------------


class _OrderedQueryTap(QueryTap):
    """Engine-scoped ``QueryTap`` that also keeps statements in execution order.

    ``tests/concurrency/group_lock_helpers.py::capture_statements`` pins its
    listener to a single session's connection, and ``apply_observed_node_command``
    opens its own session from the factory — that helper records none of these
    statements and every assertion below would pass vacuously.
    """

    def __init__(self) -> None:
        super().__init__()
        self.statements: list[str] = []

    def __call__(
        self,
        conn: object,
        cursor: object,
        statement: str,
        parameters: object = None,
        context: object = None,
        executemany: bool = False,
    ) -> None:
        super().__call__(conn, cursor, statement, parameters, context, executemany)
        if self.armed:
            self.statements.append(statement)


@asynccontextmanager
async def _capture_engine_statements(db_session: AsyncSession) -> AsyncIterator[_OrderedQueryTap]:
    tap = _OrderedQueryTap()
    engine = db_session.bind.sync_engine  # type: ignore[union-attr]
    event.listen(engine, "before_cursor_execute", tap)
    try:
        yield tap
    finally:
        event.remove(engine, "before_cursor_execute", tap)


async def _seed_device_with_node(
    db: AsyncSession, host_id: uuid.UUID, *, name: str, port: int
) -> tuple[Device, AppiumNode]:
    device = await create_device(
        db,
        host_id=host_id,
        name=name,
        identity_value=name,
        connection_target=name,
        operational_state=DeviceOperationalState.available,
    )
    node = AppiumNode(
        device_id=device.id,
        port=port,
        desired_state=AppiumDesiredState.running,
        desired_port=port,
    )
    db.add(node)
    await db.flush()
    return device, node


def _running_row(device: Device, node: AppiumNode, *, host_id: uuid.UUID) -> DesiredRow:
    return DesiredRow(
        device_id=device.id,
        host_id=host_id,
        node_id=node.id,
        connection_target=device.connection_target or device.identity_value,
        desired_state="running",
        desired_port=node.port,
        port=node.port,
        pid=None,
        active_connection_target=None,
        stop_pending=False,
    )


def _reconciler(session_factory: async_sessionmaker[AsyncSession]) -> ReconcilerService:
    return ReconcilerService(
        publisher=event_bus,
        settings=FakeSettingsReader({}),
        pool=Mock(),
        circuit_breaker=Mock(),
        session_factory=session_factory,
    )


@pytest.mark.usefixtures("seeded_driver_packs")
async def test_observed_running_takes_one_device_lock_before_the_node(
    db_session_maker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    """One observation settles under exactly one Device lock, taken before the
    AppiumNode lock. The pre-boundary shape re-locked Device inside the health
    writer after ``mark_node_started`` had already locked it."""
    device, node = await _seed_device_with_node(db_session, db_host.id, name="obs-lock-single", port=4723)
    await db_session.commit()

    write_observed = _reconciler(db_session_maker)._write_observed_factory()
    async with _capture_engine_statements(db_session) as tap:
        await write_observed(
            row=_running_row(device, node, host_id=db_host.id),
            state="running",
            port=4723,
            pid=999,
            details=NodeStartDetails(active_connection_target=device.connection_target),
        )

    statements = tap.statements
    device_locks = [sql for sql in statements if "FROM devices" in sql and "FOR UPDATE" in sql]
    node_locks = [sql for sql in statements if "FROM appium_nodes" in sql and "FOR UPDATE" in sql]
    assert len(device_locks) == 1
    assert len(node_locks) <= 1
    if node_locks:
        assert statements.index(device_locks[0]) < statements.index(node_locks[0])

    async with db_session_maker() as verify:
        stored = (await verify.execute(select(AppiumNode).where(AppiumNode.device_id == device.id))).scalar_one()
        assert stored.pid == 999


@pytest.mark.usefixtures("seeded_driver_packs")
async def test_peer_observations_lock_devices_in_ascending_uuid_order(
    db_session_maker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Three desired rows handed over in reverse UUID order settle in ascending
    UUID order, each under exactly one ``lock_device_handle``."""
    seeded = [
        await _seed_device_with_node(db_session, db_host.id, name=f"obs-order-{index}", port=4730 + index)
        for index in range(3)
    ]
    await db_session.commit()

    rows = [_running_row(device, node, host_id=db_host.id) for device, node in seeded]
    ordered_ids = sorted((row.device_id for row in rows), key=str)

    handle_locks: list[uuid.UUID] = []
    plain_locks: list[uuid.UUID] = []
    real_lock_device_handle = device_locking.lock_device_handle
    real_lock_device = device_locking.lock_device

    async def recording_lock_device_handle(db: AsyncSession, device_id: uuid.UUID, **kwargs: object) -> object:
        handle_locks.append(device_id)
        return await real_lock_device_handle(db, device_id, **kwargs)  # type: ignore[arg-type]

    async def recording_lock_device(db: AsyncSession, device_id: uuid.UUID, **kwargs: object) -> object:
        plain_locks.append(device_id)
        return await real_lock_device(db, device_id, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(device_locking, "lock_device_handle", recording_lock_device_handle)
    monkeypatch.setattr(device_locking, "lock_device", recording_lock_device)

    payload = {
        "appium_processes": {
            "running_nodes": [
                {
                    "port": node.port,
                    "pid": 5000 + node.port,
                    "connection_target": device.connection_target,
                    "platform_id": "android_mobile",
                }
                for device, node in seeded
            ]
        }
    }

    await _reconciler(db_session_maker).reconcile_host(
        host_id=db_host.id,
        host_ip=db_host.ip,
        agent_port=db_host.agent_port,
        rows=sorted(rows, key=lambda item: str(item.device_id), reverse=True),
        backoff_until_by_device={},
        payload=payload,
    )

    assert handle_locks == ordered_ids
    assert plain_locks == []

    async with db_session_maker() as verify:
        stored = {
            row.device_id: row
            for row in (await verify.execute(select(AppiumNode).where(AppiumNode.device_id.in_(ordered_ids)))).scalars()
        }
    assert [stored[device_id].pid for device_id in ordered_ids] == [5000 + stored[d].port for d in ordered_ids]


@pytest.mark.usefixtures("seeded_driver_packs")
async def test_failed_observation_rolls_back_only_its_own_device(
    db_session_maker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A real aborted transaction in the middle command leaves the first and third
    observed facts and their events durable, and the middle device unchanged."""
    seeded = [
        await _seed_device_with_node(db_session, db_host.id, name=f"obs-peer-{index}", port=4750 + index)
        for index in range(3)
    ]
    await db_session.commit()

    rows = sorted(
        (_running_row(device, node, host_id=db_host.id) for device, node in seeded),
        key=lambda item: str(item.device_id),
    )

    started_calls = 0
    real_mark_node_started = appium_reconciler.mark_node_started

    async def failing_middle_command(db: AsyncSession, *args: object, **kwargs: object) -> object:
        nonlocal started_calls
        started_calls += 1
        result = await real_mark_node_started(db, *args, **kwargs)  # type: ignore[arg-type]
        if started_calls == 2:
            # A real aborted PostgreSQL transaction: a mocked side_effect would
            # leave the session clean and exercise a different code path.
            await db.execute(text("SELECT 1 / 0"))
        return result

    monkeypatch.setattr(appium_reconciler, "mark_node_started", failing_middle_command)

    write_observed = _reconciler(db_session_maker)._write_observed_factory()
    for row in rows:
        # The convergence loop swallows one row's failure and continues.
        with contextlib.suppress(DBAPIError):
            await write_observed(
                row=row,
                state="running",
                port=row.port,
                pid=5000 + (row.port or 0),
                details=NodeStartDetails(active_connection_target=row.connection_target),
            )

    ordered_ids = [row.device_id for row in rows]
    rolled_back_id = ordered_ids[1]
    async with db_session_maker() as verify:
        stored = {
            row.device_id: row
            for row in (await verify.execute(select(AppiumNode).where(AppiumNode.device_id.in_(ordered_ids)))).scalars()
        }
        announced = {
            row.data["device_id"]
            for row in (
                await verify.execute(select(SystemEvent).where(SystemEvent.type == "node.state_changed"))
            ).scalars()
        }

    assert stored[ordered_ids[0]].pid == 5000 + stored[ordered_ids[0]].port
    assert stored[ordered_ids[2]].pid == 5000 + stored[ordered_ids[2]].port
    assert stored[rolled_back_id].pid is None
    assert {str(ordered_ids[0]), str(ordered_ids[2])} <= announced
    assert str(rolled_back_id) not in announced


@pytest.mark.usefixtures("seeded_driver_packs")
async def test_observation_command_treats_a_deleted_device_as_a_noop(
    db_session_maker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    """A device deleted after the desired-row inventory is a logged no-op, not a
    leaked ``NoResultFound``."""
    device, node = await _seed_device_with_node(db_session, db_host.id, name="obs-deleted", port=4740)
    await db_session.commit()
    row = _running_row(device, node, host_id=db_host.id)

    async with db_session_maker() as remover:
        await remover.execute(delete(Device).where(Device.id == device.id))
        await remover.commit()

    write_observed = _reconciler(db_session_maker)._write_observed_factory()
    await write_observed(row=row, state="running", port=4740, pid=1, details=NodeStartDetails())
    await write_observed(row=row, state="stopped", port=None, pid=None, details=NodeStartDetails())

    async with db_session_maker() as verify:
        assert (
            await verify.execute(select(AppiumNode).where(AppiumNode.device_id == row.device_id))
        ).scalar_one_or_none() is None
