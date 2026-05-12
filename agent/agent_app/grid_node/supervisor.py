from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING, Protocol

from agent_app._supervision import ExponentialBackoff

if TYPE_CHECKING:
    from collections.abc import Callable


class GridNodeServiceProtocol(Protocol):
    @property
    def node_id(self) -> str:
        raise NotImplementedError

    async def start(self) -> None:
        raise NotImplementedError

    async def stop(self) -> None:
        raise NotImplementedError

    async def run_heartbeat_once(self) -> None:
        raise NotImplementedError

    def snapshot(self) -> dict[str, object]:
        raise NotImplementedError

    def slot_stereotype_caps(self) -> dict[str, object]:
        raise NotImplementedError

    def has_active_session(self) -> bool:
        raise NotImplementedError

    async def reregister_with_stereotype(
        self, *, new_caps: dict[str, object], drain_grace_sec: float | None = None
    ) -> None:
        raise NotImplementedError


class Clock(Protocol):
    async def sleep(self, delay: float) -> None:
        raise NotImplementedError


class AsyncioClock:
    async def sleep(self, delay: float) -> None:
        await asyncio.sleep(delay)


class GridNodeSupervisorHandle:
    def __init__(
        self,
        *,
        factory: Callable[[], GridNodeServiceProtocol],
        clock: Clock,
        heartbeat_sec: float,
        startup_timeout_sec: float = 30.0,
    ) -> None:
        self._factory = factory
        self._clock = clock
        self._heartbeat_sec = heartbeat_sec
        self._startup_timeout_sec = startup_timeout_sec
        self._task: asyncio.Task[None] | None = None
        self._stop_requested = asyncio.Event()
        self._running = asyncio.Event()
        self._stopped = asyncio.Event()
        self._errored = asyncio.Event()
        self._service: GridNodeServiceProtocol | None = None

    @property
    def errored(self) -> bool:
        return self._errored.is_set()

    @property
    def service(self) -> GridNodeServiceProtocol | None:
        return self._service

    async def start(self) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stop_requested.set()
        if self._service is not None:
            await self._service.stop()
        self._running.clear()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        self._stopped.set()

    async def wait_until_running(self) -> None:
        # Slow hosts (cold uvicorn boot, slow ZMQ XSUB/XPUB handshake) can need
        # several seconds to reach the `running` event. The default startup
        # timeout is configurable so callers do not see spurious TimeoutError
        # on hardware where the service would still come up healthy.
        running_task = asyncio.create_task(self._running.wait())
        errored_task = asyncio.create_task(self._errored.wait())
        done, pending = await asyncio.wait(
            {running_task, errored_task}, timeout=self._startup_timeout_sec, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
        for task in pending:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if not done:
            raise TimeoutError("grid node supervisor did not report running")
        if errored_task in done and self.errored:
            raise RuntimeError("grid node supervisor failed before running")

    async def wait_until_errored(self) -> None:
        await asyncio.wait_for(self._errored.wait(), timeout=1.0)

    async def wait_until_stopped(self) -> None:
        await asyncio.wait_for(self._stopped.wait(), timeout=1.0)

    def snapshot(self) -> dict[str, object]:
        if self.errored:
            status = "error"
        elif self._running.is_set():
            status = "up"
        elif self._stopped.is_set():
            status = "stopped"
        else:
            status = "starting"
        return {"errored": self.errored, "running": self._running.is_set(), "status": status}

    def is_running(self) -> bool:
        return self._running.is_set()

    async def _run(self) -> None:
        backoff = ExponentialBackoff(base=1.0, factor=2.0, cap=30.0, max_attempts=5, window_sec=300.0)
        while not self._stop_requested.is_set():
            try:
                # `record_attempt` runs before the factory + start so that a
                # factory exception (config errors, transient zmq failures,
                # etc.) is also counted toward the retry budget — without it
                # the backoff window would never be consumed and the loop
                # would burn CPU through tight retries.
                backoff.record_attempt(asyncio.get_running_loop().time())
                service = self._factory()
                self._service = service
                await service.start()
            except Exception:
                if not backoff.can_attempt(asyncio.get_running_loop().time()):
                    self._errored.set()
                    self._stopped.set()
                    return
                await self._clock.sleep(backoff.next_delay())
                continue
            self._running.set()
            if service.snapshot().get("requested_stop") is True:
                await service.stop()
                self._running.clear()
                self._stopped.set()
                return
            while not self._stop_requested.is_set():
                await self._clock.sleep(self._heartbeat_sec)
                if self._stop_requested.is_set():
                    break
                try:
                    await service.run_heartbeat_once()
                except Exception:
                    self._errored.set()
                    self._running.clear()
                    with contextlib.suppress(Exception):
                        await service.stop()
                    self._stopped.set()
                    return
                if service.snapshot().get("requested_stop") is True:
                    break
            await service.stop()
            self._running.clear()
            self._stopped.set()
            return


def start_grid_node_supervisor(
    *, factory: Callable[[], GridNodeServiceProtocol], clock: Clock | None = None, config: object | None = None
) -> GridNodeSupervisorHandle:
    heartbeat_sec = 5.0
    startup_timeout_sec = 30.0
    if config is not None:
        heartbeat_sec = float(getattr(config, "heartbeat_sec", heartbeat_sec))
        startup_timeout_sec = float(getattr(config, "startup_timeout_sec", startup_timeout_sec))
    return GridNodeSupervisorHandle(
        factory=factory,
        clock=clock or AsyncioClock(),
        heartbeat_sec=heartbeat_sec,
        startup_timeout_sec=startup_timeout_sec,
    )
