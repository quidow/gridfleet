"""NodeState supports replacing slot stereotypes."""

from __future__ import annotations

import asyncio
import time

import pytest

from agent_app.grid_node.node_state import NodeState
from agent_app.grid_node.protocol import Slot, Stereotype


def _slot(caps: dict[str, object]) -> Slot:
    return Slot(id="slot-1", stereotype=Stereotype(caps=caps))


def test_replace_slot_stereotype_swaps_caps() -> None:
    state = NodeState(slots=[_slot({"platformName": "Android", "gridfleet:run_id": "free"})], now=time.monotonic)
    new_caps = {"platformName": "Android", "gridfleet:run_id": "abc-123"}
    state.replace_slot_stereotype(new_caps)
    snapshot = state.snapshot_slots()
    assert snapshot[0].stereotype.caps == new_caps


def test_replace_slot_stereotype_preserves_available_state() -> None:
    state = NodeState(slots=[_slot({"x": 1})], now=time.monotonic)
    state.replace_slot_stereotype({"x": 2})
    assert state.snapshot_slots()[0].state == "AVAILABLE"


@pytest.mark.asyncio
async def test_replace_under_concurrent_access() -> None:
    state = NodeState(slots=[_slot({"x": 1})], now=time.monotonic)

    async def writer() -> None:
        for index in range(100):
            state.replace_slot_stereotype({"x": index})
            await asyncio.sleep(0)

    async def reader() -> None:
        for _ in range(100):
            _ = state.snapshot_slots()
            await asyncio.sleep(0)

    await asyncio.gather(writer(), reader())
