from __future__ import annotations

import asyncio
import signal
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock

from app import main

if TYPE_CHECKING:
    from collections.abc import Coroutine

    from pytest import MonkeyPatch


class FakeLoop:
    def __init__(self) -> None:
        self.callbacks: dict[signal.Signals, object] = {}
        self.removed: list[signal.Signals] = []

    def add_signal_handler(self, signum: signal.Signals, callback: object) -> None:
        self.callbacks[signum] = callback

    def remove_signal_handler(self, signum: signal.Signals) -> None:
        self.removed.append(signum)


class FakeSessionFactory:
    def __init__(self, db: object) -> None:
        self._db = db

    def __call__(self) -> FakeSessionFactory:
        return self

    async def __aenter__(self) -> object:
        return self._db

    async def __aexit__(self, _exc_type: object, _exc: object, _tb: object) -> bool:
        return False


async def _forever() -> None:
    await asyncio.Event().wait()


def _setting_value(key: str) -> int:
    values = {
        "appium.reservation_ttl_sec": 120,
        "appium.startup_timeout_sec": 30,
    }
    return values[key]


def _patch_agent_http_pool(monkeypatch: MonkeyPatch) -> tuple[AsyncMock, AsyncMock]:
    import app.services.agent_http_pool as agent_http_pool_module

    reopen = AsyncMock()
    close = AsyncMock()
    monkeypatch.setattr(agent_http_pool_module.agent_http_pool, "reopen", reopen)
    monkeypatch.setattr(agent_http_pool_module.agent_http_pool, "close", close)
    return reopen, close


async def test_lifespan_starts_and_cleans_up_background_tasks(monkeypatch: MonkeyPatch) -> None:
    db = AsyncMock()
    session_factory = FakeSessionFactory(db)
    loop = FakeLoop()
    engine = SimpleNamespace(dispose=AsyncMock())
    created_tasks: list[asyncio.Task[None]] = []
    real_create_task = asyncio.create_task

    def tracking_create_task(coro: Coroutine[object, object, None]) -> asyncio.Task[None]:
        task = real_create_task(coro)
        created_tasks.append(task)
        return task

    import app.database as database_module
    import app.services.event_bus as event_bus_module
    import app.services.settings_service as settings_service_module

    pool_reopen, pool_close = _patch_agent_http_pool(monkeypatch)
    monkeypatch.setattr(database_module, "async_session", session_factory)
    monkeypatch.setattr(event_bus_module.event_bus, "configure", Mock())
    monkeypatch.setattr(event_bus_module.event_bus, "register_handler", Mock())
    monkeypatch.setattr(event_bus_module.event_bus, "start", AsyncMock())
    monkeypatch.setattr(event_bus_module.event_bus, "shutdown", AsyncMock())
    monkeypatch.setattr(settings_service_module.settings_service, "configure_store_refresh", Mock())
    monkeypatch.setattr(settings_service_module.settings_service, "initialize", AsyncMock())
    monkeypatch.setattr(settings_service_module.settings_service, "get", Mock(side_effect=_setting_value))
    monkeypatch.setattr(settings_service_module.settings_service, "shutdown", AsyncMock())
    monkeypatch.setattr(settings_service_module.settings_service, "handle_system_event", AsyncMock())
    monkeypatch.setattr(main.webhook_dispatcher, "configure", Mock())
    monkeypatch.setattr(main.webhook_dispatcher, "handle_system_event", AsyncMock())
    monkeypatch.setattr(main.webhook_dispatcher, "webhook_delivery_loop", lambda session_factory: _forever())
    monkeypatch.setattr(main.shutdown_coordinator, "reset", Mock())
    monkeypatch.setattr(main.shutdown_coordinator, "begin_shutdown", AsyncMock())
    monkeypatch.setattr(main.shutdown_coordinator, "wait_for_drain", AsyncMock())
    monkeypatch.setattr(main.control_plane_leader, "try_acquire", AsyncMock(return_value=True))
    monkeypatch.setattr(main.control_plane_leader, "release", AsyncMock())
    monkeypatch.setattr(main, "shutdown_background_tasks", AsyncMock())
    monkeypatch.setattr(main, "engine", engine)
    monkeypatch.setattr(main.asyncio, "get_running_loop", lambda: loop)
    monkeypatch.setattr(main.asyncio, "create_task", tracking_create_task)
    monkeypatch.setattr(main, "heartbeat_loop", _forever)
    monkeypatch.setattr(main, "session_sync_loop", _forever)
    monkeypatch.setattr(main, "node_health_loop", _forever)
    monkeypatch.setattr(main, "device_connectivity_loop", _forever)
    monkeypatch.setattr(main, "property_refresh_loop", _forever)
    monkeypatch.setattr(main, "hardware_telemetry_loop", _forever)
    monkeypatch.setattr(main, "host_resource_telemetry_loop", _forever)
    monkeypatch.setattr(main, "durable_job_worker_loop", lambda session_factory: _forever())
    monkeypatch.setattr(main, "run_reaper_loop", _forever)
    monkeypatch.setattr(main, "data_cleanup_loop", _forever)
    monkeypatch.setattr(main, "session_viability_loop", _forever)
    monkeypatch.setattr(main, "fleet_capacity_collector_loop", _forever)
    monkeypatch.setattr(main, "pack_drain_loop", _forever)
    monkeypatch.setattr(main, "appium_resource_sweeper_loop", _forever)

    async with main.lifespan(main.app):
        assert len(created_tasks) == 15
        loop.callbacks[signal.SIGTERM]()
        await asyncio.sleep(0)

    assert settings_service_module.settings_service.initialize.await_count == 1
    assert pool_reopen.await_count == 1
    assert pool_close.await_count == 1
    assert len(loop.removed) == 2
    assert all(task.done() for task in created_tasks)


