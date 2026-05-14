from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from sqlalchemy import select

from app.hosts.models import Host
from app.seeding.runner import SeedResult, run_scenario

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@pytest.mark.asyncio
async def test_run_scenario_wipes_then_seeds(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
) -> None:
    # Pre-seed some data that should be wiped.
    from app.hosts.models import OSType

    db_session.add(Host(hostname="pre-existing", ip="1.1.1.1", os_type=OSType.linux))
    await db_session.commit()

    result: SeedResult = await run_scenario(
        session_factory=db_session_maker,
        scenario="minimal",
        seed=42,
        wipe=True,
        skip_telemetry=True,
    )
    assert result.scenario == "minimal"
    assert result.rows_written > 0
    assert "hosts" in result.row_counts

    # Verify the pre-existing host is gone and the scenario host is present.
    hosts = (await db_session.execute(select(Host))).scalars().all()
    assert len(hosts) == 1
    assert hosts[0].hostname == "lab-linux-01"
