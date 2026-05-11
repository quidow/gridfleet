"""Desired-state convergence algorithm tests."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from app.services.appium_reconciler_convergence import (
    DesiredRow,
    ObservedEntry,
    converge_host_rows,
    decide_convergence_action,
)


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
        "state": "stopped",
        "port": None,
        "pid": None,
        "active_connection_target": None,
    }
    defaults.update(kw)
    return DesiredRow(**defaults)  # type: ignore[arg-type]


def test_desired_running_no_token_no_observed_picks_start() -> None:
    row = _row(desired_state="running", desired_port=4723)
    action = decide_convergence_action(row, observed=None, now=datetime.now(UTC))
    assert action.kind == "start"
    assert action.port == 4723


def test_desired_running_no_token_observed_matching_picks_noop() -> None:
    row = _row(
        desired_state="running",
        desired_port=4723,
        state="running",
        port=4723,
        pid=12345,
        active_connection_target="emulator-5554",
    )
    obs = ObservedEntry(port=4723, pid=12345, connection_target=row.connection_target)
    action = decide_convergence_action(row, observed=obs, now=datetime.now(UTC))
    assert action.kind == "no_op"


def test_desired_running_observed_but_db_stopped_repairs_observed_state() -> None:
    row = _row(desired_state="running", desired_port=4723, state="stopped", port=4723)
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
        state="running",
        port=4723,
    )
    obs = ObservedEntry(port=4723, pid=1, connection_target=row.connection_target)
    action = decide_convergence_action(row, observed=obs, now=datetime.now(UTC))
    assert action.kind == "clear_expired_token"


def test_desired_stopped_with_observed_picks_stop() -> None:
    row = _row(desired_state="stopped", state="running", port=4723)
    obs = ObservedEntry(port=4723, pid=1, connection_target=row.connection_target)
    action = decide_convergence_action(row, observed=obs, now=datetime.now(UTC))
    assert action.kind == "stop"
    assert action.port == 4723


def test_desired_stopped_no_observed_picks_noop_or_db_clear() -> None:
    row = _row(desired_state="stopped", state="stopped")
    action = decide_convergence_action(row, observed=None, now=datetime.now(UTC))
    assert action.kind == "no_op"


def test_desired_stopped_no_observed_but_db_says_running_picks_db_clear() -> None:
    row = _row(desired_state="stopped", state="running", port=4723)
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
    )

    start_agent.assert_awaited_once()
    stop_agent.assert_not_awaited()
    write_observed.assert_awaited_once()


@pytest.mark.asyncio
async def test_converge_host_rows_repairs_observed_running_db_stopped() -> None:
    row = _row(desired_state="running", desired_port=4723, state="stopped", port=4723)
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
    )

    write_observed.assert_awaited_once()
