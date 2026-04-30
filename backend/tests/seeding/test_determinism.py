"""Seeding is deterministic: same seed → identical row counts + key sets."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from sqlalchemy import delete, select

from app.models.device import Device
from app.models.host import Host
from app.models.test_run import TestRun
from app.seeding.context import SeedContext
from app.seeding.scenarios.minimal import apply_minimal

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


async def _snapshot(
    session_maker: async_sessionmaker[AsyncSession],
) -> tuple[list[str], list[str], list[str]]:
    """Create and return a snapshot of seeded data: (hosts, devices, runs)."""
    async with session_maker() as session:
        await session.execute(delete(TestRun))
        await session.execute(delete(Device))
        await session.execute(delete(Host))
        await session.commit()
        ctx = SeedContext.build(session=session, seed=42)
        await apply_minimal(ctx)
        await session.commit()
        hosts = [h.hostname for h in (await session.execute(select(Host))).scalars()]
        devices = [d.identity_value for d in (await session.execute(select(Device))).scalars()]
        runs = [r.name for r in (await session.execute(select(TestRun))).scalars()]
        return sorted(hosts), sorted(devices), sorted(runs)


@pytest.mark.asyncio
async def test_minimal_snapshot_is_deterministic(
    db_session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Same seed produces identical snapshots in independent schemas."""
    first = await _snapshot(db_session_maker)
    second = await _snapshot(db_session_maker)
    assert first == second
