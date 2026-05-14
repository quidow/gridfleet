from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.devices.models import Device, DeviceEventType
from app.devices.services import lifecycle_policy_actions as actions
from app.runs.models import RunState


def test_lifecycle_policy_action_small_branch_helpers() -> None:
    manual = SimpleNamespace(auto_manage=False)
    assert actions.offline_summary_state(manual) == actions.DeviceLifecyclePolicySummaryState.manual
    assert actions.failure_event_type("connectivity") == DeviceEventType.connectivity_lost

    device = Device(id=__import__("uuid").uuid4())
    intents = actions._crash_intents(device, source="connectivity", reason="lost")
    assert intents[0].source == f"connectivity:{device.id}"
    assert intents[0].payload["stop_mode"] == "defer"


async def test_restore_run_if_needed_early_return_branches() -> None:
    run = SimpleNamespace(state=RunState.completed)
    assert await actions.restore_run_if_needed(AsyncMock(), SimpleNamespace(), run, None, reason="r", source="s") == (
        run,
        None,
    )
