"""Desired-state convergence algorithm tests."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from app.appium_nodes.exceptions import NodeAlreadyRunningError
from app.appium_nodes.services.reconciler_convergence import (
    ConvergenceAction,
    DesiredRow,
    ObservedEntry,
    _execute_action,
    decide_convergence_action,
    match_observed_entry,
    reap_orphan_nodes,
)


async def converge_host_rows(
    *,
    host_id: uuid.UUID,
    rows: list[DesiredRow],
    agent_running: list[ObservedEntry],
    now: datetime,
    start_agent: object,
    stop_agent: object,
    write_observed: object,
    clear_token: object,
    reset_start_failure: object,
    raise_errors: bool = False,
) -> None:
    """Test-local re-implementation of the deleted free function, using the same logic."""
    observed_by_target = {entry.connection_target: entry for entry in agent_running}
    for row in sorted(rows, key=lambda r: str(r.device_id)):
        obs = match_observed_entry(row, observed_by_target)
        action = decide_convergence_action(row, observed=obs, now=now)
        try:
            await _execute_action(
                host_id=host_id,
                row=row,
                action=action,
                start_agent=start_agent,  # type: ignore[arg-type]
                stop_agent=stop_agent,  # type: ignore[arg-type]
                write_observed=write_observed,  # type: ignore[arg-type]
                clear_token=clear_token,  # type: ignore[arg-type]
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
        "transition_token": None,
        "transition_deadline": None,
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


def test_desired_running_no_token_observed_port_mismatch_picks_stop_then_retry() -> None:
    row = _row(desired_state="running", desired_port=4723)
    obs = ObservedEntry(port=4999, pid=12345, connection_target=row.connection_target)
    action = decide_convergence_action(row, observed=obs, now=datetime.now(UTC))
    assert action.kind == "stop"
    assert action.port == 4999
    assert action.clear_desired_port is True


@pytest.mark.parametrize("kind", ["start", "restart"])
@pytest.mark.asyncio
async def test_execute_action_treats_already_running_as_benign_noop(kind: str) -> None:
    """When the agent reports the target already running, the start leg of a
    start/restart action must be a no-op: no ``write_observed`` push, no raise.
    The next observation tick records the running node via ``db_mark_running``."""
    row = _row(desired_state="running", desired_port=4723, port=4724, pid=1)
    action = ConvergenceAction(kind=kind, port=4723, stop_port=4724, start_port=4723)

    start_agent = AsyncMock(side_effect=NodeAlreadyRunningError("already running for target on port 4724"))
    stop_agent = AsyncMock()
    write_observed = AsyncMock()

    # Must not raise.
    await _execute_action(
        host_id=uuid.uuid4(),
        row=row,
        action=action,
        start_agent=start_agent,  # type: ignore[arg-type]
        stop_agent=stop_agent,  # type: ignore[arg-type]
        write_observed=write_observed,  # type: ignore[arg-type]
        clear_token=AsyncMock(),  # type: ignore[arg-type]
        reset_start_failure=AsyncMock(),  # type: ignore[arg-type]
    )

    start_agent.assert_awaited_once()
    write_observed.assert_not_awaited()


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


async def test_reap_orphan_nodes_keeps_node_reported_under_active_connection_target() -> None:
    """A virtual device's node reports its live ADB serial (the row's
    ``active_connection_target``), not the registered AVD target — it must count
    as a known target. Reaping it puts the reconciler in a permanent 30s
    stop/start loop for the device."""
    from app.appium_nodes.services.reconciler_convergence import reap_orphan_nodes

    observed = [ObservedEntry(port=4724, pid=1, connection_target="emulator-5554")]
    rows = [
        _row(
            connection_target="Television_1080p",
            active_connection_target="emulator-5554",
            desired_state="running",
            desired_port=4724,
        )
    ]
    stop_agent = AsyncMock()

    reaped = await reap_orphan_nodes(observed, rows, stop_agent=stop_agent)

    assert reaped == []
    stop_agent.assert_not_awaited()


async def test_reap_orphan_nodes_stops_each_orphan_port() -> None:
    from app.appium_nodes.services.reconciler_convergence import reap_orphan_nodes

    observed = [
        ObservedEntry(port=4723, pid=1, connection_target="dev-A"),
        ObservedEntry(port=4724, pid=2, connection_target="dev-A"),  # duplicate
        ObservedEntry(port=4725, pid=3, connection_target="ghost"),  # unknown
    ]
    rows = [_row(connection_target="dev-A", desired_state="running")]
    stop_agent = AsyncMock()

    reaped = await reap_orphan_nodes(observed, rows, stop_agent=stop_agent)

    assert sorted(reaped) == [4723, 4725]
    stopped_ports = sorted(call.kwargs["port"] for call in stop_agent.await_args_list)
    assert stopped_ports == [4723, 4725]
    # Orphans carry no desired row.
    assert all(call.kwargs["row"] is None for call in stop_agent.await_args_list)


async def test_reap_orphan_nodes_swallows_stop_errors() -> None:
    """A failing stop for one orphan must not abort the host cycle."""
    from app.appium_nodes.services.reconciler_convergence import reap_orphan_nodes

    observed = [ObservedEntry(port=4999, pid=9, connection_target="ghost")]
    stop_agent = AsyncMock(side_effect=RuntimeError("agent unreachable"))

    reaped = await reap_orphan_nodes(observed, [], stop_agent=stop_agent)

    assert reaped == [4999]
    stop_agent.assert_awaited_once()


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


def test_desired_running_active_token_picks_restart() -> None:
    row = _row(
        desired_state="running",
        desired_port=4723,
        transition_token=uuid.uuid4(),
        transition_deadline=datetime.now(UTC) + timedelta(seconds=60),
        port=4723,
    )
    obs = ObservedEntry(port=4723, pid=12345, connection_target=row.connection_target)
    action = decide_convergence_action(row, observed=obs, now=datetime.now(UTC))
    assert action.kind == "restart"
    assert action.stop_port == 4723
    assert action.start_port == 4723


def test_desired_running_active_token_without_observation_restarts_without_stop() -> None:
    row = _row(
        desired_state="running",
        desired_port=4724,
        transition_token=uuid.uuid4(),
        transition_deadline=datetime.now(UTC) + timedelta(seconds=60),
        port=4723,
    )
    action = decide_convergence_action(row, observed=None, now=datetime.now(UTC))
    assert action.kind == "restart"
    assert action.stop_port is None
    assert action.start_port == 4724


def test_desired_running_expired_token_picks_clear_then_running_no_token() -> None:
    row = _row(
        desired_state="running",
        desired_port=4723,
        transition_token=uuid.uuid4(),
        transition_deadline=datetime.now(UTC) - timedelta(seconds=1),
        port=4723,
        pid=1,
        active_connection_target="emulator-5554",
    )
    obs = ObservedEntry(port=4723, pid=1, connection_target=row.connection_target)
    action = decide_convergence_action(row, observed=obs, now=datetime.now(UTC))
    assert action.kind == "clear_expired_token"


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
async def test_converge_host_rows_calls_start_for_running_intent_no_observation() -> None:
    row = _row(desired_state="running", desired_port=4723)
    start_agent = AsyncMock(return_value={"pid": 1234, "port": 4723})
    stop_agent = AsyncMock()
    write_observed = AsyncMock()

    await converge_host_rows(
        host_id=row.host_id,
        rows=[row],
        agent_running=[],
        now=datetime.now(UTC),
        start_agent=start_agent,
        stop_agent=stop_agent,
        write_observed=write_observed,
        clear_token=AsyncMock(),
        reset_start_failure=AsyncMock(),
    )

    start_agent.assert_awaited_once()
    stop_agent.assert_not_awaited()
    write_observed.assert_awaited_once()


@pytest.mark.asyncio
async def test_converge_host_rows_resets_start_failure_when_observed_matches_db() -> None:
    # Row carries reconciler failure residue: reset must be called.
    row = _row(
        desired_state="running",
        desired_port=4723,
        port=4723,
        pid=12345,
        active_connection_target="emulator-5554",
        lifecycle_policy_state={"last_failure_source": "appium_reconciler", "last_failure_reason": "timeout"},
    )
    observed = ObservedEntry(port=4723, pid=12345, connection_target=row.connection_target)
    reset_start_failure = AsyncMock()
    start_agent = AsyncMock()
    stop_agent = AsyncMock()
    write_observed = AsyncMock()

    await converge_host_rows(
        host_id=row.host_id,
        rows=[row],
        agent_running=[observed],
        now=datetime.now(UTC),
        start_agent=start_agent,
        stop_agent=stop_agent,
        write_observed=write_observed,
        clear_token=AsyncMock(),
        reset_start_failure=reset_start_failure,
    )

    reset_start_failure.assert_awaited_once_with(row=row)
    start_agent.assert_not_awaited()
    stop_agent.assert_not_awaited()
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
        start_agent=AsyncMock(),
        stop_agent=AsyncMock(),
        write_observed=AsyncMock(),
        clear_token=AsyncMock(),
        reset_start_failure=reset_start_failure,
    )

    reset_start_failure.assert_not_awaited()


@pytest.mark.asyncio
async def test_converge_host_rows_clears_desired_port_when_start_uses_fallback_port() -> None:
    row = _row(desired_state="running", desired_port=4723)
    start_agent = AsyncMock(return_value={"pid": 1234, "port": 4724})
    write_observed = AsyncMock()

    await converge_host_rows(
        host_id=row.host_id,
        rows=[row],
        agent_running=[],
        now=datetime.now(UTC),
        start_agent=start_agent,
        stop_agent=AsyncMock(),
        write_observed=write_observed,
        clear_token=AsyncMock(),
        reset_start_failure=AsyncMock(),
    )

    write_observed.assert_awaited_once_with(
        row=row,
        state="running",
        port=4724,
        pid=1234,
        active_connection_target=row.connection_target,
        clear_desired_port=True,
        allocated_caps=None,
    )


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
        start_agent=AsyncMock(),
        stop_agent=AsyncMock(),
        write_observed=write_observed,
        clear_token=AsyncMock(),
        reset_start_failure=AsyncMock(),
    )

    write_observed.assert_awaited_once_with(
        row=row,
        state="running",
        port=4723,
        pid=12345,
        active_connection_target=row.connection_target,
    )


@pytest.mark.asyncio
async def test_converge_host_rows_skips_one_failed_row_continues_others() -> None:
    host_id = uuid.uuid4()
    row_bad = _row(host_id=host_id, desired_state="running", desired_port=4724)
    row_ok = _row(host_id=host_id, desired_state="running", desired_port=4723)

    async def start_agent(*, row: DesiredRow, port: int | None) -> dict[str, int]:
        if row.device_id == row_bad.device_id:
            raise RuntimeError("agent unreachable")
        return {"pid": 1, "port": port or 4723}

    write_observed = AsyncMock()
    await converge_host_rows(
        host_id=host_id,
        rows=[row_bad, row_ok],
        agent_running=[],
        now=datetime.now(UTC),
        start_agent=start_agent,
        stop_agent=AsyncMock(),
        write_observed=write_observed,
        clear_token=AsyncMock(),
        reset_start_failure=AsyncMock(),
    )

    write_observed.assert_awaited_once()


@pytest.mark.asyncio
async def test_converge_host_rows_clear_token_and_db_clear_branches() -> None:
    expired = _row(
        desired_state="running",
        desired_port=4723,
        transition_token=uuid.uuid4(),
        transition_deadline=datetime.now(UTC) - timedelta(seconds=1),
        port=4723,
        pid=1,
        active_connection_target="emulator-5554",
    )
    stale = _row(
        desired_state="stopped",
        connection_target="stale",
        port=4724,
        pid=2,
        active_connection_target="stale",
    )
    clear_token = AsyncMock()
    write_observed = AsyncMock()

    await converge_host_rows(
        host_id=expired.host_id,
        rows=[expired, stale],
        agent_running=[ObservedEntry(port=4723, pid=1, connection_target=expired.connection_target)],
        now=datetime.now(UTC),
        start_agent=AsyncMock(),
        stop_agent=AsyncMock(),
        write_observed=write_observed,
        clear_token=clear_token,
        reset_start_failure=AsyncMock(),
    )

    clear_token.assert_awaited_once_with(row=expired)
    write_observed.assert_awaited_once_with(
        row=stale,
        state="stopped",
        port=None,
        pid=None,
        active_connection_target=None,
    )


@pytest.mark.asyncio
async def test_converge_host_rows_noop_and_raise_errors_branch() -> None:
    noop = _row(desired_state="stopped")
    await converge_host_rows(
        host_id=noop.host_id,
        rows=[noop],
        agent_running=[],
        now=datetime.now(UTC),
        start_agent=AsyncMock(),
        stop_agent=AsyncMock(),
        write_observed=AsyncMock(),
        clear_token=AsyncMock(),
        reset_start_failure=AsyncMock(),
    )

    failing = _row(desired_state="running", desired_port=4723)
    with pytest.raises(RuntimeError, match="start failed"):
        await converge_host_rows(
            host_id=failing.host_id,
            rows=[failing],
            agent_running=[],
            now=datetime.now(UTC),
            start_agent=AsyncMock(side_effect=RuntimeError("start failed")),
            stop_agent=AsyncMock(),
            write_observed=AsyncMock(),
            clear_token=AsyncMock(),
            reset_start_failure=AsyncMock(),
            raise_errors=True,
        )


def _stop_recorder(stopped: list[int]) -> object:
    async def _stop(*, row: object = None, port: int) -> None:
        stopped.append(port)

    return _stop


@pytest.mark.asyncio
async def test_reap_orphan_nodes_stops_row_less_host_process() -> None:
    # A running process whose target matches no desired row must be stopped,
    # otherwise a stray Appium process leaks (the case the deleted Phase-1
    # ``no_db_row`` path used to cover).
    observed = [ObservedEntry(port=4731, pid=1, connection_target="10.0.0.5:4731")]
    stopped: list[int] = []
    reaped = await reap_orphan_nodes(observed, [], stop_agent=_stop_recorder(stopped))
    assert stopped == [4731]
    assert reaped == [4731]


@pytest.mark.asyncio
async def test_reap_orphan_nodes_keeps_backed_off_node() -> None:
    # A node present in desired rows (even one in recovery backoff, which the
    # reaper is keyed off the FULL desired set to protect) must NOT be reaped.
    row = _row(desired_state="running", connection_target="10.0.0.6:4732")
    observed = [ObservedEntry(port=4732, pid=1, connection_target="10.0.0.6:4732")]
    stopped: list[int] = []
    await reap_orphan_nodes(observed, [row], stop_agent=_stop_recorder(stopped))
    assert stopped == []
