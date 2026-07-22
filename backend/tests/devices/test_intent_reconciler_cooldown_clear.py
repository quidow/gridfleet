from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.appium_nodes.models import AppiumDesiredState, AppiumNode
from app.devices.services import intent_reconciler
from app.devices.services.intent_reconciler import ReconcileCandidate, reconcile_device_command
from tests.helpers import create_device
from tests.helpers import test_event_bus as event_bus

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.hosts.models import Host

pytestmark = pytest.mark.db


async def test_cooldown_clear_failure_does_not_block_device_fold(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="cooldown-clear-failure")
    db_session.add(AppiumNode(device_id=device.id, port=4723, desired_state=AppiumDesiredState.stopped))
    await db_session.commit()
    clear = AsyncMock(side_effect=RuntimeError("boom"))
    monkeypatch.setattr(intent_reconciler, "_clear_elapsed_cooldown_for_locked_device", clear)

    await reconcile_device_command(
        db_session_maker,
        ReconcileCandidate(device.id, delete_expired_intents=False, clear_elapsed_cooldown=True),
        publisher=event_bus,
        packs={},
    )

    clear.assert_awaited_once()
    async with db_session_maker() as verify:
        node = (await verify.execute(select(AppiumNode).where(AppiumNode.device_id == device.id))).scalar_one()
        assert node.desired_state is AppiumDesiredState.running
