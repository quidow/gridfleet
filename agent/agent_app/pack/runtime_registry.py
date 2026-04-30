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