async def test_lifespan_skips_background_tasks_when_not_control_plane_leader(monkeypatch: MonkeyPatch) -> None:
    db = AsyncMock()
    session_factory = FakeSessionFactory(db)
    loop = FakeLoop()
    engine = SimpleNamespace(dispose=AsyncMock())
    create_task = Mock(side_effect=asyncio.create_task)

    import app.database as database_module
    import app.services.event_bus as event_bus_module
    import app.services.settings_service as settings_service_module

    pool_reopen, pool_close = _patch_agent_http_pool(monkeypatch)
    monkeypatch.setattr(database_module, "async_session", session_factory)
    monkeypatch.setattr(event_bus_module.event_bus, "configure", Mock())
    monkeypatch.setattr(event_bus_module.event_bus, "register_handler", Mock())
    monkeypatch.setattr(event_bus_module.event_bus, "start", AsyncMock())
    monkeypatch.setattr(event_bus_module.event_bus, "shutdown", AsyncMock())
    monkeypatch.setattr(settings_service_module.settings_service, "configure_store_refresh", Mock())
    monkeypatch.setattr(settings_service_module.settings_service, "initialize", AsyncMock())
    monkeypatch.setattr(settings_service_module.settings_service, "get", Mock(side_effect=_setting_value))
    monkeypatch.setattr(settings_service_module.settings_service, "shutdown", AsyncMock())
    monkeypatch.setattr(settings_service_module.settings_service, "handle_system_event", AsyncMock())
    monkeypatch.setattr(main.webhook_dispatcher, "configure", Mock())
    monkeypatch.setattr(main.webhook_dispatcher, "handle_system_event", AsyncMock())
    monkeypatch.setattr(main.shutdown_coordinator, "reset", Mock())
    monkeypatch.setattr(main.shutdown_coordinator, "begin_shutdown", AsyncMock())
    monkeypatch.setattr(main.shutdown_coordinator, "wait_for_drain", AsyncMock())
    monkeypatch.setattr(main.control_plane_leader, "try_acquire", AsyncMock(return_value=False))
    monkeypatch.setattr(main.control_plane_leader, "release", AsyncMock())
    monkeypatch.setattr(main, "shutdown_background_tasks", AsyncMock())
    monkeypatch.setattr(main, "engine", engine)
    monkeypatch.setattr(main.asyncio, "get_running_loop", lambda: loop)
    monkeypatch.setattr(main.asyncio, "create_task", create_task)

    async with main.lifespan(main.app):
        pass

    assert create_task.call_count == 0
    assert pool_reopen.await_count == 1
    assert pool_close.await_count == 1


async def test_lifespan_skips_background_tasks_when_freeze_flag_set(monkeypatch: MonkeyPatch) -> None:
    db = AsyncMock()
    session_factory = FakeSessionFactory(db)
    loop = FakeLoop()
    engine = SimpleNamespace(dispose=AsyncMock())
    create_task = Mock(side_effect=asyncio.create_task)
    try_acquire = AsyncMock(return_value=True)

    import app.database as database_module
    import app.services.event_bus as event_bus_module
    import app.services.settings_service as settings_service_module

    monkeypatch.setenv("GRIDFLEET_FREEZE_BACKGROUND_LOOPS", "1")
    pool_reopen, pool_close = _patch_agent_http_pool(monkeypatch)
    monkeypatch.setattr(database_module, "async_session", session_factory)
    monkeypatch.setattr(event_bus_module.event_bus, "configure", Mock())
    monkeypatch.setattr(event_bus_module.event_bus, "register_handler", Mock())
    monkeypatch.setattr(event_bus_module.event_bus, "start", AsyncMock())
    monkeypatch.setattr(event_bus_module.event_bus, "shutdown", AsyncMock())
    monkeypatch.setattr(settings_service_module.settings_service, "configure_store_refresh", Mock())
    monkeypatch.setattr(settings_service_module.settings_service, "initialize", AsyncMock())
    monkeypatch.setattr(settings_service_module.settings_service, "get", Mock(side_effect=_setting_value))
    monkeypatch.setattr(settings_service_module.settings_service, "shutdown", AsyncMock())
    monkeypatch.setattr(settings_service_module.settings_service, "handle_system_event", AsyncMock())
    monkeypatch.setattr(main.webhook_dispatcher, "configure", Mock())
    monkeypatch.setattr(main.webhook_dispatcher, "handle_system_event", AsyncMock())
    monkeypatch.setattr(main.shutdown_coordinator, "reset", Mock())
    monkeypatch.setattr(main.shutdown_coordinator, "begin_shutdown", AsyncMock())
    monkeypatch.setattr(main.shutdown_coordinator, "wait_for_drain", AsyncMock())
    monkeypatch.setattr(main.control_plane_leader, "try_acquire", try_acquire)
    monkeypatch.setattr(main.control_plane_leader, "release", AsyncMock())
    monkeypatch.setattr(main, "shutdown_background_tasks", AsyncMock())
    monkeypatch.setattr(main, "engine", engine)
    monkeypatch.setattr(main.asyncio, "get_running_loop", lambda: loop)
    monkeypatch.setattr(main.asyncio, "create_task", create_task)

    async with main.lifespan(main.app):
        pass

    assert create_task.call_count == 0
    assert try_acquire.await_count == 0
    assert pool_reopen.await_count == 1
    assert pool_close.await_count == 1
