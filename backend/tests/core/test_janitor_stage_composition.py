"""The janitor's composed stage closures, end to end.

``_build_janitor`` wires each stage as a closure over ``AppServices``. The
pack-drain stage in particular opens its own ``begin()`` inside the closure --
the only stage that does -- and nothing exercised that composition, so a
rewiring that dropped the boundary or called the wrong service would have
shipped green.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

from app.agent_comm.circuit_breaker import AgentCircuitBreaker
from app.agent_comm.http_pool import AgentHttpPool
from app.composition import AppServices, compose_app
from app.events.event_bus import EventBus
from app.main import _build_janitor
from app.settings.service import SettingsService

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

pytestmark = pytest.mark.db


@pytest.fixture
def app_services(db_session_maker: async_sessionmaker[AsyncSession]) -> AppServices:
    return compose_app(
        session_factory=db_session_maker,
        bus=EventBus(),
        settings_svc=SettingsService(),
        http_pool=AgentHttpPool(),
        circuit_breaker=AgentCircuitBreaker(publisher=AsyncMock()),
    )


async def test_pack_drain_stage_runs_the_lifecycle_call_inside_a_transaction(
    app_services: AppServices, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[bool] = []

    async def _record(db: AsyncSession) -> None:
        seen.append(db.in_transaction())

    monkeypatch.setattr(app_services.packs.lifecycle, "complete_draining_packs_once", AsyncMock(side_effect=_record))

    janitor = _build_janitor(app_services)
    # JanitorLoop exposes no public accessor for its stages (tests/core/test_janitor.py
    # already reaches into other private members of this same class).
    stage = next(entry for entry in janitor._stages if entry.name == "pack_drain")
    await stage.run(db_session)

    assert seen == [True], "the pack-drain stage must call the lifecycle inside its own transaction"


def test_every_janitor_stage_is_named_and_has_a_cadence(app_services: AppServices) -> None:
    """A stage with no interval never runs; a duplicate name shadows its twin."""
    janitor = _build_janitor(app_services)
    names = [stage.name for stage in janitor._stages]
    assert names == sorted(set(names), key=names.index), f"duplicate janitor stage name in {names}"
    assert all(stage.interval_sec > 0 for stage in janitor._stages)
