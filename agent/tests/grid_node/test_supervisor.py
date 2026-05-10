from __future__ import annotations

import asyncio

import pytest

from agent_app.grid_node.config import GridNodeConfig
from agent_app.grid_node.protocol import Slot, Stereotype
from agent_app.grid_node.supervisor import start_grid_node_supervisor


class FakeClock:
    def __init__(self) -> None:
        self.sleeps: list[float] = []

    async def sleep(self, _delay: float) -> None:
        self.sleeps.append(_delay)
        await asyncio.sleep(0)


class RecordingService:
    def __init__(self, *, fail_start: bool = False, request_stop: bool = False) -> None:
        self.fail_start = fail_start
        self.request_stop = request_stop
        self.stop_called = False
        self.heartbeat_calls = 0

    async def start(self) -> None:
        if self.fail_start:
            raise RuntimeError("boom")

    async def stop(self) -> None:
        self.stop_called = True

    async def run_heartbeat_once(self) -> None:
        self.heartbeat_calls += 1
        if self.request_stop:
            self.request_stop = False

    def snapshot(self) -> dict[str, object]:
        return {"requested_stop": self.request_stop}


class FlakyServiceFactory:
    def __init__(self, *, failures_before_success: int) -> None:
        self.failures_before_success = failures_before_success
        self.created = 0

    def __call__(self) -> RecordingService:
        self.created += 1
        return RecordingService(fail_start=self.created <= self.failures_before_success)


class AlwaysCrashingServiceFactory:
    def __init__(self) -> None:
        self.created = 0

    def __call__(self) -> RecordingService:
        self.created += 1
        return RecordingService(fail_start=True)


@pytest.mark.asyncio
async def test_supervisor_restarts_service_after_crash() -> None:
    factory = FlakyServiceFactory(failures_before_success=1)
    handle = start_grid_node_supervisor(factory=factory, clock=FakeClock())
    await handle.start()
    await handle.wait_until_running()
    assert factory.created == 2
    await handle.stop()


@pytest.mark.asyncio
async def test_supervisor_reports_errored_after_five_crashes_in_window() -> None:
    factory = AlwaysCrashingServiceFactory()
    handle = start_grid_node_supervisor(factory=factory, clock=FakeClock())
    await handle.start()
    await handle.wait_until_errored()
    assert handle.errored is True
    assert factory.created == 5


@pytest.mark.asyncio
async def test_supervisor_calls_stop_when_service_requests_stop() -> None:
    services: list[RecordingService] = []

    def factory() -> RecordingService:
        service = RecordingService(request_stop=True)
        services.append(service)
        return service

    handle = start_grid_node_supervisor(factory=factory, clock=FakeClock())
    await handle.start()
    await handle.wait_until_stopped()
    assert services[0].stop_called is True


@pytest.mark.asyncio
async def test_supervisor_runs_periodic_heartbeat_with_config_interval() -> None:
    service = RecordingService()
    clock = FakeClock()
    handle = start_grid_node_supervisor(factory=lambda: service, clock=clock, config=_config(heartbeat_sec=2.5))
    await handle.start()
    await handle.wait_until_running()
    for _ in range(20):
        if service.heartbeat_calls:
            break
        await asyncio.sleep(0)
    assert service.heartbeat_calls >= 1
    assert clock.sleeps[-1] == 2.5
    await handle.stop()


def test_supervisor_snapshot_includes_status() -> None:
    handle = start_grid_node_supervisor(factory=RecordingService, clock=FakeClock())
    assert handle.snapshot()["status"] == "starting"


def _config(*, heartbeat_sec: float) -> GridNodeConfig:
    return GridNodeConfig(
        node_id="node-1",
        node_uri="http://127.0.0.1:5555",
        appium_upstream="http://127.0.0.1:4723",
        slots=[Slot(id="slot-1", stereotype=Stereotype(caps={"platformName": "Android"}))],
        hub_publish_url="tcp://127.0.0.1:4442",
        hub_subscribe_url="tcp://127.0.0.1:4443",
        heartbeat_sec=heartbeat_sec,
        session_timeout_sec=300.0,
        proxy_timeout_sec=30.0,
    )
