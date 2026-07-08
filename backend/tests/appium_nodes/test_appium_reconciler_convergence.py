"""Desired-state convergence algorithm tests."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from app.appium_nodes.services.reconciler_convergence import (
    DesiredRow,
    ObservedEntry,
    _execute_action,
    decide_convergence_action,
    match_observed_entry,
)


async def converge_host_rows(
    *,
    host_id: uuid.UUID,
    rows: list[DesiredRow],
    agent_running: list[ObservedEntry],
    now: datetime,
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
    write_observed = AsyncMock()

    await converge_host_rows(
        host_id=row.host_id,
        rows=[row],
        agent_running=[observed],
        now=datetime.now(UTC),
        write_observed=write_observed,
        clear_token=AsyncMock(),
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
        clear_token=AsyncMock(),
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
        write_observed=AsyncMock(),
        clear_token=AsyncMock(),
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
            clear_token=AsyncMock(),
            reset_start_failure=AsyncMock(),
            raise_errors=True,
        )
