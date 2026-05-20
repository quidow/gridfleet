from __future__ import annotations

import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent_app.pack.runtime import RuntimeEnv


class RuntimeRegistry:
    def __init__(self) -> None:
        self._by_pack: dict[str, RuntimeEnv] = {}
        self._lock = threading.Lock()

    def set_for_pack(self, pack_id: str, env: RuntimeEnv) -> None:
        with self._lock:
            self._by_pack[pack_id] = env

    def get_for_pack(self, pack_id: str) -> RuntimeEnv | None:
        with self._lock:
            return self._by_pack.get(pack_id)

    def purge_except(self, pack_ids: set[str]) -> None:
        """Drop every pack not in *pack_ids*.

        Called from ``PackStateLoop.run_once`` after a reconcile so a pack
        the backend has retired from the desired list cannot continue to
        satisfy ``resolve_appium_invocation_for_pack`` against a stale
        runtime env.
        """

        with self._lock:
            stale = [pack_id for pack_id in self._by_pack if pack_id not in pack_ids]
            for pack_id in stale:
                self._by_pack.pop(pack_id, None)
