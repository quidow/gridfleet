from __future__ import annotations

import asyncio

import pytest

from agent_app.grid_node.supervisor import start_grid_node_supervisor


class FakeClock:
    def __init__(self) -> None:
        self.sleeps: list[float] = []

    async def sleep(self, delay: float) -> None:
        self.sleeps.append(delay)
        await asyncio.sleep(0)


class RecordingService:
    def __init__(
        self,
        *,
        fail_start: bool = False,
        fail_heartbeat: bool = False,
        request_stop: bool = False,
    ) -> None:
        self.fail_start = fail_start
        self.fail_heartbeat = fail_heartbeat
        self.request_stop = request_stop
        self.stop_called = False
        self.heartbeat_calls = 0

    @property
    def node_id(self) -> str:
        return "node-1"

    async def start(self) -> None:
        if self.fail_start:
            raise RuntimeError("boom")

    async def stop(self) -> None:
        self.stop_called = True

    async def run_heartbeat_once(self) -> None:
        self.heartbeat_calls += 1
        if self.fail_heartbeat:
            raise RuntimeError("heartbeat failed")

    def snapshot(self) -> dict[str, object]:
        return {"requested_stop": self.request_stop}

    def slot_stereotype_caps(self) -> dict[str, object]:
        return {}

    def has_active_session(self) -> bool:
        return False

    async def reregister_with_stereotype(
        self, *, new_caps: dict[str, object], drain_grace_sec: float | None = None
    ) -> None:
        pass


class CrashingFactory:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> RecordingService:
        self.calls += 1
        raise RuntimeError("factory boom")


@pytest.mark.asyncio
async def test_supervisor_wait_until_running_timeout_when_never_started() -> None:
    handle = start_grid_node_supervisor(
        factory=lambda: RecordingService(fail_start=True),
        clock=FakeClock(),
        config=FakeConfig(startup_timeout_sec=0.05, heartbeat_sec=1.0),
    )
    with pytest.raises(TimeoutError, match="did not report running"):
        await handle.wait_until_running()


@pytest.mark.asyncio
async def test_supervisor_wait_until_running_raises_when_errored_before_running() -> None:
    handle = start_grid_node_supervisor(
        factory=CrashingFactory(),
        clock=FakeClock(),
        config=FakeConfig(startup_timeout_sec=2.0, heartbeat_sec=1.0),
    )
    await handle.start()
    with pytest.raises(RuntimeError, match="grid node supervisor failed before running"):
        await handle.wait_until_running()


@pytest.mark.asyncio
async def test_supervisor_stop_before_running_sets_stopped() -> None:
    handle = start_grid_node_supervisor(
        factory=RecordingService,
        clock=FakeClock(),
        config=FakeConfig(heartbeat_sec=1.0),
    )
    await handle.stop()
    snap = handle.snapshot()
    assert snap["status"] == "stopped"
    assert snap["running"] is False
    assert handle.is_running() is False


@pytest.mark.asyncio
async def test_supervisor_service_stop_requested_mid_heartbeat() -> None:
    class StopAfterOne(RecordingService):
        async def run_heartbeat_once(self) -> None:
            self.heartbeat_calls += 1
            self.request_stop = True

    handle = start_grid_node_supervisor(
        factory=StopAfterOne,
        clock=FakeClock(),
        config=FakeConfig(heartbeat_sec=0.1),
    )
    await handle.start()
    await handle.wait_until_stopped()
    assert handle.snapshot()["status"] == "stopped"


@pytest.mark.asyncio
async def test_supervisor_wait_until_errored() -> None:
    handle = start_grid_node_supervisor(
        factory=lambda: RecordingService(fail_start=True),
        clock=FakeClock(),
        config=FakeConfig(heartbeat_sec=1.0),
    )
    await handle.start()
    await handle.wait_until_errored()
    assert handle.errored is True


@pytest.mark.asyncio
async def test_supervisor_heartbeat_failure_triggers_stop_and_error() -> None:
    service = RecordingService(fail_heartbeat=True)
    handle = start_grid_node_supervisor(
        factory=lambda: service,
        clock=FakeClock(),
        config=FakeConfig(heartbeat_sec=0.1),
    )
    await handle.start()
    await handle.wait_until_errored()
    assert service.stop_called is True
    assert handle.snapshot()["status"] == "error"


def test_supervisor_snapshot_starting_no_task() -> None:
    handle = start_grid_node_supervisor(factory=RecordingService, clock=FakeClock())
    assert handle.snapshot() == {"errored": False, "running": False, "status": "starting"}


class FakeConfig:
    def __init__(self, *, heartbeat_sec: float = 5.0, startup_timeout_sec: float = 30.0) -> None:
        self.heartbeat_sec = heartbeat_sec
        self.startup_timeout_sec = startup_timeout_sec
