"""Shared scaffold for periodic background loops.

Owns the while/observe/except/sleep skeleton that every loop used to
hand-roll. Subclasses provide the cycle body and declare their
failure-logging message; divergent wake/startup behavior is expressed
through the hooks, never as special cases here.
"""

from __future__ import annotations

import asyncio
import time
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, ClassVar

from app.core.metrics_recorders import record_background_loop_effective_period
from app.core.observability import get_logger, observe_background_loop

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.core.type_defs import SessionFactory

logger = get_logger(__name__)


def stage_due(cycle_index: int, *, base_interval: float, stage_interval: float) -> bool:
    """True when a stage with its own interval is due on this sweep cycle."""
    divisor = max(1, round(stage_interval / base_interval))
    return cycle_index % divisor == 0


class BackgroundLoop(ABC):
    """Periodic loop skeleton; subclasses fill in the cycle body and policy."""

    loop_name: ClassVar[str]
    cycle_failed_message: ClassVar[str]

    @property
    @abstractmethod
    def _session_factory(self) -> SessionFactory: ...

    @abstractmethod
    def _interval(self) -> float:
        """Seconds between cycles; read every iteration."""

    @abstractmethod
    async def _run_cycle(self, db: AsyncSession) -> None:
        """One cycle of work, inside the observe/session context."""

    async def _on_start(self) -> None:
        """Runs once before the first wait/cycle (hook registration, caching)."""
        return None

    async def _wait(self, interval: float) -> None:
        """Inter-cycle wait; doorbell loops override with wait_for_wake."""
        await asyncio.sleep(interval)

    def _on_cycle_end(self, elapsed_seconds: float, interval: float) -> None:
        """Runs after every cycle (success and failure)."""
        return None

    def _on_cycle_error(self) -> None:
        """Runs on generic cycle failure, before the failure log."""
        return None

    async def run(self) -> None:
        await self._on_start()
        while True:
            interval = self._interval()
            cycle_start = time.monotonic()
            try:
                async with observe_background_loop(self.loop_name, interval).cycle(), self._session_factory() as db:
                    await self._run_cycle(db)
            except Exception:
                self._on_cycle_error()
                logger.exception(self.cycle_failed_message)
                self._on_cycle_end(time.monotonic() - cycle_start, interval)
            else:
                self._on_cycle_end(time.monotonic() - cycle_start, interval)
            # Sleep only the remainder of the interval so the cycle period is a
            # true cadence (interval), not interval + cycle_duration. Doorbell
            # overrides forward this value as their wait_for_wake timeout, so the
            # cadence fix applies to them too.
            await self._wait(max(0.0, interval - (time.monotonic() - cycle_start)))
            # Real cadence (cycle work + sleep); captures early doorbell wakes too.
            record_background_loop_effective_period(self.loop_name, time.monotonic() - cycle_start)
