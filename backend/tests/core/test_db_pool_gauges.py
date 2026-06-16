from __future__ import annotations

from app.core.database import _refresh_db_pool_gauges, engine
from app.core.metrics_recorders import DB_POOL_CHECKED_OUT, DB_POOL_OVERFLOW, DB_POOL_SIZE


async def test_db_pool_gauges_mirror_engine_pool() -> None:
    await _refresh_db_pool_gauges(None)  # type: ignore[arg-type]
    pool = engine.pool
    assert DB_POOL_SIZE._value.get() == pool.size()  # type: ignore[attr-defined]
    assert DB_POOL_CHECKED_OUT._value.get() == pool.checkedout()  # type: ignore[attr-defined]
    assert DB_POOL_OVERFLOW._value.get() == pool.overflow()  # type: ignore[attr-defined]
