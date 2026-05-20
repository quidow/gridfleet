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
        # Defensive yield: if multiple ``set()`` calls land in the same
        # event-loop tick (synthetic rotation), let later ones finish
        # before snapshotting ``_value`` so all waiters agree on the
        # latest id. Long-lived consumers must still re-read via
        # ``get()`` per request — see callers in ``lifespan.py``.
        await asyncio.sleep(0)
        assert self._value is not None
        return self._value
