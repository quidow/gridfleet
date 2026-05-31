"""Contract tests for node.state_changed after-commit dispatch."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: TC002

from app.appium_nodes.services.reconciler_agent import mark_node_started, mark_node_stopped
from app.devices import locking as device_locking
from app.devices.models import DeviceOperationalState
from app.devices.services.state import set_hold as _orig_set_hold
from app.devices.services.state import set_operational_state as _orig_set_op
from tests.fakes import FakeSettingsReader
from tests.helpers import seed_host_and_device, settle_after_commit_tasks
from tests.helpers import test_event_bus as event_bus

pytestmark = pytest.mark.usefixtures("seeded_driver_packs")


@pytest.fixture(autouse=True)
def _inject_publisher(monkeypatch: pytest.MonkeyPatch) -> None:
    """Inject publisher into state machine so events fire."""

    async def _wrapped_set_op(device: object, new_state: object, **kwargs: object) -> object:
        kwargs.setdefault("publisher", event_bus)
        return await _orig_set_op(device, new_state, **kwargs)  # type: ignore[arg-type]

    async def _wrapped_set_hold(device: object, new_hold: object, **kwargs: object) -> object:
        kwargs.setdefault("publisher", event_bus)
        return await _orig_set_hold(device, new_hold, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr("app.devices.services.lifecycle_state_machine.set_operational_state", _wrapped_set_op)
    monkeypatch.setattr("app.devices.services.lifecycle_state_machine.set_hold", _wrapped_set_hold)


async def test_mark_node_started_queues_state_changed_after_availability(
    db_session: AsyncSession,
    event_bus_capture: list[tuple[str, dict[str, Any]]],
) -> None:
    _, device = await seed_host_and_device(
        db_session,
        identity="node-start-1",
        operational_state=DeviceOperationalState.offline,
    )
    event_bus_capture.clear()

    locked = await device_locking.lock_device(db_session, device.id)
    await mark_node_started(db_session, locked, port=4730, pid=42, publisher=event_bus, settings=FakeSettingsReader({}))
    await settle_after_commit_tasks()

    types_in_order = [name for name, _ in event_bus_capture]
    assert "device.operational_state_changed" in types_in_order
    assert "node.state_changed" in types_in_order
    avail_idx = types_in_order.index("device.operational_state_changed")
    node_idx = types_in_order.index("node.state_changed")
    assert avail_idx < node_idx, f"FIFO order: availability must precede node.state_changed; got {types_in_order}"

    node_payload = next(p for n, p in event_bus_capture if n == "node.state_changed")
    assert node_payload["new_state"] == "running"
    assert node_payload["port"] == 4730


async def test_mark_node_stopped_queues_state_changed(
    db_session: AsyncSession,
    event_bus_capture: list[tuple[str, dict[str, Any]]],
) -> None:
    _, device = await seed_host_and_device(db_session, identity="node-stop-1")
    event_bus_capture.clear()

    locked = await device_locking.lock_device(db_session, device.id)
    await mark_node_started(db_session, locked, port=4731, pid=43, publisher=event_bus, settings=FakeSettingsReader({}))
    event_bus_capture.clear()

    locked = await device_locking.lock_device(db_session, device.id)
    await mark_node_stopped(db_session, locked, publisher=event_bus)
    await settle_after_commit_tasks()

    node_events = [p for n, p in event_bus_capture if n == "node.state_changed"]
    assert len(node_events) == 1
    assert node_events[0]["new_state"] == "stopped"
