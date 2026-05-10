from __future__ import annotations

import pytest

from agent_app.grid_node.node_state import NodeState, NoFreeSlotError, NoMatchingSlotError
from agent_app.grid_node.protocol import Slot, Stereotype


def _slot(slot_id: str, **caps: object) -> Slot:
    return Slot(id=slot_id, stereotype=Stereotype(caps=dict(caps)))


def test_reserve_assigns_a_free_matching_slot() -> None:
    state = NodeState(slots=[_slot("s1", platformName="Android")], now=lambda: 10.0)
    reservation = state.reserve({"platformName": "Android"})
    assert reservation.slot_id == "s1"
    assert reservation.id
    snapshot = state.snapshot()
    assert snapshot.slots[0].state == "RESERVED"
    assert snapshot.slots[0].reservation_id == reservation.id


def test_reserve_raises_no_matching_slot_on_caps_mismatch() -> None:
    state = NodeState(slots=[_slot("s1", platformName="Android")], now=lambda: 10.0)
    with pytest.raises(NoMatchingSlotError):
        state.reserve({"platformName": "iOS"})


def test_reserve_raises_no_free_slot_when_matching_slot_is_reserved() -> None:
    state = NodeState(slots=[_slot("s1", platformName="Android")], now=lambda: 10.0)
    state.reserve({"platformName": "Android"})
    with pytest.raises(NoFreeSlotError):
        state.reserve({"platformName": "Android"})


def test_commit_promotes_reserved_slot_to_busy() -> None:
    state = NodeState(slots=[_slot("s1", platformName="Android")], now=lambda: 10.0)
    reservation = state.reserve({"platformName": "Android"})
    state.commit(reservation.id, session_id="session-1", started_at=15.0)
    snapshot = state.snapshot()
    assert snapshot.slots[0].state == "BUSY"
    assert snapshot.slots[0].reservation_id is None
    assert snapshot.slots[0].session_id == "session-1"
    assert snapshot.slots[0].started_at == 15.0


def test_abort_releases_reserved_slot_and_is_idempotent() -> None:
    state = NodeState(slots=[_slot("s1", platformName="Android")], now=lambda: 10.0)
    reservation = state.reserve({"platformName": "Android"})
    state.abort(reservation.id)
    state.abort(reservation.id)
    snapshot = state.snapshot()
    assert snapshot.slots[0].state == "FREE"
    assert snapshot.slots[0].reservation_id is None
    assert snapshot.slots[0].reserved_at is None


def test_release_releases_busy_slot_and_is_idempotent() -> None:
    state = NodeState(slots=[_slot("s1", platformName="Android")], now=lambda: 10.0)
    reservation = state.reserve({"platformName": "Android"})
    state.commit(reservation.id, session_id="session-1", started_at=15.0)
    state.release("session-1")
    state.release("session-1")
    state.release("unknown-session")
    snapshot = state.snapshot()
    assert snapshot.slots[0].state == "FREE"
    assert snapshot.slots[0].session_id is None
    assert snapshot.slots[0].started_at is None
