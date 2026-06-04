"""NodeHealthService doorbell semantics (grid node-event wake path).

Mirrors the SessionSyncService doorbell: wake() is sticky until consumed,
wait_for_wake() reports doorbell vs timeout and clears the flag.
"""

from __future__ import annotations

from unittest.mock import Mock

from app.appium_nodes.services.node_health import NodeHealthService
from tests.fakes import FakeSettingsReader, make_fake_grid
from tests.helpers import test_event_bus as event_bus


def _make_service() -> NodeHealthService:
    return NodeHealthService(
        publisher=event_bus,
        settings=FakeSettingsReader({}),
        pool=Mock(),
        circuit_breaker=Mock(),
        grid=make_fake_grid(),
        recovery_control=Mock(),
        health=Mock(),
        incidents=Mock(),
    )


async def test_wake_before_wait_returns_immediately() -> None:
    svc = _make_service()
    svc.wake()
    assert await svc.wait_for_wake(timeout=5.0) is True


async def test_wait_times_out_without_wake() -> None:
    svc = _make_service()
    assert await svc.wait_for_wake(timeout=0.05) is False


async def test_wait_clears_doorbell_after_wake() -> None:
    svc = _make_service()
    svc.wake()
    assert await svc.wait_for_wake(timeout=1.0) is True
    # Consumed: a second wait must time out, not return immediately.
    assert await svc.wait_for_wake(timeout=0.05) is False


async def test_burst_of_wakes_coalesces_into_one() -> None:
    svc = _make_service()
    svc.wake()
    svc.wake()
    svc.wake()
    assert await svc.wait_for_wake(timeout=1.0) is True
    assert await svc.wait_for_wake(timeout=0.05) is False
