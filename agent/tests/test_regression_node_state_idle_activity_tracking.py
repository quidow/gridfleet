"""Regression test for the idle/activity semantics of ``NodeState.expire_idle``.

Per ``agent_app/config.py`` (``AGENT_GRID_NODE_SESSION_TIMEOUT_SEC``),
the agent must close a session that "has not seen a WebDriver call within
this window". A continuously active session must NOT be expired purely
because total elapsed time crossed the timeout — that was Bug 6 of the
2026-05-20 agent audit.
"""

from __future__ import annotations

from agent_app.grid_node.node_state import NodeState
from agent_app.grid_node.protocol import Slot, Stereotype


def test_expire_idle_does_not_kill_session_with_recent_activity() -> None:
    now_holder = [0.0]
    slot = Slot(id="slot-1", stereotype=Stereotype(caps={"platformName": "android"}))
    state = NodeState(slots=[slot], now=lambda: now_holder[0])

    reservation = state.reserve({"platformName": "android"})
    state.commit(
        reservation.id,
        session_id="session-1",
        started_at=0.0,
        capabilities={},
        session_start_iso="2026-05-20T00:00:00Z",
    )

    # Simulate a long-running session that exchanges WebDriver calls at
    # irregular intervals. Each call resets the idle timer.
    for activity_time in (10.0, 200.0, 500.0, 1000.0, 1500.0):
        now_holder[0] = activity_time
        state.mark_active("session-1", now=activity_time)

    # 1700s since the last activity, well within the 1800s idle window —
    # even though total session duration is 3200s.
    now_holder[0] = 1500.0 + 1700.0
    assert state.expire_idle(now=now_holder[0], timeout_sec=1800.0) == []


def test_expire_idle_still_kills_session_with_no_activity() -> None:
    """Sessions that never registered activity degrade to duration-based expiry."""

    slot = Slot(id="slot-1", stereotype=Stereotype(caps={"platformName": "android"}))
    state = NodeState(slots=[slot], now=lambda: 10.0)
    reservation = state.reserve({"platformName": "android"})
    state.commit(reservation.id, session_id="session-1", started_at=20.0)

    # No mark_active calls — `expire_idle` falls back to `started_at`.
    assert state.expire_idle(now=100.0, timeout_sec=60.0) == ["session-1"]
