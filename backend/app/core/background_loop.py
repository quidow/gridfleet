"""Shared scaffold for periodic leader-owned background loops.

Owns the while/observe/except/sleep skeleton that every loop used to
hand-roll. Subclasses provide the cycle body and declare their leadership
and failure-logging policy explicitly; divergent wake/startup behavior is
expressed through the hooks, never as special cases here.

`exit_on_leadership_lost=False` mirrors the pre-scaffold behavior of loops
without a LeadershipLost handler: the error is swallowed by the generic
failure path (log + continue), because LeadershipLost subclasses
RuntimeError and was caught by their `except Exception`.
"""

from __future__ import annotations

import asyncio
import os
import time
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, ClassVar

from app.core.leader.advisory import LeadershipLost
from app.core.observability import get_logger, observe_background_loop

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.core.type_defs import SessionFactory

logger = get_logger(__name__)


class BackgroundLoop(ABC):
    """Periodic loop skeleton; subclasses fill in the cycle body and policy."""

    loop_name: ClassVar[str]
    exit_on_leadership_lost: ClassVar[bool]
    cycle_failed_message: ClassVar[str]
    sleep_before_first_cycle: ClassVar[bool] = False

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
        """Runs after every cycle (success, failure, and pre-exit on leadership loss)."""
        return None

    def _on_cycle_error(self) -> None:
        """Runs on generic cycle failure, before the failure log."""
        return None

    def _leadership_lost_event(self) -> str:
        return f"{self.loop_name}_loop_leadership_lost"

    async def run(self) -> None:
        await self._on_start()
        if self.sleep_before_first_cycle:
            await self._wait(self._interval())
        while True:
            interval = self._interval()
            cycle_start = time.monotonic()
            try:
                async with observe_background_loop(self.loop_name, interval).cycle(), self._session_factory() as db:
                    await self._run_cycle(db)
            except LeadershipLost as exc:
                self._on_cycle_end(time.monotonic() - cycle_start, interval)
                if self.exit_on_leadership_lost:
                    logger.error(
                        self._leadership_lost_event(),
                        reason=str(exc),
                        action="exiting_process_to_prevent_split_brain",
                    )
                    os._exit(70)
                logger.exception(self.cycle_failed_message)
            except Exception:
                self._on_cycle_error()
                logger.exception(self.cycle_failed_message)
                self._on_cycle_end(time.monotonic() - cycle_start, interval)
            else:
                self._on_cycle_end(time.monotonic() - cycle_start, interval)
            await self._wait(interval)
