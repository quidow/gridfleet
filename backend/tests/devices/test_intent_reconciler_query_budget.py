from __future__ import annotations

import contextlib
import uuid
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import event

from app.devices.services.intent_reconciler import ReconcileCandidate, reconcile_device_command
from app.devices.services.readiness import load_packs_by_ids
from tests.helpers import seed_host_and_running_node
from tests.helpers import test_event_bus as event_bus

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

pytestmark = [pytest.mark.db, pytest.mark.usefixtures("seeded_driver_packs")]


@contextlib.contextmanager
def capture_sql(engine: AsyncEngine) -> Iterator[list[str]]:
    statements: list[str] = []

    def listener(
        conn: object,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        statements.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", listener)
    try:
        yield statements
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", listener)


async def test_steady_reconcile_has_three_reads(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _host, device, _node = await seed_host_and_running_node(
        db_session,
        identity=f"reconcile-budget-{uuid.uuid4().hex[:8]}",
    )
    await db_session.commit()
    async with db_session_maker() as catalog_db:
        packs = await load_packs_by_ids(catalog_db, [device.pack_id])
        for pack in packs.values():
            catalog_db.expunge(pack)

    candidate = ReconcileCandidate(device.id, delete_expired_intents=False, clear_elapsed_cooldown=False)
    await reconcile_device_command(db_session_maker, candidate, publisher=event_bus, packs=packs)
    engine = db_session_maker.kw["bind"]
    with capture_sql(engine) as statements:
        result = await reconcile_device_command(db_session_maker, candidate, publisher=event_bus, packs=packs)

    reads = [sql for sql in statements if sql.lstrip().upper().startswith(("SELECT", "WITH"))]
    assert result.changed is False
    assert len(reads) == 3, reads
