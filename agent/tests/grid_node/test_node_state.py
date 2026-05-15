from __future__ import annotations

import pytest

from agent_app.grid_node.node_state import NodeState, NoFreeSlotError, NoMatchingSlotError, ReservationGoneError
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


def test_expire_reservations_releases_old_reservations() -> None:
    state = NodeState(slots=[_slot("s1", platformName="Android")], now=lambda: 10.0)
    reservation = state.reserve({"platformName": "Android"})
    expired = state.expire_reservations(now=45.0, ttl_sec=30.0)
    assert expired == [reservation.id]
    assert state.snapshot().slots[0].state == "FREE"


def test_commit_after_reservation_expiry_raises() -> None:
    state = NodeState(slots=[_slot("s1", platformName="Android")], now=lambda: 10.0)
    reservation = state.reserve({"platformName": "Android"})
    state.expire_reservations(now=45.0, ttl_sec=30.0)
    with pytest.raises(ReservationGoneError):
        state.commit(reservation.id, session_id="session-1", started_at=50.0)


def test_mark_drain_blocks_new_reservations_and_is_idempotent() -> None:
    state = NodeState(slots=[_slot("s1", platformName="Android")], now=lambda: 10.0)
    state.mark_drain()
    state.mark_drain()
    assert state.snapshot().drain is True
    with pytest.raises(NoFreeSlotError):
        state.reserve({"platformName": "Android"})


def test_expire_idle_returns_busy_sessions_past_timeout() -> None:
    state = NodeState(slots=[_slot("s1", platformName="Android")], now=lambda: 10.0)
    reservation = state.reserve({"platformName": "Android"})
    state.commit(reservation.id, session_id="session-1", started_at=20.0)
    assert state.expire_idle(now=100.0, timeout_sec=60.0) == ["session-1"]
    assert state.snapshot().slots[0].state == "BUSY"


def test_reserve_matches_nested_capability_subset() -> None:
    state = NodeState(
        slots=[
            _slot(
                "s1",
                platformName="Android",
                **{"appium:options": {"automationName": "UiAutomator2", "udid": "device-1"}},
            )
        ],
        now=lambda: 10.0,
    )
    reservation = state.reserve({"appium:options": {"automationName": "UiAutomator2"}})
    assert reservation.slot_id == "s1"


def test_android_chrome_slot_cannot_be_reserved_while_native_slot_is_held() -> None:
    # A grid node represents one physical device. Android nodes advertise a
    # native and a chrome slot for capability routing, but the device can only
    # run one Appium session at a time, so the second reservation must fail.
    state = NodeState(
        slots=[
            _slot("native", platformName="Android"),
            _slot("chrome", platformName="Android", browserName="Chrome"),
        ],
        now=lambda: 10.0,
    )
    native = state.reserve({"platformName": "Android"})
    assert native.slot_id == "native"
    with pytest.raises(NoFreeSlotError):
        state.reserve({"platformName": "Android", "browserName": "Chrome"})


def test_android_chrome_slot_reservable_after_native_slot_release() -> None:
    state = NodeState(
        slots=[
            _slot("native", platformName="Android"),
            _slot("chrome", platformName="Android", browserName="Chrome"),
        ],
        now=lambda: 10.0,
    )
    native = state.reserve({"platformName": "Android"})
    state.abort(native.id)
    chrome = state.reserve({"platformName": "Android", "browserName": "Chrome"})
    assert chrome.slot_id == "chrome"
