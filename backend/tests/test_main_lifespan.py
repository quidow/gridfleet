from __future__ import annotations

import asyncio
import importlib
import signal
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock

from fastapi.responses import JSONResponse, Response
from sqlalchemy import select

from app import main
from app.hosts.models import Host, HostStatus

if TYPE_CHECKING:
    from collections.abc import Coroutine

    from pytest import LogCaptureFixture, MonkeyPatch
    from sqlalchemy.ext.asyncio import AsyncSession


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


def test_main_imports_in_fresh_interpreter() -> None:
    result = subprocess.run(
        [sys.executable, "-c", "import app.main"],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def _setting_value(key: str) -> int:
    values = {
        "appium.startup_timeout_sec": 30,
        "general.leader_keepalive_interval_sec": 5,
        "general.leader_stale_threshold_sec": 30,
    }
    return values[key]


def _patch_agent_http_pool(monkeypatch: MonkeyPatch) -> tuple[AsyncMock, AsyncMock]:
    import app.agent_comm.http_pool as agent_http_pool_module

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

    def tracking_create_task(
        coro: Coroutine[object, object, None],
        *,
        name: str | None = None,
    ) -> asyncio.Task[None]:
        task = real_create_task(coro, name=name)
        created_tasks.append(task)
        return task

    import app.core.database as database_module
    import app.settings.service as settings_service_module

    event_bus_module = importlib.import_module("app.events.event_bus")

    pool_reopen, pool_close = _patch_agent_http_pool(monkeypatch)
    monkeypatch.setattr(database_module, "async_session", session_factory)
    monkeypatch.setattr(main, "session_factory", session_factory)
    monkeypatch.setattr(main, "_validate_online_agent_contracts", AsyncMock())
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
    monkeypatch.setattr(main, "control_plane_leader_keepalive_loop", _forever)
    monkeypatch.setattr(main, "control_plane_leader_watcher_loop", _forever)
    monkeypatch.setattr(main, "heartbeat_loop", _forever)
    monkeypatch.setattr(main, "session_sync_loop", _forever)
    monkeypatch.setattr(main, "node_health_loop", _forever)
    monkeypatch.setattr(main, "device_connectivity_loop", _forever)
    monkeypatch.setattr(main, "property_refresh_loop", _forever)
    monkeypatch.setattr(main, "hardware_telemetry_loop", _forever)
    monkeypatch.setattr(main, "host_resource_telemetry_loop", _forever)
    monkeypatch.setattr(main.job_queue, "durable_job_worker_loop", lambda session_factory: _forever())
    monkeypatch.setattr(main, "run_reaper_loop", _forever)
    monkeypatch.setattr(main, "data_cleanup_loop", _forever)
    monkeypatch.setattr(main, "session_viability_loop", _forever)
    monkeypatch.setattr(main, "fleet_capacity_collector_loop", _forever)
    monkeypatch.setattr(main, "pack_drain_loop", _forever)
    monkeypatch.setattr(main, "appium_reconciler_loop", _forever)
    monkeypatch.setattr(main, "device_intent_reconciler_loop", _forever)

    async with main.lifespan(main.app):
        expected_leader_loop_names = {
            "control_plane_leader_keepalive",
            "heartbeat_loop",
            "session_sync_loop",
            "node_health_loop",
            "device_connectivity_loop",
            "property_refresh_loop",
            "hardware_telemetry_loop",
            "host_resource_telemetry_loop",
            "durable_job_worker_loop",
            "webhook_dispatcher.webhook_delivery_loop",
            "run_reaper_loop",
            "data_cleanup_loop",
            "session_viability_loop",
            "fleet_capacity_collector_loop",
            "pack_drain_loop",
            "appium_reconciler_loop",
            "device_intent_reconciler_loop",
        }
        task_names = {task.get_name() for task in created_tasks}
        assert task_names >= expected_leader_loop_names
        assert "control_plane_leader_watcher" in task_names
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

    import app.core.database as database_module
    import app.settings.service as settings_service_module

    event_bus_module = importlib.import_module("app.events.event_bus")

    pool_reopen, pool_close = _patch_agent_http_pool(monkeypatch)
    monkeypatch.setattr(database_module, "async_session", session_factory)
    monkeypatch.setattr(main, "session_factory", session_factory)
    monkeypatch.setattr(main, "_validate_online_agent_contracts", AsyncMock())
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
    monkeypatch.setattr(main, "control_plane_leader_watcher_loop", _forever)

    async with main.lifespan(main.app):
        pass

    assert create_task.call_count == 1
    assert create_task.call_args.kwargs["name"] == "control_plane_leader_watcher"
    assert pool_reopen.await_count == 1
    assert pool_close.await_count == 1


async def test_lifespan_skips_background_tasks_when_freeze_flag_set(monkeypatch: MonkeyPatch) -> None:
    db = AsyncMock()
    session_factory = FakeSessionFactory(db)
    loop = FakeLoop()
    engine = SimpleNamespace(dispose=AsyncMock())
    create_task = Mock(side_effect=asyncio.create_task)
    try_acquire = AsyncMock(return_value=True)

    import app.core.database as database_module
    import app.settings.service as settings_service_module

    event_bus_module = importlib.import_module("app.events.event_bus")

    monkeypatch.setenv("GRIDFLEET_FREEZE_BACKGROUND_LOOPS", "1")
    pool_reopen, pool_close = _patch_agent_http_pool(monkeypatch)
    monkeypatch.setattr(database_module, "async_session", session_factory)
    monkeypatch.setattr(main, "session_factory", session_factory)
    monkeypatch.setattr(main, "_validate_online_agent_contracts", AsyncMock())
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


async def test_lifespan_does_not_self_preempt_during_startup(monkeypatch: MonkeyPatch) -> None:
    db = AsyncMock()
    session_factory = FakeSessionFactory(db)
    loop = FakeLoop()
    engine = SimpleNamespace(dispose=AsyncMock())
    sequence: list[str] = []
    real_create_task = asyncio.create_task

    def tracking_create_task(
        coro: Coroutine[object, object, None],
        *,
        name: str | None = None,
    ) -> asyncio.Task[None]:
        if name == "control_plane_leader_watcher":
            sequence.append("watcher_task_created")
        return real_create_task(coro, name=name)

    async def tracking_try_acquire(*_args: object, **_kwargs: object) -> bool:
        sequence.append("try_acquire_started")
        await asyncio.sleep(0)
        sequence.append("try_acquire_returned")
        return True

    import app.core.database as database_module
    import app.settings.service as settings_service_module

    event_bus_module = importlib.import_module("app.events.event_bus")

    _patch_agent_http_pool(monkeypatch)
    monkeypatch.setattr(database_module, "async_session", session_factory)
    monkeypatch.setattr(main, "session_factory", session_factory)
    monkeypatch.setattr(main, "_validate_online_agent_contracts", AsyncMock())
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
    monkeypatch.setattr(main.control_plane_leader, "try_acquire", tracking_try_acquire)
    monkeypatch.setattr(main.control_plane_leader, "release", AsyncMock())
    monkeypatch.setattr(main, "shutdown_background_tasks", AsyncMock())
    monkeypatch.setattr(main, "engine", engine)
    monkeypatch.setattr(main.asyncio, "get_running_loop", lambda: loop)
    monkeypatch.setattr(main.asyncio, "create_task", tracking_create_task)
    monkeypatch.setattr(main, "control_plane_leader_keepalive_loop", _forever)
    monkeypatch.setattr(main, "control_plane_leader_watcher_loop", _forever)
    monkeypatch.setattr(main, "heartbeat_loop", _forever)
    monkeypatch.setattr(main, "session_sync_loop", _forever)
    monkeypatch.setattr(main, "node_health_loop", _forever)
    monkeypatch.setattr(main, "device_connectivity_loop", _forever)
    monkeypatch.setattr(main, "property_refresh_loop", _forever)
    monkeypatch.setattr(main, "hardware_telemetry_loop", _forever)
    monkeypatch.setattr(main, "host_resource_telemetry_loop", _forever)
    monkeypatch.setattr(main.job_queue, "durable_job_worker_loop", lambda session_factory: _forever())
    monkeypatch.setattr(main, "run_reaper_loop", _forever)
    monkeypatch.setattr(main, "data_cleanup_loop", _forever)
    monkeypatch.setattr(main, "session_viability_loop", _forever)
    monkeypatch.setattr(main, "fleet_capacity_collector_loop", _forever)
    monkeypatch.setattr(main, "pack_drain_loop", _forever)
    monkeypatch.setattr(main, "appium_reconciler_loop", _forever)
    monkeypatch.setattr(main, "device_intent_reconciler_loop", _forever)

    async with main.lifespan(main.app):
        pass

    assert sequence.index("try_acquire_returned") < sequence.index("watcher_task_created"), (
        f"watcher created before try_acquire completed: {sequence}"
    )


async def test_startup_marks_unsupported_online_agent_contracts_offline(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    db_host.capabilities = {"orchestration_contract_version": 1}
    await db_session.commit()

    await main._validate_online_agent_contracts(db_session)

    host = (await db_session.execute(select(Host).where(Host.id == db_host.id))).scalar_one()
    assert host.status == HostStatus.offline


async def test_cancel_and_wait_logs_non_cancelled_task_failure(caplog: LogCaptureFixture) -> None:
    async def failing() -> None:
        raise RuntimeError("shutdown boom")

    task = asyncio.create_task(failing(), name="failing-task")
    await asyncio.sleep(0)

    await main._cancel_and_wait_for_tasks([task], label="backend")

    assert any(
        isinstance(record.msg, dict)
        and record.msg["message"] == "%s task %s failed during shutdown"
        and record.msg["positional_args"] == ("backend", "failing-task")
        for record in caplog.records
    )


async def test_health_metrics_and_availability_helpers(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(main, "check_liveness", AsyncMock(return_value={"status": "ok"}))
    assert await main.live_health() == {"status": "ok"}

    monkeypatch.setattr(main, "check_readiness", AsyncMock(return_value=({"status": "ready"}, 202)))
    ready = await main.ready_health(db=AsyncMock())
    health = await main.health(db=AsyncMock())
    assert isinstance(ready, JSONResponse)
    assert isinstance(health, JSONResponse)
    assert ready.status_code == 202
    assert health.status_code == 202

    monkeypatch.setattr(main, "refresh_system_gauges", AsyncMock())
    monkeypatch.setattr(main, "render_metrics", Mock(return_value=b"metrics"))
    metrics = await main.metrics(db=AsyncMock())
    assert isinstance(metrics, Response)
    assert metrics.body == b"metrics"

    ready_device = SimpleNamespace()
    blocked_device = SimpleNamespace()
    monkeypatch.setattr(main.device_service, "list_devices", AsyncMock(return_value=[ready_device, blocked_device]))
    monkeypatch.setattr(main, "is_ready_for_use_async", AsyncMock(side_effect=[True, True]))
    monkeypatch.setattr(
        main.device_health,
        "device_allows_allocation",
        Mock(side_effect=[True, False]),
    )

    availability = await main.check_availability(platform_id="android_mobile", count=2, db=AsyncMock())

    assert availability == {
        "available": False,
        "requested": 2,
        "matched": 1,
        "platform_id": "android_mobile",
    }
