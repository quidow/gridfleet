from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import pytest

from app.devices.services.intent_reconciler import ReconcileCandidate, reconcile_device_command
from app.packs.services.catalog_view import load_pack_catalog
from tests.concurrency.group_lock_helpers import pin_statement_listener
from tests.fakes.session_factory import RecordingSessionFactory
from tests.helpers import seed_host_and_running_node
from tests.helpers import test_event_bus as event_bus

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

pytestmark = [pytest.mark.db, pytest.mark.usefixtures("seeded_driver_packs")]


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
        packs = await load_pack_catalog(catalog_db, [device.pack_id])

    candidate = ReconcileCandidate(device.id, delete_expired_intents=False, clear_elapsed_cooldown=False)
    await reconcile_device_command(db_session_maker, candidate, publisher=event_bus, packs=packs)
    recorder = RecordingSessionFactory(db_session_maker, statement_pinner=pin_statement_listener)
    try:
        result = await reconcile_device_command(recorder, candidate, publisher=event_bus, packs=packs)
        statements = recorder.statements_for(0)
    finally:
        recorder.close()

    reads = [sql for sql in statements if sql.startswith(("select", "with"))]
    assert result.changed is False
    assert len(reads) == 3, reads
