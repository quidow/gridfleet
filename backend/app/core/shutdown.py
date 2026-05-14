from __future__ import annotations

import asyncio


class ShutdownCoordinator:
    def __init__(self) -> None:
        self._shutting_down = False
        self._active_requests = 0
        self._drained = asyncio.Event()
        self._drained.set()

    def is_shutting_down(self) -> bool:
        return self._shutting_down

    def active_requests(self) -> int:
        return self._active_requests

    async def begin_shutdown(self) -> None:
        if self._shutting_down:
            return
        self._shutting_down = True
        if self._active_requests == 0:
            self._drained.set()

    def request_started(self) -> None:
        self._active_requests += 1
        if self._active_requests == 1:
            self._drained.clear()

    def request_finished(self) -> None:
        if self._active_requests == 0:
            return
        self._active_requests -= 1
        if self._active_requests == 0:
            self._drained.set()

    async def wait_for_drain(self, timeout: float) -> bool:
        try:
            await asyncio.wait_for(self._drained.wait(), timeout=timeout)
        except TimeoutError:
            return False
        return True

    def reset(self) -> None:
        self._shutting_down = False
        self._active_requests = 0
        self._drained = asyncio.Event()
        self._drained.set()


shutdown_coordinator = ShutdownCoordinator()
