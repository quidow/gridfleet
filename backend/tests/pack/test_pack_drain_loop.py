import asyncio
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from unittest.mock import AsyncMock, Mock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.packs.models import DriverPack, PackState
from app.packs.services import drain as pack_drain
from app.packs.services.drain import complete_draining_packs_once

pytestmark = pytest.mark.asyncio


async def test_complete_draining_packs_once_disables_empty_draining_pack(db_session: AsyncSession) -> None:
    pack = DriverPack(
        id="draining-pack",
        origin="uploaded",
        display_name="Draining Pack",
        maintainer="tests",
        license="Apache-2.0",
        state=PackState.draining,
    )
    db_session.add(pack)
    await db_session.commit()

    changed = await complete_draining_packs_once(db_session)

    assert changed == ["draining-pack"]
    refreshed = await db_session.get(DriverPack, "draining-pack")
    assert refreshed is not None
    assert refreshed.state == PackState.disabled


async def test_pack_drain_loop_runs_one_logged_cycle() -> None:
    @asynccontextmanager
    async def cycle() -> AsyncIterator[None]:
        yield

    class Observation:
        def cycle(self) -> AbstractAsyncContextManager[None]:
            return cycle()

    class SessionScope:
        async def __aenter__(self) -> object:
            return object()

        async def __aexit__(self, *args: object) -> None:
            return None

    with (
        patch("app.packs.services.drain.observe_background_loop", new=Mock(return_value=Observation())),
        patch("app.packs.services.drain.async_session", new=Mock(return_value=SessionScope())),
        patch("app.packs.services.drain.complete_draining_packs_once", new=AsyncMock(return_value=["pack-a"])),
        patch("app.packs.services.drain.logger.info") as info,
        patch("app.packs.services.drain.asyncio.sleep", new=AsyncMock(side_effect=asyncio.CancelledError)),
        pytest.raises(asyncio.CancelledError),
    ):
        await pack_drain.pack_drain_loop()

    info.assert_called_once_with("Completed draining driver packs: %s", "pack-a")
