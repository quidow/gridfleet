from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy.exc import NoResultFound

from app.appium_nodes.services import node_health as node_health
from app.core.leader import keepalive as control_plane_leader_keepalive
from app.core.leader.advisory import LeadershipLost
from app.devices.services import (
    connectivity as device_connectivity,
)
from app.devices.services import (
    data_cleanup as data_cleanup,
)
from app.devices.services import (
    intent_reconciler as intent_reconciler,
)
from app.runs import service_reaper as run_reaper

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


class _Observation:
    @asynccontextmanager
    async def cycle(self) -> AsyncGenerator[AsyncMock, None]:
        yield AsyncMock()


@asynccontextmanager
async def _fake_session() -> AsyncGenerator[AsyncMock, None]:
    yield AsyncMock()


async def test_intent_reconciler_loop_exits_on_leadership_loss(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(intent_reconciler.settings_service, "get", lambda _key: 1)
    monkeypatch.setattr(intent_reconciler, "observe_background_loop", Mock(return_value=_Observation()))
    monkeypatch.setattr(intent_reconciler, "async_session", _fake_session)
    monkeypatch.setattr(
        intent_reconciler,
        "run_device_intent_reconciler_once",
        AsyncMock(side_effect=LeadershipLost("stale leader")),
    )
    monkeypatch.setattr(intent_reconciler.os, "_exit", Mock(side_effect=SystemExit(70)))

    with pytest.raises(SystemExit):
        await intent_reconciler.device_intent_reconciler_loop()

    intent_reconciler.os._exit.assert_called_once_with(70)


async def test_intent_reconciler_loop_logs_cycle_failure_and_sleeps(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(intent_reconciler.settings_service, "get", lambda _key: 1)
    monkeypatch.setattr(intent_reconciler, "observe_background_loop", Mock(return_value=_Observation()))
    monkeypatch.setattr(intent_reconciler, "async_session", _fake_session)
    monkeypatch.setattr(
        intent_reconciler,
        "run_device_intent_reconciler_once",
        AsyncMock(side_effect=RuntimeError("boom")),
    )
    sleep = AsyncMock(side_effect=asyncio.CancelledError())
    monkeypatch.setattr(intent_reconciler.asyncio, "sleep", sleep)

    with pytest.raises(asyncio.CancelledError):
        await intent_reconciler.device_intent_reconciler_loop()

    sleep.assert_awaited_once_with(1)


async def test_node_health_loop_exits_on_leadership_loss(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(node_health.settings_service, "get", lambda _key: 1)
    monkeypatch.setattr(node_health, "observe_background_loop", Mock(return_value=_Observation()))
    monkeypatch.setattr(node_health, "async_session", _fake_session)
    monkeypatch.setattr(node_health, "_check_nodes", AsyncMock(side_effect=LeadershipLost("stale leader")))
    monkeypatch.setattr(node_health.os, "_exit", Mock(side_effect=SystemExit(70)))

    with pytest.raises(SystemExit):
        await node_health.node_health_loop()

    node_health.os._exit.assert_called_once_with(70)


async def test_node_health_check_skips_device_deleted_after_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    device = Mock(id=__import__("uuid").uuid4(), host_id=__import__("uuid").uuid4())
    node = Mock(device=device, port=4723, pid=123, active_connection_target="serial")

    class Result:
        def scalars(self) -> Result:
            return self

        def all(self) -> list[object]:
            return [node]

    db = AsyncMock()
    db.execute = AsyncMock(return_value=Result())
    db.commit = AsyncMock()
    monkeypatch.setattr(node_health, "_bounded_check_node_health", AsyncMock(return_value={"healthy": True}))
    monkeypatch.setattr(node_health.grid_service, "get_grid_status", AsyncMock(return_value={}))
    monkeypatch.setattr(node_health.grid_service, "available_node_device_ids", Mock(return_value=set()))
    monkeypatch.setattr(node_health, "assert_current_leader", AsyncMock())
    monkeypatch.setattr(node_health.device_locking, "lock_device", AsyncMock(side_effect=NoResultFound))

    await node_health._check_nodes(db)

    db.commit.assert_awaited_once()


async def test_device_connectivity_loop_exits_on_leadership_loss(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(device_connectivity.settings_service, "get", lambda _key: 1)
    monkeypatch.setattr(device_connectivity, "observe_background_loop", Mock(return_value=_Observation()))
    monkeypatch.setattr(device_connectivity, "async_session", _fake_session)
    monkeypatch.setattr(device_connectivity, "_check_expired_cooldowns", AsyncMock())
    monkeypatch.setattr(
        device_connectivity,
        "_check_connectivity",
        AsyncMock(side_effect=LeadershipLost("stale leader")),
    )
    monkeypatch.setattr(device_connectivity.os, "_exit", Mock(side_effect=SystemExit(70)))

    with pytest.raises(SystemExit):
        await device_connectivity.device_connectivity_loop()

    device_connectivity.os._exit.assert_called_once_with(70)


async def test_run_reaper_loop_exits_on_initial_leadership_loss(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(run_reaper.settings_service, "get", lambda _key: 1)
    monkeypatch.setattr(run_reaper, "observe_background_loop", Mock(return_value=_Observation()))
    monkeypatch.setattr(run_reaper, "async_session", _fake_session)
    monkeypatch.setattr(run_reaper, "_reap_stale_runs", AsyncMock(side_effect=LeadershipLost("stale leader")))
    monkeypatch.setattr(run_reaper.os, "_exit", Mock(side_effect=SystemExit(70)))

    with pytest.raises(SystemExit):
        await run_reaper.run_reaper_loop()

    run_reaper.os._exit.assert_called_once_with(70)


async def test_run_reaper_loop_exits_on_repeated_leadership_loss(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(run_reaper.settings_service, "get", lambda _key: 1)
    monkeypatch.setattr(run_reaper, "observe_background_loop", Mock(return_value=_Observation()))
    monkeypatch.setattr(run_reaper, "async_session", _fake_session)
    monkeypatch.setattr(
        run_reaper,
        "_reap_stale_runs",
        AsyncMock(side_effect=[None, LeadershipLost("stale leader")]),
    )
    monkeypatch.setattr(run_reaper.asyncio, "sleep", AsyncMock(return_value=None))
    monkeypatch.setattr(run_reaper.os, "_exit", Mock(side_effect=SystemExit(70)))

    with pytest.raises(SystemExit):
        await run_reaper.run_reaper_loop()

    run_reaper.os._exit.assert_called_once_with(70)


async def test_data_cleanup_loop_logs_failure_and_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(data_cleanup.settings_service, "get", lambda _key: 1)
    monkeypatch.setattr(data_cleanup, "schedule_background_loop", AsyncMock())
    monkeypatch.setattr(data_cleanup, "observe_background_loop", Mock(return_value=_Observation()))
    monkeypatch.setattr(data_cleanup, "async_session", _fake_session)
    monkeypatch.setattr(data_cleanup, "_cleanup_old_data", AsyncMock(side_effect=RuntimeError("boom")))
    sleep = AsyncMock(side_effect=[None, asyncio.CancelledError()])
    monkeypatch.setattr(data_cleanup.asyncio, "sleep", sleep)

    with pytest.raises(asyncio.CancelledError):
        await data_cleanup.data_cleanup_loop()

    data_cleanup.schedule_background_loop.assert_awaited_once_with(data_cleanup.LOOP_NAME, 3600.0)
    sleep.assert_any_await(3600.0)


async def test_control_plane_leader_keepalive_loop_exits_on_leadership_loss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(control_plane_leader_keepalive, "_setting", lambda _key: 1)
    monkeypatch.setattr(control_plane_leader_keepalive, "observe_background_loop", Mock(return_value=_Observation()))
    monkeypatch.setattr(
        control_plane_leader_keepalive,
        "run_keepalive_once",
        AsyncMock(side_effect=LeadershipLost("stale leader")),
    )
    monkeypatch.setattr(control_plane_leader_keepalive.os, "_exit", Mock(side_effect=SystemExit(70)))

    with pytest.raises(SystemExit):
        await control_plane_leader_keepalive.control_plane_leader_keepalive_loop()

    control_plane_leader_keepalive.os._exit.assert_called_once_with(70)
