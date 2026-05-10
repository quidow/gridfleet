from __future__ import annotations

import asyncio

import pytest

from agent_app.grid_node.supervisor import start_grid_node_supervisor


class FakeClock:
    async def sleep(self, _delay: float) -> None:
        await asyncio.sleep(0)


class RecordingService:
    def __init__(self, *, fail_start: bool = False, request_stop: bool = False) -> None:
        self.fail_start = fail_start
        self.request_stop = request_stop
        self.stop_called = False

    async def start(self) -> None:
        if self.fail_start:
            raise RuntimeError("boom")

    async def stop(self) -> None:
        self.stop_called = True

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
