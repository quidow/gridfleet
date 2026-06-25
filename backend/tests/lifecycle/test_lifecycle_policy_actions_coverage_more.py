from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from app.devices.models import Device, DeviceEventType
from app.lifecycle.services import actions
from app.lifecycle.services.actions import LifecyclePolicyActionsService
from app.lifecycle.services.incidents import LifecycleIncidentService
from app.runs.models import RunState
from app.runs.service_reservation import RunReservationService
from tests.fakes import build_review_service


def test_lifecycle_policy_action_small_branch_helpers() -> None:
    assert actions.failure_event_type("connectivity") == DeviceEventType.connectivity_lost

    device = Device(id=__import__("uuid").uuid4())
    intents = actions._crash_intents(device, source="connectivity")
    assert intents[0].source == f"connectivity:{device.id}"
    assert intents[0].payload["stop_mode"] == "defer"


async def test_restore_run_if_needed_early_return_branches() -> None:
    svc = LifecyclePolicyActionsService(
        publisher=Mock(),
        reservation=RunReservationService(review=build_review_service()),
        incidents=LifecycleIncidentService(),
    )
    run = SimpleNamespace(state=RunState.completed)
    assert await svc.restore_run_if_needed(AsyncMock(), SimpleNamespace(), run, None, reason="r", source="s") == (
        run,
        None,
    )
