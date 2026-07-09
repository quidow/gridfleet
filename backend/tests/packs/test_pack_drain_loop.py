import asyncio
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock, patch

import pytest

from app.packs.models import DriverPack, PackState
from app.packs.services import drain as pack_drain
from app.packs.services.drain import PackDrainLoop
from app.packs.services.lifecycle import PackLifecycleService
from app.packs.services_container import PackServices

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.asyncio


@asynccontextmanager
async def _cycle() -> AsyncIterator[None]:
    yield


class _Observation:
    def cycle(self) -> AbstractAsyncContextManager[None]:
        return _cycle()


async def test_complete_draining_packs_once_disables_empty_draining_pack(db_session: AsyncSession) -> None:
    pack = DriverPack(
        id="draining-pack",
        display_name="Draining Pack",
        maintainer="tests",
        license="Apache-2.0",
        state=PackState.draining,
    )
    db_session.add(pack)
    await db_session.commit()

    loop = PackDrainLoop(
        services=PackServices(
            catalog=Mock(),
            release=Mock(),
            status=Mock(),
            lifecycle=PackLifecycleService(),
            discovery=Mock(),
            storage=Mock(),
            session_factory=Mock(),
        )
    )
    changed = await loop._complete_draining_packs_once(db_session)

    assert changed == ["draining-pack"]
    refreshed = await db_session.get(DriverPack, "draining-pack")
    assert refreshed is not None
    assert refreshed.state == PackState.disabled


async def test_pack_drain_loop_runs_one_logged_cycle() -> None:
    class SessionScope:
        async def __aenter__(self) -> object:
            return object()

        async def __aexit__(self, *args: object) -> None:
            return None

    with (
        patch("app.core.background_loop.observe_background_loop", new=Mock(return_value=_Observation())),
        patch.object(pack_drain.PackDrainLoop, "_complete_draining_packs_once", new=AsyncMock(return_value=["pack-a"])),
        patch("app.packs.services.drain.logger.info") as info,
        patch("app.core.background_loop.asyncio.sleep", new=AsyncMock(side_effect=asyncio.CancelledError)),
        pytest.raises(asyncio.CancelledError),
    ):
        loop = pack_drain.PackDrainLoop(
            services=PackServices(
                catalog=Mock(),
                release=Mock(),
                status=Mock(),
                lifecycle=Mock(),
                discovery=Mock(),
                storage=Mock(),
                session_factory=SessionScope,
            )
        )
        await loop.run()

    info.assert_called_once_with("Completed draining driver packs: %s", "pack-a")


async def test_pack_drain_cycle_failure_does_not_kill_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    class _SessionScope:
        async def __aenter__(self) -> object:
            return object()

        async def __aexit__(self, *args: object) -> None:
            return None

    services = PackServices(
        catalog=Mock(),
        release=Mock(),
        status=Mock(),
        lifecycle=Mock(),
        discovery=Mock(),
        storage=Mock(),
        session_factory=_SessionScope,
    )
    loop = PackDrainLoop(services=services)
    calls: dict[str, int] = {"n": 0}

    async def _boom(db: object) -> None:
        calls["n"] += 1
        raise RuntimeError("cycle blew up")

    monkeypatch.setattr(loop, "_complete_draining_packs_once", _boom)
    monkeypatch.setattr(loop, "_interval", lambda: 0.0)  # avoid real POLL_INTERVAL_SEC sleep
    monkeypatch.setattr("app.core.background_loop.observe_background_loop", Mock(return_value=_Observation()))
    task = asyncio.create_task(loop.run())
    try:
        for _ in range(200):
            if calls["n"] >= 2:
                break
            await asyncio.sleep(0)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert calls["n"] >= 2  # loop survived the first failure
