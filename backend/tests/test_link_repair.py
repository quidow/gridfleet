from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from app.core.leader import state_store
from app.devices.services.link_repair import (
    REPAIR_ATTEMPTS_NAMESPACE,
    REPAIR_MAX_ATTEMPTS,
    next_repair_attempt,
    reset_repair_attempts,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


@pytest.mark.db
@pytest.mark.asyncio
async def test_attempt_budget_increments_then_exhausts(db_session: AsyncSession) -> None:
    identity = "192.168.1.254:5555"
    seen = []
    for _ in range(REPAIR_MAX_ATTEMPTS + 1):
        seen.append(await next_repair_attempt(db_session, identity))
    # First REPAIR_MAX_ATTEMPTS return an attempt number; the last returns None (exhausted).
    assert seen[:REPAIR_MAX_ATTEMPTS] == list(range(1, REPAIR_MAX_ATTEMPTS + 1))
    assert seen[REPAIR_MAX_ATTEMPTS] is None


@pytest.mark.db
@pytest.mark.asyncio
async def test_reset_clears_counter(db_session: AsyncSession) -> None:
    identity = "192.168.1.254:5555"
    await next_repair_attempt(db_session, identity)
    await reset_repair_attempts(db_session, identity)
    assert await state_store.get_value(db_session, REPAIR_ATTEMPTS_NAMESPACE, identity) is None
    assert await next_repair_attempt(db_session, identity) == 1
