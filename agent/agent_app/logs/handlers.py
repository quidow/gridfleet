"""Log handlers attached to the agent root logger."""

from __future__ import annotations

import logging
from collections import deque
from threading import Lock


class RingBufferHandler(logging.Handler):
    """Bounded in-memory log buffer that stores pre-formatted strings."""

    def __init__(self, maxlen: int = 1000) -> None:
        super().__init__()
        self._buf: deque[str] = deque(maxlen=maxlen)
        self._buf_lock = Lock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            rendered = self.format(record)
        except Exception:
            self.handleError(record)
            return
        with self._buf_lock:
            self._buf.append(rendered)

    def snapshot(self) -> list[str]:
        with self._buf_lock:
            return list(self._buf)
