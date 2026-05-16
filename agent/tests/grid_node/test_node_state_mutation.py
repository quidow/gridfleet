"""NodeState supports per-slot cap updates."""

from __future__ import annotations

import asyncio
import time
from uuid import uuid4

import pytest

from agent_app.grid_node.node_state import NodeState
from agent_app.grid_node.protocol import Slot, Stereotype


def _slot(caps: dict[str, object]) -> Slot:
    return Slot(id=str(uuid4()), stereotype=Stereotype(caps=caps))


def test_update_all_slot_caps_merges_into_existing_caps() -> None:
    state = NodeState(slots=[_slot({"platformName": "Android", "gridfleet:run_id": "free"})], now=time.monotonic)
    state.update_all_slot_caps({"gridfleet:run_id": "abc-123"})
    assert state.snapshot_slots()[0].stereotype.caps == {"platformName": "Android", "gridfleet:run_id": "abc-123"}


def test_update_all_slot_caps_preserves_per_slot_identity_fields() -> None:
    """Chrome slot's ``browserName`` must survive shared-field updates."""
    state = NodeState(
        slots=[
            _slot({"platformName": "Android", "appium:udid": "d1", "gridfleet:run_id": "free"}),
            _slot(
                {"platformName": "Android", "appium:udid": "d1", "browserName": "chrome", "gridfleet:run_id": "free"}
            ),
        ],
        now=time.monotonic,
    )
    state.update_all_slot_caps({"gridfleet:run_id": "run-xyz"})
    snapshot = state.snapshot_slots()
    assert snapshot[0].stereotype.caps.get("browserName") is None
    assert snapshot[1].stereotype.caps.get("browserName") == "chrome"
    assert snapshot[0].stereotype.caps["gridfleet:run_id"] == "run-xyz"
    assert snapshot[1].stereotype.caps["gridfleet:run_id"] == "run-xyz"


def test_update_all_slot_caps_preserves_available_state() -> None:
    state = NodeState(slots=[_slot({"x": 1})], now=time.monotonic)
    state.update_all_slot_caps({"x": 2})
    assert state.snapshot_slots()[0].state == "AVAILABLE"


@pytest.mark.asyncio
async def test_update_all_slot_caps_under_concurrent_access() -> None:
    state = NodeState(slots=[_slot({"x": 1})], now=time.monotonic)

    async def writer() -> None:
        for index in range(100):
            state.update_all_slot_caps({"x": index})
            await asyncio.sleep(0)

    async def reader() -> None:
        for _ in range(100):
            _ = state.snapshot_slots()
            await asyncio.sleep(0)

    await asyncio.gather(writer(), reader())
