"""Cross-domain housekeeping loop: one BackgroundLoop hosting stage_due stages.

Scheduling doctrine: ``BackgroundLoop`` for independent lifecycles;
``stage_due`` stages only as sub-cadences of an owning sweep. The janitor is
the owning sweep for trivial periodic chores that do not earn their own loop.
Stages are injected by the composition root so this module stays domain-free.
Stage cadences are plumbing constants (design P5), not registry settings.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.core.background_loop import BackgroundLoop, stage_due
from app.core.observability import get_logger

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.core.type_defs import SessionFactory

logger = get_logger(__name__)

LOOP_NAME = "janitor"
JANITOR_BASE_INTERVAL_SEC = 15.0


@dataclass(frozen=True)
class JanitorStage:
    """One housekeeping chore run at its own sub-cadence of the janitor tick."""

    name: str
    interval_sec: float
    run: Callable[[AsyncSession], Awaitable[object]]
    skip_first_cycle: bool = False  # e.g. data_cleanup: never run at boot


class JanitorLoop(BackgroundLoop):
    """Leader-owned housekeeping loop; see module docstring."""

    loop_name = LOOP_NAME
    cycle_failed_message = "janitor_cycle_failed"

    def __init__(self, *, session_factory: SessionFactory, stages: Sequence[JanitorStage]) -> None:
        self._sf = session_factory
        self._stages = tuple(stages)
        self._cycle_index = 0

    @property
    def _session_factory(self) -> SessionFactory:
        return self._sf

    def _interval(self) -> float:
        return JANITOR_BASE_INTERVAL_SEC

    async def _run_cycle(self, db: AsyncSession) -> None:
        for stage in self._stages:
            if stage.skip_first_cycle and self._cycle_index == 0:
                continue
            if not stage_due(
                self._cycle_index, base_interval=JANITOR_BASE_INTERVAL_SEC, stage_interval=stage.interval_sec
            ):
                continue
            try:
                await stage.run(db)
            except Exception:
                # Stage isolation (host_sweep precedent): one chore's failure
                # must not starve the others this tick. Roll back so the shared
                # session is clean for the next stage.
                logger.exception("janitor_stage_failed", stage=stage.name)
                await db.rollback()

    def _on_cycle_end(self, elapsed_seconds: float, interval: float) -> None:
        del elapsed_seconds, interval
        self._cycle_index += 1
