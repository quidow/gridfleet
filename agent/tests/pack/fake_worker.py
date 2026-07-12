"""Worker-shaped fake wrapping a plain in-process adapter double.

Gives a test double the surface dispatch relies on — ``call``,
``supported_hooks``, ``pack_id``/``release``, ``alive`` — by reusing the
real worker's argument binding (``_bind_call``), so the double receives the
same rebuilt context dataclasses an adapter sees inside a worker subprocess
(minus the JSON round-trip).
"""

from __future__ import annotations

from typing import Any

import agent_app.pack.worker_protocol as wp
from agent_app.pack.worker import _bind_call


class FakeWorkerHandle:
    def __init__(self, adapter: object, *, pack_id: str | None = None, release: str | None = None) -> None:
        self._adapter = adapter
        self.pack_id = pack_id if pack_id is not None else str(getattr(adapter, "pack_id", ""))
        self.release = release if release is not None else str(getattr(adapter, "pack_release", ""))
        self.supported_hooks = frozenset(hook for hook in wp.HOOK_SPECS if callable(getattr(adapter, hook, None)))
        self.alive = True

    async def call(self, hook: str, payload: dict[str, Any]) -> object:
        return await _bind_call(self._adapter, hook, payload)
