"""Core metrics surface for the GridFleet backend.

Phase 0a state: this module exposes only the registration-based gauge
fan-out dispatcher. The Prometheus registry, gauge objects, and recorder
functions still live in ``app/metrics_recorders.py`` and the legacy
aggregator in ``app/metrics.py``; Phase 0b moves the registry and
recorders here. The fan-out dispatcher will accumulate per-domain
callbacks over phases 5/6/13/14 (events, jobs, devices, sessions).

``app/main.py`` calls ``app.metrics.refresh_system_gauges_legacy``
directly until Phase 14 flips the ``/metrics`` route over to the
dispatcher below.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

GaugeRefresher = Callable[["AsyncSession"], Awaitable[None]]

_refreshers: list[GaugeRefresher] = []


def register_gauge_refresher(fn: GaugeRefresher) -> None:
    """Register a per-domain gauge refresher callback.

    Each contributing domain calls this from its ``__init__.py`` during
    its migration phase. The dispatcher iterates and awaits every
    registered callback when :func:`refresh_system_gauges` is invoked.
    """
    _refreshers.append(fn)


async def refresh_system_gauges(db: AsyncSession) -> None:
    """Fan-out dispatcher. Awaits every registered refresher in order.

    Empty in Phase 0a. Stays empty until each contributing domain
    migrates and calls :func:`register_gauge_refresher`. ``app/main.py``
    does not call this function yet — see module docstring.
    """
    for fn in _refreshers:
        await fn(db)


__all__ = [
    "GaugeRefresher",
    "refresh_system_gauges",
    "register_gauge_refresher",
]
