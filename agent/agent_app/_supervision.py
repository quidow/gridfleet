from __future__ import annotations

import collections
from dataclasses import dataclass, field


@dataclass
class ExponentialBackoff:
    base: float
    factor: float
    cap: float
    max_attempts: int
    window_sec: float
    _step: int = 0
    _attempts: collections.deque[float] = field(default_factory=collections.deque)

    def next_delay(self) -> float:
        delay = min(self.cap, self.base * (self.factor**self._step))
        self._step += 1
        return delay

    def reset(self) -> None:
        self._step = 0
        self._attempts.clear()

    def record_attempt(self, now: float) -> None:
        self._attempts.append(now)
        self._trim(now)

    def attempts_in_window(self, now: float) -> int:
        self._trim(now)
        return len(self._attempts)

    def can_attempt(self, now: float) -> bool:
        return self.attempts_in_window(now) < self.max_attempts

    def _trim(self, now: float) -> None:
        cutoff = now - self.window_sec
        while self._attempts and self._attempts[0] < cutoff:
            self._attempts.popleft()
