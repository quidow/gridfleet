"""Regression: Android native+chrome slots must not converge after reconfigure.

Previously the agent round-tripped a single dict through
``slot_stereotype_caps`` (first slot only) and ``replace_slot_stereotype``
(broadcasts to every slot), which erased the chrome slot's
``browserName="chrome"`` whenever a test run set or cleared ``grid_run_id``.

The fix replaces both with ``update_all_slot_caps``, an in-place per-slot
merge that only touches the changed key. This test guards against any future
caller reintroducing a "first-slot wins" broadcast pattern.
"""

from __future__ import annotations

import time

from agent_app.grid_node.node_state import NodeState
from agent_app.grid_node.protocol import build_slots


def test_update_all_slot_caps_preserves_chrome_after_run_id_change() -> None:
    base = {"platformName": "Android", "appium:udid": "device-123"}
    slots = build_slots(base_caps=base, grid_slots=["native", "chrome"])
    state = NodeState(slots=slots, now=time.monotonic)

    before = state.snapshot_slots()
    assert before[0].stereotype.caps.get("browserName") is None
    assert before[1].stereotype.caps.get("browserName") == "chrome"

    state.update_all_slot_caps({"gridfleet:run_id": "test-run-id"})

    after = state.snapshot_slots()
    assert after[0].stereotype.caps.get("browserName") is None
    assert after[1].stereotype.caps.get("browserName") == "chrome"
    assert after[0].stereotype.caps["gridfleet:run_id"] == "test-run-id"
    assert after[1].stereotype.caps["gridfleet:run_id"] == "test-run-id"
    assert after[0].stereotype.caps != after[1].stereotype.caps
