from __future__ import annotations

from agent_app.grid_node.node_state import NodeState
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
