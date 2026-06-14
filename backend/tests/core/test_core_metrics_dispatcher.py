"""Unit tests for app.core.metrics dispatcher.

The dispatcher is a process-global list. Reset it between tests so
state from one test does not leak into the next.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

import app.core.metrics as core_metrics

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy.ext.asyncio import AsyncSession


@pytest.fixture(autouse=True)
def _reset_refreshers() -> Iterator[None]:
    saved = list(core_metrics._refreshers)
    core_metrics._refreshers.clear()
    yield
    core_metrics._refreshers.clear()
    core_metrics._refreshers.extend(saved)


async def test_refresh_system_gauges_is_noop_when_no_refreshers_registered() -> None:
    await core_metrics.refresh_system_gauges(db=None)  # type: ignore[arg-type]


async def test_register_gauge_refresher_invokes_callback_on_refresh() -> None:
    calls: list[object] = []

    async def cb(db: AsyncSession) -> None:
        calls.append(db)

    core_metrics.register_gauge_refresher(cb)

    sentinel = object()
    await core_metrics.refresh_system_gauges(db=sentinel)  # type: ignore[arg-type]

    assert calls == [sentinel]


async def test_register_gauge_refresher_runs_callbacks_in_order() -> None:
    order: list[int] = []

    def make_cb(n: int) -> core_metrics.GaugeRefresher:
        async def cb(_db: AsyncSession) -> None:
            order.append(n)

        return cb

    core_metrics.register_gauge_refresher(make_cb(1))
    core_metrics.register_gauge_refresher(make_cb(2))
    core_metrics.register_gauge_refresher(make_cb(3))

    await core_metrics.refresh_system_gauges(db=None)  # type: ignore[arg-type]
    assert order == [1, 2, 3]


async def test_register_gauge_refresher_propagates_callback_errors() -> None:
    async def cb(_db: AsyncSession) -> None:
        raise RuntimeError("boom")

    core_metrics.register_gauge_refresher(cb)

    with pytest.raises(RuntimeError, match="boom"):
        await core_metrics.refresh_system_gauges(db=None)  # type: ignore[arg-type]
