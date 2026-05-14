"""Verify wipe_all_tables truncates every ORM-registered table."""

import pytest
from sqlalchemy import select

from app.core.database import Base
from app.hosts.models import Host, OSType
from app.seeding.runner import wipe_all_tables


@pytest.mark.asyncio
async def test_wipe_all_tables_removes_rows(db_session) -> None:  # noqa: ANN001
    host = Host(hostname="pre-wipe", ip="10.0.0.1", os_type=OSType.linux)
    db_session.add(host)
    await db_session.commit()

    result = await db_session.execute(select(Host))
    assert result.scalars().first() is not None

    await wipe_all_tables(db_session, table_names=sorted(Base.metadata.tables.keys()))
    await db_session.commit()

    result = await db_session.execute(select(Host))
    assert result.scalars().first() is None
