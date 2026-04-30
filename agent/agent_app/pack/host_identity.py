from __future__ import annotations

import asyncio


class HostIdentity:
    def __init__(self) -> None:
        self._value: str | None = None
        self._event = asyncio.Event()

    def set(self, host_id: str) -> None:
        self._value = host_id
        self._event.set()

    def get(self) -> str | None:
        return self._value

    async def wait(self) -> str:
        await self._event.wait()
        assert self._value is not None
        return self._value
