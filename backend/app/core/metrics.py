"""Core metrics surface for the GridFleet backend.

Exposes the registration-based gauge fan-out dispatcher. Each domain
registers a refresher via :func:`register_gauge_refresher` from its
package ``__init__``; ``app/main.py``'s ``/metrics`` route calls
:func:`refresh_system_gauges` to run them all. Prometheus gauge objects
and recorder functions live in ``app/core/metrics_recorders.py``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

GaugeRefresher = Callable[["AsyncSession"], Awaitable[None]]

_refreshers: list[GaugeRefresher] = []


def register_gauge_refresher(fn: GaugeRefresher) -> None:
    """Register a per-domain gauge refresher callback.

    Each contributing domain calls this from its ``__init__.py``. The
    dispatcher iterates and awaits every registered callback when
    :func:`refresh_system_gauges` is invoked.
    """
    _refreshers.append(fn)


async def refresh_system_gauges(db: AsyncSession) -> None:
    """Fan-out dispatcher. Awaits every registered refresher in order."""
    for fn in _refreshers:
        await fn(db)


def render_metrics() -> bytes:
    return generate_latest()


__all__ = [
    "CONTENT_TYPE_LATEST",
    "GaugeRefresher",
    "refresh_system_gauges",
    "register_gauge_refresher",
    "render_metrics",
]
