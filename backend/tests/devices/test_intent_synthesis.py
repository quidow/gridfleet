from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from app.core.timeutil import now_utc
from app.devices.services.intent_synthesis import synthesize_fact_intents
from tests.helpers import create_device

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.hosts.models import Host


@pytest.mark.db
async def test_synthesis_is_empty_for_plain_device(db_session: AsyncSession, db_host: Host) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="synth-empty")
    # ``node`` is unused by every synthesis family; the real caller always passes a
    # live node (the ``node is not None`` branch of ``reconcile_device``).
    intents = await synthesize_fact_intents(db_session, device, None, [], now_utc())
    assert intents == []
