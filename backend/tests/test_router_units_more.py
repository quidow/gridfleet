from __future__ import annotations

# ruff: noqa: SIM117
import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, Mock, patch

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError

from app.analytics import router as analytics
from app.analytics.schemas import DeviceReliabilityRow, DeviceUtilizationRow, GroupByOption
from app.appium_nodes.models import AppiumDesiredState
from app.appium_nodes.routers import admin as admin_appium_nodes
from app.appium_nodes.routers import nodes as nodes_router
from app.core.errors import AgentCallError, PackDisabledError, PackUnavailableError
from app.core.pagination import CursorPage, CursorPaginationError
from app.devices.models import ConnectionType, DeviceType
from app.devices.routers import (
    bulk as bulk,
)
from app.devices.routers import (
    control as devices_control,
)
from app.devices.routers import (
    core as devices_core,
)
from app.devices.routers import (
    groups as device_groups,
)
from app.devices.routers import (
    helpers as device_route_helpers,
)
from app.devices.routers import (
    test_data as devices_test_data,
)
from app.devices.schemas.device import (
    BulkMaintenanceEnter,
    BulkTagsUpdate,
    DeviceVerificationCreate,
    DeviceVerificationUpdate,
)
from app.devices.services.identity_conflicts import DeviceIdentityConflictError
from app.devices.services.intent import IntentService
from app.events import router as events
from app.grid import router as grid
from app.hosts import router as hosts
from app.lifecycle import router as lifecycle
from app.packs.routers import (
    agent_state as agent_driver_packs,
)
from app.packs.routers import (
    catalog as driver_packs,
)
from app.packs.routers import (
    export as driver_pack_export,
)
from app.packs.routers import (
    uploads as driver_pack_uploads,
)
from app.packs.schemas import CurrentReleasePatch, RuntimePolicy
from app.plugins import router as plugins_router
from app.plugins.schemas import PluginCreate, PluginUpdate
from app.plugins.service import PluginService
from app.runs import router as runs
from app.runs.models import RunState
from app.runs.schemas import (
    ReservedDeviceInfo,
    RunCooldownRequest,
    RunCreate,
    RunPreparationFailureReport,
    RunRead,
    SessionCounts,
)
from app.sessions import router as sessions
from app.settings import router as settings_router
from app.settings.schemas import SettingsBulkUpdate, SettingUpdate
from app.settings.services_container import SettingsServices
from app.verification import router as devices_verification_router
from app.webhooks import router as webhooks
from tests.conftest import settings_service
from tests.fakes import FakeSettingsReader
from tests.helpers import test_event_bus as event_bus

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    from sqlalchemy.ext.asyncio import AsyncSession


class DummySession:
    def __init__(self, get_result: object | None = None, execute_result: object | None = None) -> None:
        self.get_result = get_result
        self.execute_result = execute_result
        self.committed = False

    async def get(self, *_args: object, **_kwargs: object) -> object | None:
        return self.get_result

    async def execute(self, *_args: object, **_kwargs: object) -> object | None:
        return self.execute_result

    async def commit(self) -> None:
        self.committed = True

    async def refresh(self, _obj: object) -> None:
        return None


class MutatingSession(DummySession):
    def __init__(self, get_result: object | None = None, execute_result: object | None = None) -> None:
        super().__init__(get_result=get_result, execute_result=execute_result)
        self.refreshed: list[object] = []

    async def refresh(self, obj: object) -> None:
        self.refreshed.append(obj)


def _mock_settings_svc(service: object | None = None) -> SettingsServices:
    """Build a SettingsServices with a mock or real service for unit-test route calls."""
    svc = service if service is not None else settings_service
    return SettingsServices(service=svc, config=Mock(), session_factory=object())  # type: ignore[arg-type]


async def test_settings_router_error_paths() -> None:
    svc = Mock()
    ss = _mock_settings_svc(svc)

    svc.bulk_update = AsyncMock(side_effect=KeyError("missing"))
    with pytest.raises(HTTPException) as caught:
        await settings_router.bulk_update_settings(
            SettingsBulkUpdate(settings={"missing": 1}),
            db=object(),
            settings_services=ss,
            events=SimpleNamespace(publisher=event_bus),
        )
    assert caught.value.status_code == 404

    svc.bulk_update = AsyncMock(side_effect=ValueError("bad"))
    with pytest.raises(HTTPException) as caught:
        await settings_router.bulk_update_settings(
            SettingsBulkUpdate(settings={"bad": 1}),
            db=object(),
            settings_services=ss,
            events=SimpleNamespace(publisher=event_bus),
        )
    assert caught.value.status_code == 400

    svc.get_setting_response = Mock(side_effect=KeyError("missing"))
    with pytest.raises(HTTPException) as caught:
        await settings_router.get_setting("missing", settings_services=ss)
    assert caught.value.status_code == 404

    svc.update = AsyncMock(side_effect=KeyError("missing"))
    with pytest.raises(HTTPException) as caught:
        await settings_router.update_setting(
            "missing",
            SettingUpdate(value=1),
            db=object(),
            settings_services=ss,
            events=SimpleNamespace(publisher=event_bus),
        )
    assert caught.value.status_code == 404

    svc.update = AsyncMock(side_effect=ValueError("bad"))
    with pytest.raises(HTTPException) as caught:
        await settings_router.update_setting(
            "bad",
            SettingUpdate(value=1),
            db=object(),
            settings_services=ss,
            events=SimpleNamespace(publisher=event_bus),
        )
    assert caught.value.status_code == 400

    svc.reset = AsyncMock(side_effect=KeyError("missing"))
    with pytest.raises(HTTPException) as caught:
        await settings_router.reset_setting(
            "missing", db=object(), events=SimpleNamespace(publisher=event_bus), settings_services=ss
        )
    assert caught.value.status_code == 404


async def test_agent_driver_pack_router_delegates_and_commits() -> None:
    host_id = uuid.uuid4()
    db = DummySession()
    desired_payload = {"host_id": str(host_id), "desired": []}

    compute = AsyncMock(return_value=desired_payload)
    mock_packs_desired = SimpleNamespace(status=SimpleNamespace(compute_desired=compute))
    response = await agent_driver_packs.desired(host_id=host_id, db=db, packs=mock_packs_desired)

    assert response == desired_payload
    compute.assert_awaited_once_with(db, host_id)

    status_payload: dict[str, object] = {"host_id": str(host_id), "packs": []}
    apply_status = AsyncMock()
    mock_packs_status = SimpleNamespace(status=SimpleNamespace(apply_status=apply_status))
    response = await agent_driver_packs.status(payload=status_payload, db=db, packs=mock_packs_status)

    assert response.status_code == 204
    assert db.committed is True
    apply_status.assert_awaited_once_with(db, status_payload)


async def test_analytics_router_non_csv_and_capacity_defaults() -> None:
    row = DeviceUtilizationRow(
        device_id=str(uuid.uuid4()),
        device_name="device-1",
        platform_id="android_mobile",
        total_session_time_sec=30.0,
        idle_time_sec=570.0,
        busy_pct=5.0,
        session_count=1,
    )
    reliability_row = DeviceReliabilityRow(
        device_id=str(uuid.uuid4()),
        device_name="device-1",
        platform_id="android_mobile",
        health_check_failures=0,
        connectivity_losses=0,
        node_crashes=0,
        total_incidents=0,
    )
    capacity = SimpleNamespace(timeline=[])

    with patch.object(analytics.analytics_service, "get_device_utilization", new=AsyncMock(return_value=[row])):
        utilization = await analytics.device_utilization(date_from=None, date_to=None, export_format=None, db=object())
    assert utilization == [row]

    with patch.object(
        analytics.analytics_service, "get_device_reliability", new=AsyncMock(return_value=[reliability_row])
    ):
        reliability = await analytics.device_reliability(date_from=None, date_to=None, export_format=None, db=object())
    assert reliability == [reliability_row]

    db = object()
    mock_timeline = AsyncMock(return_value=capacity)
    mock_fleet_capacity = SimpleNamespace(get_fleet_capacity_timeline=mock_timeline)
    mock_device_services = SimpleNamespace(fleet_capacity=mock_fleet_capacity)
    response = await analytics.fleet_capacity_timeline(
        date_from=None, date_to=None, db=db, device_services=mock_device_services
    )

    assert response is capacity
    assert mock_timeline.await_args.args == (db,)
    assert mock_timeline.await_args.kwargs["date_from"] is not None
    assert mock_timeline.await_args.kwargs["date_to"] is not None


async def test_lifecycle_incidents_router_returns_paginated_response() -> None:
    db = object()
    list_incidents = AsyncMock(return_value=([], "next", "prev"))
    lifecycle_services = SimpleNamespace(incidents=SimpleNamespace(list_lifecycle_incidents_paginated=list_incidents))
    response = await lifecycle.get_lifecycle_incidents(
        limit=5, device_id=None, cursor=None, direction="newer", db=db, lifecycle_services=lifecycle_services
    )

    assert response["items"] == []
    assert response["limit"] == 5
    assert response["next_cursor"] == "next"
    assert response["prev_cursor"] == "prev"
    list_incidents.assert_awaited_once_with(db, limit=5, device_id=None, cursor=None, direction="newer")


async def test_more_router_success_and_not_found_branches(monkeypatch: pytest.MonkeyPatch) -> None:
    group_id = uuid.uuid4()
    device_id = uuid.uuid4()

    ds_update_none = SimpleNamespace(groups=SimpleNamespace(update_group=AsyncMock(return_value=None)))
    with pytest.raises(HTTPException) as exc:
        await device_groups.update_group(
            group_id,
            device_groups.DeviceGroupUpdate(name="new"),
            db=object(),
            device_services=ds_update_none,
        )
    assert exc.value.status_code == 404
    updated_group = SimpleNamespace(id=group_id)
    ds_update_ok = SimpleNamespace(
        groups=SimpleNamespace(
            update_group=AsyncMock(return_value=updated_group),
            get_group=AsyncMock(return_value={"id": group_id}),
        )
    )
    assert await device_groups.update_group(
        group_id,
        device_groups.DeviceGroupUpdate(name="new"),
        db=object(),
        device_services=ds_update_ok,
    ) == {"id": group_id}

    ds_members_none = SimpleNamespace(groups=SimpleNamespace(get_group=AsyncMock(return_value=None)))
    with pytest.raises(HTTPException) as exc:
        await device_groups.add_members(
            group_id,
            device_groups.GroupMembershipUpdate(device_ids=[device_id]),
            db=object(),
            device_services=ds_members_none,
        )
    assert exc.value.status_code == 404
    with pytest.raises(HTTPException) as exc:
        await device_groups.remove_members(
            group_id,
            device_groups.GroupMembershipUpdate(device_ids=[device_id]),
            db=object(),
            device_services=ds_members_none,
        )
    assert exc.value.status_code == 404

    mock_ds_update = SimpleNamespace(
        crud=SimpleNamespace(update_device=AsyncMock(return_value=object())),
        presenter=SimpleNamespace(serialize_device=AsyncMock(return_value={"id": "ok"})),
    )
    assert await devices_core.update_device(
        device_id,
        devices_core.DevicePatch(name="new"),
        db=object(),
        device_services=mock_ds_update,
    ) == {"id": "ok"}

    info = runs.ReservedDeviceInfo(
        device_id=str(device_id),
        identity_value="serial",
        pack_id="pack",
        platform_id="android",
        os_version="14",
    )
    run = _run_obj()
    db = DummySession(
        execute_result=SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [SimpleNamespace(id=device_id)]))
    )
    mock_svc = Mock()
    mock_svc.get = Mock(return_value="http://grid:4444")
    mock_rs_create = SimpleNamespace(
        allocator=AsyncMock(),
        lifecycle=AsyncMock(),
        failure=AsyncMock(),
        query=AsyncMock(),
    )
    mock_rs_create.allocator.create_run = AsyncMock(return_value=(run, [info]))
    mock_rs_create.query.hydrate_reserved_device_infos = AsyncMock()
    created = await runs.create_run(
        RunCreate(name="ci", requirements=[{"pack_id": "pack", "platform_id": "android"}]),
        include="config",
        db=db,
        run_services=mock_rs_create,
    )
    assert created["id"] == run.id
    mock_rs_create.query.hydrate_reserved_device_infos.assert_awaited_once()

    pack_id = "appium-demo"
    pack = SimpleNamespace(id=pack_id)
    mock_packs_update = SimpleNamespace(lifecycle=SimpleNamespace(transition_pack_state=AsyncMock(return_value=pack)))
    with patch("app.packs.routers.catalog.build_pack_out", new=Mock(return_value={"id": pack_id})):
        assert (
            await driver_packs.update_pack(
                pack_id,
                driver_packs.PackPatch(state="enabled"),
                _username="admin",
                session=object(),
                packs=mock_packs_update,
            )
        ) == {"id": pack_id}

    mock_packs_policy = SimpleNamespace(catalog=SimpleNamespace(set_runtime_policy=AsyncMock(return_value=pack)))
    with patch("app.packs.routers.catalog.build_pack_out", new=Mock(return_value={"id": pack_id})):
        assert (
            await driver_packs.update_runtime_policy(
                pack_id,
                driver_packs.RuntimePolicyPatch(runtime_policy=RuntimePolicy()),
                _username="admin",
                session=object(),
                packs=mock_packs_policy,
            )
        ) == {"id": pack_id}

    mock_packs_del_404 = SimpleNamespace(
        catalog=SimpleNamespace(delete_pack=AsyncMock(side_effect=LookupError("missing")))
    )
    with pytest.raises(HTTPException) as exc:
        await driver_packs.delete_driver_pack(
            pack_id, _username="admin", session=DummySession(), packs=mock_packs_del_404
        )
    assert exc.value.status_code == 404

    plugin_id = uuid.uuid4()
    plugin = SimpleNamespace(id=plugin_id)
    fake_ps = SimpleNamespace(plugin=SimpleNamespace(update_plugin=AsyncMock(return_value=plugin)))
    result = await plugins_router.update_plugin(  # type: ignore[arg-type]
        plugin_id, PluginUpdate(enabled=False), db=object(), plugin_services=fake_ps
    )
    assert result is plugin

    fake_ps2 = SimpleNamespace(plugin=SimpleNamespace(sync_all_host_plugins=AsyncMock(return_value={"synced": 1})))
    assert await plugins_router.sync_all_plugins(db=object(), plugin_services=fake_ps2) == {"synced": 1}  # type: ignore[arg-type]

    reset_svc = Mock()
    reset_svc.reset_all = AsyncMock()
    assert await settings_router.reset_all_settings(
        db=object(), events=SimpleNamespace(publisher=event_bus), settings_services=_mock_settings_svc(reset_svc)
    ) == {"status": "all settings reset to defaults"}
    reset_svc.reset_all.assert_awaited_once()

    webhook_id = uuid.uuid4()
    webhook = SimpleNamespace(id=webhook_id)
    mock_wh_svc = SimpleNamespace(crud=SimpleNamespace(get_webhook=AsyncMock(return_value=webhook)))
    assert await webhooks.get_webhook(webhook_id, db=object(), webhook_services=mock_wh_svc) is webhook  # type: ignore[arg-type]
    mock_wh_svc2 = SimpleNamespace(crud=SimpleNamespace(update_webhook=AsyncMock(return_value=webhook)))
    assert (
        await webhooks.update_webhook(
            webhook_id,
            webhooks.WebhookUpdate(enabled=True),
            db=object(),
            webhook_services=mock_wh_svc2,  # type: ignore[arg-type]
        )
        is webhook
    )

    device = object()
    mock_crud_none = SimpleNamespace(get_device=AsyncMock(return_value=None))
    with pytest.raises(HTTPException) as exc:
        await device_route_helpers.get_device_or_404(device_id, object(), mock_crud_none)
    assert exc.value.status_code == 404
    mock_crud_ok = SimpleNamespace(get_device=AsyncMock(return_value=device))
    assert await device_route_helpers.get_device_or_404(device_id, object(), mock_crud_ok) is device

    async def fake_get() -> str:
        return "queued"

    queue = SimpleNamespace(get=fake_get)
    assert await events._wait_for_queue_event(queue) == "queued"  # type: ignore[arg-type]

    wait_calls = 0

    async def fake_wait_then_cancel(
        _queue: object,
        *,
        timeout: float | None = None,
    ) -> devices_verification_router.Event:
        nonlocal wait_calls
        wait_calls += 1
        if wait_calls == 1:
            raise TimeoutError()
        raise asyncio.CancelledError()

    monkeypatch.setattr(events, "_wait_for_queue_event", fake_wait_then_cancel)
    monkeypatch.setattr(events, "KEEPALIVE_INTERVAL", 0.01)
    mock_event_services = SimpleNamespace(
        subscriber=SimpleNamespace(subscribe=Mock(return_value=asyncio.Queue()), unsubscribe=Mock()),
    )
    event_response = await events.event_stream(
        SimpleNamespace(is_disconnected=AsyncMock(return_value=False)),
        events=mock_event_services,
        types=None,
        device_ids=None,
    )
    assert await event_response.body_iterator.__anext__() == {"comment": "keepalive"}
    with pytest.raises(StopAsyncIteration):
        await event_response.body_iterator.__anext__()
    await event_response.body_iterator.aclose()


async def test_device_verification_sse_filter_and_disconnect_branches(monkeypatch: pytest.MonkeyPatch) -> None:
    queue: asyncio.Queue[devices_verification_router.Event] = asyncio.Queue()
    await queue.put(devices_verification_router.Event(type="other.event", data={}, id="ignored"))
    request = SimpleNamespace(is_disconnected=AsyncMock(side_effect=[False, True]))
    initial_job = {"job_id": "job-stream", "status": "running", "current_stage": "probe"}

    mock_event_services = SimpleNamespace(
        subscriber=SimpleNamespace(subscribe=Mock(return_value=queue), unsubscribe=Mock())
    )

    mock_verification_services_none = SimpleNamespace(
        service=SimpleNamespace(get_verification_job=AsyncMock(return_value=None))
    )
    with pytest.raises(HTTPException) as exc:
        await devices_verification_router.stream_device_verification_job_events(
            "missing",
            request,
            db=SimpleNamespace(bind=None),
            event_services=mock_event_services,
            verification_services=mock_verification_services_none,
        )
    assert exc.value.status_code == 404

    mock_verification_services = SimpleNamespace(
        service=SimpleNamespace(get_verification_job=AsyncMock(return_value=initial_job))
    )

    response = await devices_verification_router.stream_device_verification_job_events(
        "job-stream",
        request,
        db=SimpleNamespace(bind=None),
        event_services=mock_event_services,
        verification_services=mock_verification_services,
    )
    assert (await response.body_iterator.__anext__())["event"] == "device.verification.updated"
    with pytest.raises(StopAsyncIteration):
        await response.body_iterator.__anext__()

    empty_queue = SimpleNamespace(get=object)

    class FakeTask:
        def __await__(self) -> object:
            async def done() -> devices_verification_router.Event:
                return devices_verification_router.Event(type="x", data={}, id="1")

            return done().__await__()

        def done(self) -> bool:
            return False

        def cancel(self) -> None:
            return None

    monkeypatch.setattr(devices_verification_router.asyncio, "create_task", Mock(return_value=FakeTask()))
    monkeypatch.setattr(devices_verification_router.asyncio, "gather", AsyncMock(return_value=[]))
    assert (await devices_verification_router._read_queue_event(empty_queue)).type == "x"  # type: ignore[arg-type]


async def test_plugins_router_missing_host_and_plugin_paths() -> None:
    plugin_id = uuid.uuid4()
    fake_ps_update = SimpleNamespace(plugin=SimpleNamespace(update_plugin=AsyncMock(return_value=None)))
    with pytest.raises(HTTPException) as caught:
        await plugins_router.update_plugin(  # type: ignore[arg-type]
            plugin_id, PluginUpdate(enabled=True), db=object(), plugin_services=fake_ps_update
        )
    assert caught.value.status_code == 404

    fake_ps_delete = SimpleNamespace(plugin=SimpleNamespace(delete_plugin=AsyncMock(return_value=False)))
    with pytest.raises(HTTPException) as caught:
        await plugins_router.delete_plugin(  # type: ignore[arg-type]
            plugin_id, db=object(), plugin_services=fake_ps_delete
        )
    assert caught.value.status_code == 404

    fake_hs_none = SimpleNamespace(crud=SimpleNamespace(get_host=AsyncMock(return_value=None)))
    fake_ps_host = SimpleNamespace(plugin=SimpleNamespace())
    with pytest.raises(HTTPException) as caught:
        await plugins_router.host_plugins(
            uuid.uuid4(), db=object(), plugin_services=fake_ps_host, host_services=fake_hs_none
        )  # type: ignore[arg-type]
    assert caught.value.status_code == 404

    fake_ps_sync = SimpleNamespace(plugin=SimpleNamespace())
    with pytest.raises(HTTPException) as caught:
        await plugins_router.sync_host_plugins(
            uuid.uuid4(), db=object(), plugin_services=fake_ps_sync, host_services=fake_hs_none
        )  # type: ignore[arg-type]
    assert caught.value.status_code == 404


async def test_device_verification_router_error_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    db = SimpleNamespace(bind=None)
    mock_verification_services_pack_error = SimpleNamespace(
        service=SimpleNamespace(start_verification_job=AsyncMock(side_effect=PackUnavailableError("pack")))
    )
    with pytest.raises(HTTPException) as caught:
        await devices_verification_router.create_device_verification_job(
            Mock(), db=db, verification_services=mock_verification_services_pack_error
        )
    assert caught.value.status_code == 422

    mock_device_services_none = SimpleNamespace(
        crud=SimpleNamespace(get_device=AsyncMock(return_value=None)),
    )
    with pytest.raises(HTTPException) as caught:
        await devices_verification_router.create_existing_device_verification_job(
            uuid.uuid4(),
            Mock(),
            db=db,
            device_services=mock_device_services_none,
            verification_services=SimpleNamespace(service=SimpleNamespace()),
        )
    assert caught.value.status_code == 404

    mock_verification_services_no_job = SimpleNamespace(
        service=SimpleNamespace(get_verification_job=AsyncMock(return_value=None))
    )
    with pytest.raises(HTTPException) as caught:
        await devices_verification_router.get_device_verification_job(
            "missing", db=db, verification_services=mock_verification_services_no_job
        )
    assert caught.value.status_code == 404


async def test_hosts_router_auto_tasks_and_driver_pack_404() -> None:
    host_id = uuid.uuid4()

    class SessionCtx:
        async def __aenter__(self) -> object:
            return object()

        async def __aexit__(self, *args: object) -> None:
            return None

    host = SimpleNamespace(id=host_id, hostname="host-a")
    discovery_result = SimpleNamespace(new_devices=[SimpleNamespace(id=uuid.uuid4())])
    mock_publisher = AsyncMock()
    _auto_ss = FakeSettingsReader({})
    _auto_cb = Mock()
    fake_discovery = AsyncMock()
    fake_discovery.discover_devices = AsyncMock(return_value=discovery_result)
    fake_crud_host = SimpleNamespace(get_host=AsyncMock(return_value=host))
    with patch.object(hosts, "async_session", return_value=SessionCtx()):
        await hosts._auto_discover(host_id, mock_publisher, fake_discovery, fake_crud_host)
    mock_publisher.publish.assert_awaited_once()

    fake_crud_host2 = SimpleNamespace(get_host=AsyncMock(return_value=host))
    with (
        patch.object(hosts, "async_session", return_value=SessionCtx()),
        patch.object(PluginService, "list_plugins", new=AsyncMock(return_value=[object()])),
        patch.object(PluginService, "auto_sync_host_plugins", new=AsyncMock()) as sync,
    ):
        await hosts._auto_prepare_host_diagnostics(
            host_id, settings=_auto_ss, circuit_breaker=_auto_cb, crud=fake_crud_host2
        )  # type: ignore[arg-type]
    sync.assert_awaited_once()

    fake_crud_none = SimpleNamespace(get_host=AsyncMock(return_value=None))
    fake_discovery2 = AsyncMock()
    fake_discovery2.discover_devices = AsyncMock(return_value=discovery_result)
    with patch.object(hosts, "async_session", return_value=SessionCtx()):
        await hosts._auto_discover(host_id, mock_publisher, fake_discovery2, fake_crud_none)
        await hosts._auto_prepare_host_diagnostics(
            host_id, settings=_auto_ss, circuit_breaker=_auto_cb, crud=fake_crud_none
        )  # type: ignore[arg-type]

    fake_crud_err = SimpleNamespace(get_host=AsyncMock(side_effect=RuntimeError("db")))
    fake_discovery3 = AsyncMock()
    fake_discovery3.discover_devices = AsyncMock(return_value=discovery_result)
    with (
        patch.object(hosts, "async_session", return_value=SessionCtx()),
        patch.object(hosts.logger, "exception", new=Mock()) as log_exception,
    ):
        await hosts._auto_discover(host_id, mock_publisher, fake_discovery3, fake_crud_err)
        await hosts._auto_prepare_host_diagnostics(
            host_id, settings=_auto_ss, circuit_breaker=_auto_cb, crud=fake_crud_err
        )  # type: ignore[arg-type]
    assert log_exception.call_count == 2

    with pytest.raises(HTTPException) as caught:
        await hosts.host_driver_packs(host_id, db=DummySession(get_result=None), pack_services=SimpleNamespace())
    assert caught.value.status_code == 404


async def test_runs_router_missing_device_and_cooldown_branches() -> None:
    info = ReservedDeviceInfo(
        device_id=str(uuid.uuid4()),
        identity_value="missing-device",
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        os_version="14",
    )
    run = SimpleNamespace(
        id=uuid.uuid4(),
        name="run",
        state=RunState.active,
        requirements=[],
        ttl_minutes=60,
        heartbeat_timeout_sec=120,
        created_at=datetime.now(UTC),
    )
    mock_rs = SimpleNamespace(
        allocator=AsyncMock(),
        lifecycle=AsyncMock(),
        failure=AsyncMock(),
        query=AsyncMock(),
    )
    mock_rs.allocator.create_run = AsyncMock(return_value=(run, [info]))
    mock_rs.query.hydrate_reserved_device_infos = AsyncMock()

    with (
        patch.object(runs.run_service, "mark_reserved_device_info_includes_unavailable") as mark,
        patch.object(runs, "select") as select_mock,
    ):
        select_mock.return_value.where.return_value = object()
        db = DummySession(execute_result=SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [])))
        response = await runs.create_run(
            RunCreate(name="r", requirements=[]),
            include="config",
            db=db,
            run_services=mock_rs,
        )
    assert response["id"] == run.id
    mark.assert_called_once()
    mock_rs.query.hydrate_reserved_device_infos.assert_awaited_once()

    with patch.object(runs.run_service, "get_run", new=AsyncMock(return_value=None)):
        with pytest.raises(HTTPException) as caught:
            await runs.get_run(uuid.uuid4(), db=object(), run_services=mock_rs)
    assert caught.value.status_code == 404

    mock_rs.failure.cooldown_device = AsyncMock(side_effect=ValueError("Run not found"))
    with pytest.raises(HTTPException) as caught:
        await runs.cooldown_device_endpoint(
            uuid.uuid4(),
            uuid.uuid4(),
            RunCooldownRequest(reason="bad", ttl_seconds=1),
            db=object(),
            run_services=mock_rs,
        )
    assert caught.value.status_code == 404

    mock_rs.failure.cooldown_device = AsyncMock(side_effect=ValueError("ttl_seconds must be <= 30"))
    with pytest.raises(HTTPException) as caught:
        await runs.cooldown_device_endpoint(
            uuid.uuid4(),
            uuid.uuid4(),
            RunCooldownRequest(reason="bad", ttl_seconds=1),
            db=object(),
            run_services=mock_rs,
        )
    assert caught.value.status_code == 422


class ScalarResult:
    def __init__(self, value: object | None) -> None:
        self.value = value

    def scalar_one_or_none(self) -> object | None:
        return self.value


class ChunkUpload:
    def __init__(self, chunks: list[bytes], filename: str | None = "pack.tgz") -> None:
        self.chunks = chunks
        self.filename = filename

    async def read(self, _size: int) -> bytes:
        if not self.chunks:
            return b""
        return self.chunks.pop(0)


def _run_obj(*, state: RunState = RunState.active) -> SimpleNamespace:
    now = datetime(2026, 5, 1, tzinfo=UTC)
    return SimpleNamespace(
        id=uuid.uuid4(),
        name="ci run",
        state=state,
        requirements=[],
        ttl_minutes=60,
        heartbeat_timeout_sec=120,
        reserved_devices=[],
        error=None,
        created_at=now,
        started_at=now,
        completed_at=None,
        created_by="ci",
        last_heartbeat=now,
    )


def _run_read(run: SimpleNamespace) -> RunRead:
    return RunRead(
        id=run.id,
        name=run.name,
        state=run.state,
        requirements=run.requirements,
        ttl_minutes=run.ttl_minutes,
        heartbeat_timeout_sec=run.heartbeat_timeout_sec,
        reserved_devices=[],
        created_at=run.created_at,
        started_at=run.started_at,
        completed_at=run.completed_at,
        created_by=run.created_by,
        last_heartbeat=run.last_heartbeat,
        session_counts=SessionCounts(total=1, passed=1),
    )


async def test_admin_appium_node_clear_transition_paths() -> None:
    node_id = uuid.uuid4()
    device_id = uuid.uuid4()

    with pytest.raises(HTTPException) as exc:
        await admin_appium_nodes.clear_transition(
            node_id,
            admin_appium_nodes.ClearTransitionBody(reason="stuck"),
            db=DummySession(get_result=None),
            username="admin",
        )
    assert exc.value.status_code == 404

    node = SimpleNamespace(device_id=device_id)
    with (
        patch("app.appium_nodes.routers.admin.device_locking.lock_device", new=AsyncMock(return_value=object())),
        patch(
            "app.appium_nodes.routers.admin.appium_node_locking.lock_appium_node_for_device",
            new=AsyncMock(return_value=None),
        ),
    ):
        with pytest.raises(HTTPException) as exc:
            await admin_appium_nodes.clear_transition(
                node_id,
                admin_appium_nodes.ClearTransitionBody(reason="stuck"),
                db=DummySession(get_result=node),
                username="admin",
            )
    assert exc.value.status_code == 404

    locked = SimpleNamespace(device_id=device_id, transition_token=None, transition_deadline=object())
    session = MutatingSession(get_result=node)
    with (
        patch("app.appium_nodes.routers.admin.device_locking.lock_device", new=AsyncMock(return_value=object())),
        patch(
            "app.appium_nodes.routers.admin.appium_node_locking.lock_appium_node_for_device",
            new=AsyncMock(return_value=locked),
        ),
    ):
        assert (
            await admin_appium_nodes.clear_transition(
                node_id,
                admin_appium_nodes.ClearTransitionBody(reason="stuck"),
                db=session,
                username="admin",
            )
            is locked
        )
    assert session.refreshed == [locked]

    token = uuid.uuid4()
    locked = SimpleNamespace(device_id=device_id, transition_token=token, transition_deadline=object())
    session = MutatingSession(get_result=node)
    with (
        patch("app.appium_nodes.routers.admin.device_locking.lock_device", new=AsyncMock(return_value=object())),
        patch(
            "app.appium_nodes.routers.admin.appium_node_locking.lock_appium_node_for_device",
            new=AsyncMock(return_value=locked),
        ),
        patch("app.appium_nodes.routers.admin.record_event", new=AsyncMock()) as record_event,
    ):
        assert (
            await admin_appium_nodes.clear_transition(
                node_id,
                admin_appium_nodes.ClearTransitionBody(reason="stuck"),
                db=session,
                username="admin",
            )
            is locked
        )
    assert locked.transition_token is None
    assert locked.transition_deadline is None
    assert session.committed is True
    record_event.assert_awaited_once()


async def test_bulk_router_delegates_all_operations() -> None:
    device_ids = [uuid.uuid4()]
    body = SimpleNamespace(device_ids=device_ids)
    tags_body = SimpleNamespace(device_ids=device_ids, tags={"lab": "east"}, merge=True)

    for call, service_name, payload in (
        (bulk.bulk_start_nodes, "bulk_start_nodes", body),
        (bulk.bulk_stop_nodes, "bulk_stop_nodes", body),
        (bulk.bulk_restart_nodes, "bulk_restart_nodes", body),
        (bulk.bulk_delete, "bulk_delete", body),
        (bulk.bulk_enter_maintenance, "bulk_enter_maintenance", body),
        (bulk.bulk_exit_maintenance, "bulk_exit_maintenance", body),
        (bulk.bulk_reconnect, "bulk_reconnect", body),
        (bulk.bulk_update_tags, "bulk_update_tags", tags_body),
    ):
        mock_bulk = AsyncMock(**{service_name: AsyncMock(return_value={"ok": service_name})})
        device_services = SimpleNamespace(bulk=mock_bulk)
        assert await call(payload, db=object(), device_services=device_services) == {"ok": service_name}


async def test_devices_test_data_router_paths() -> None:
    device_id = uuid.uuid4()
    device = SimpleNamespace(id=device_id)
    payload = SimpleNamespace(root={"token": "abc"})

    fake_test_data_svc = SimpleNamespace(
        get_device_test_data=AsyncMock(return_value={"a": 1}),
        replace_device_test_data=AsyncMock(return_value={"token": "abc"}),
        merge_device_test_data=AsyncMock(return_value={"merged": True}),
    )
    fake_ds = SimpleNamespace(test_data=fake_test_data_svc, crud=AsyncMock())
    with (
        patch("app.devices.routers.test_data.get_device_or_404", new=AsyncMock(return_value=device)),
        patch("app.devices.routers.test_data.get_device_for_update_or_404", new=AsyncMock(return_value=device)),
    ):
        assert await devices_test_data.get_test_data(device_id, db=object(), device_services=fake_ds) == {"a": 1}  # type: ignore[arg-type]
        assert await devices_test_data.replace_test_data(device_id, payload, db=object(), device_services=fake_ds) == {
            "token": "abc"
        }  # type: ignore[arg-type]
        assert await devices_test_data.merge_test_data(device_id, payload, db=object(), device_services=fake_ds) == {
            "merged": True
        }  # type: ignore[arg-type]

    audit_log = SimpleNamespace(
        id=uuid.uuid4(),
        previous_test_data={},
        new_test_data={"a": 1},
        changed_by="admin",
        changed_at=datetime(2026, 5, 1, tzinfo=UTC),
    )
    fake_history_svc = SimpleNamespace(get_test_data_history=AsyncMock(return_value=[audit_log]))
    fake_ds_history = SimpleNamespace(test_data=fake_history_svc, crud=AsyncMock())
    with patch("app.devices.routers.test_data.get_device_or_404", new=AsyncMock(return_value=device)):
        history = await devices_test_data.get_history(device_id, db=object(), device_services=fake_ds_history)  # type: ignore[arg-type]
    assert history[0]["changed_at"] == "2026-05-01T00:00:00+00:00"


async def test_sessions_router_list_detail_and_mutation_paths() -> None:
    request = SimpleNamespace(query_params={})
    session_obj = SimpleNamespace(session_id="s1")
    detail = {
        "id": uuid.uuid4(),
        "session_id": "s1",
        "test_name": "test",
        "started_at": datetime(2026, 5, 1, tzinfo=UTC),
        "ended_at": None,
        "status": "running",
    }

    with patch("app.sessions.router._session_details_with_labels", new=AsyncMock(return_value=[detail])):
        crud = SimpleNamespace(list_sessions=AsyncMock(return_value=([session_obj], 1)))
        listed = await sessions.list_sessions(
            request,
            device_id=None,
            status=None,
            pack_id=None,
            platform_id=None,
            started_after=None,
            started_before=None,
            run_id=None,
            limit=50,
            cursor=None,
            direction="older",
            offset=0,
            sort_by="started_at",
            sort_dir="desc",
            db=object(),
            session_services=SimpleNamespace(crud=crud),  # type: ignore[arg-type]
        )
    assert listed["total"] == 1
    assert listed["items"][0]["session_id"] == "s1"

    cursor_request = SimpleNamespace(query_params={"cursor": "bad"})
    crud_err = SimpleNamespace(list_sessions_cursor=AsyncMock(side_effect=CursorPaginationError("bad cursor")))
    with pytest.raises(HTTPException) as exc:
        await sessions.list_sessions(
            cursor_request,
            device_id=None,
            status=None,
            pack_id=None,
            platform_id=None,
            started_after=None,
            started_before=None,
            run_id=None,
            limit=50,
            cursor="bad",
            direction="older",
            offset=0,
            sort_by="started_at",
            sort_dir="desc",
            db=object(),
            session_services=SimpleNamespace(crud=crud_err),  # type: ignore[arg-type]
        )
    assert exc.value.status_code == 422

    page = CursorPage(items=[session_obj], limit=50, next_cursor="next", prev_cursor="prev")
    with patch("app.sessions.router._session_details_with_labels", new=AsyncMock(return_value=[detail])):
        crud_page = SimpleNamespace(list_sessions_cursor=AsyncMock(return_value=page))
        listed = await sessions.list_sessions(
            cursor_request,
            device_id=None,
            status=None,
            pack_id=None,
            platform_id=None,
            started_after=None,
            started_before=None,
            run_id=None,
            limit=50,
            cursor="cursor",
            direction="older",
            offset=0,
            sort_by="started_at",
            sort_dir="desc",
            db=object(),
            session_services=SimpleNamespace(crud=crud_page),  # type: ignore[arg-type]
        )
    assert listed["next_cursor"] == "next"
    assert listed["prev_cursor"] == "prev"

    crud_none = SimpleNamespace(get_session=AsyncMock(return_value=None))
    with pytest.raises(HTTPException) as exc:
        await sessions.get_session("missing", db=object(), session_services=SimpleNamespace(crud=crud_none))  # type: ignore[arg-type]
    assert exc.value.status_code == 404

    with patch("app.sessions.router._session_details_with_labels", new=AsyncMock(return_value=[detail])):
        crud_found = SimpleNamespace(get_session=AsyncMock(return_value=session_obj))
        assert (
            await sessions.get_session("s1", db=object(), session_services=SimpleNamespace(crud=crud_found))  # type: ignore[arg-type]
        )["session_id"] == "s1"

    create_payload = SimpleNamespace(
        session_id="s1",
        test_name="test",
        device_id=uuid.uuid4(),
        connection_target="serial",
        status=None,
        requested_pack_id=None,
        requested_platform_id=None,
        requested_device_type=None,
        requested_connection_type=None,
        requested_capabilities=None,
        error_type=None,
        error_message=None,
    )
    crud_reg_err = SimpleNamespace(register_session=AsyncMock(side_effect=ValueError("missing")))
    with pytest.raises(HTTPException) as exc:
        await sessions.register_session(
            create_payload,
            db=object(),
            session_services=SimpleNamespace(crud=crud_reg_err),  # type: ignore[arg-type]
        )
    assert exc.value.status_code == 404
    crud_reg_ok = SimpleNamespace(register_session=AsyncMock(return_value=session_obj))
    assert (
        await sessions.register_session(
            create_payload,
            db=object(),
            session_services=SimpleNamespace(crud=crud_reg_ok),  # type: ignore[arg-type]
        )
        is session_obj
    )

    status_payload = SimpleNamespace(status="passed")
    crud_upd_none = SimpleNamespace(update_session_status=AsyncMock(return_value=None))
    with pytest.raises(HTTPException) as exc:
        await sessions.update_session_status(
            "missing",
            status_payload,
            db=object(),
            session_services=SimpleNamespace(crud=crud_upd_none),  # type: ignore[arg-type]
        )
    assert exc.value.status_code == 404
    crud_upd_ok = SimpleNamespace(update_session_status=AsyncMock(return_value=session_obj))
    assert (
        await sessions.update_session_status(
            "s1",
            status_payload,
            db=object(),
            session_services=SimpleNamespace(crud=crud_upd_ok),  # type: ignore[arg-type]
        )
        is session_obj
    )

    crud_fin_none = SimpleNamespace(mark_session_finished=AsyncMock(return_value=None))
    with pytest.raises(HTTPException) as exc:
        await sessions.post_session_finished(
            "missing",
            db=object(),
            session_services=SimpleNamespace(crud=crud_fin_none),  # type: ignore[arg-type]
        )
    assert exc.value.status_code == 404
    crud_fin_ok = SimpleNamespace(mark_session_finished=AsyncMock(return_value=session_obj))
    assert (
        await sessions.post_session_finished(
            "s1",
            db=object(),
            session_services=SimpleNamespace(crud=crud_fin_ok),  # type: ignore[arg-type]
        )
    ).status_code == 204


async def test_plugins_router_maps_service_conflicts_and_missing_resources() -> None:
    plugin_id = uuid.uuid4()
    body = PluginCreate(name="images", version="1.0.0", source="npm:images")

    fake_ps_create = SimpleNamespace(
        plugin=SimpleNamespace(create_plugin=AsyncMock(side_effect=IntegrityError("insert", {}, Exception("dupe"))))
    )
    with pytest.raises(HTTPException) as exc:
        await plugins_router.create_plugin(body, db=object(), plugin_services=fake_ps_create)  # type: ignore[arg-type]
    assert exc.value.status_code == 409

    fake_ps_update = SimpleNamespace(plugin=SimpleNamespace(update_plugin=AsyncMock(return_value=None)))
    with pytest.raises(HTTPException) as exc:
        await plugins_router.update_plugin(  # type: ignore[arg-type]
            plugin_id, PluginUpdate(version="2.0.0"), db=object(), plugin_services=fake_ps_update
        )
    assert exc.value.status_code == 404


async def test_hosts_router_registration_and_basic_crud_paths() -> None:
    host_id = uuid.uuid4()
    host = SimpleNamespace(id=host_id, hostname="host-1", devices=[])
    response = SimpleNamespace(status_code=200)

    mock_event_services = SimpleNamespace(publisher=object())

    host_settings_svc = Mock()
    host_settings_svc.get = Mock(return_value=True)
    mock_ss = _mock_settings_svc(host_settings_svc)

    _host_agent_comm = SimpleNamespace(circuit_breaker=Mock(), http_pool=None)

    fake_pack_services = SimpleNamespace(discovery=AsyncMock())
    fake_hs_reg_err = SimpleNamespace(
        crud=SimpleNamespace(register_host=AsyncMock(side_effect=IntegrityError("", {}, None)))
    )
    with pytest.raises(HTTPException) as exc:
        await hosts.register_host(  # type: ignore[arg-type]
            object(),
            response,
            db=object(),
            host_services=fake_hs_reg_err,
            event_services=mock_event_services,
            settings_services=mock_ss,
            agent_comm=_host_agent_comm,
            pack_services=fake_pack_services,
        )
    assert exc.value.status_code == 409

    fake_hs_reg_ok = SimpleNamespace(crud=SimpleNamespace(register_host=AsyncMock(return_value=(host, True))))
    with (
        patch("app.hosts.router._fire_and_forget", new=Mock()) as fire,
        patch("app.hosts.router._serialize_host", new=Mock(return_value={"id": str(host_id)})),
    ):
        result = await hosts.register_host(  # type: ignore[arg-type]
            object(),
            response,
            db=object(),
            host_services=fake_hs_reg_ok,
            event_services=mock_event_services,
            settings_services=mock_ss,
            agent_comm=_host_agent_comm,
            pack_services=fake_pack_services,
        )
        assert result == {"id": str(host_id)}
    assert response.status_code == 201
    assert fire.call_count == 2

    fake_hs_appr_none = SimpleNamespace(crud=SimpleNamespace(approve_host=AsyncMock(return_value=None)))
    with pytest.raises(HTTPException) as exc:
        await hosts.approve_host(
            host_id,
            db=object(),
            host_services=fake_hs_appr_none,
            event_services=mock_event_services,
            settings_services=mock_ss,
            agent_comm=_host_agent_comm,
            pack_services=fake_pack_services,
        )
    assert exc.value.status_code == 404

    fake_hs_appr_ok = SimpleNamespace(crud=SimpleNamespace(approve_host=AsyncMock(return_value=host)))
    with (
        patch("app.hosts.router._fire_and_forget", new=Mock()) as fire,
        patch("app.hosts.router._serialize_host", new=Mock(return_value={"id": str(host_id)})),
    ):
        result = await hosts.approve_host(
            host_id,
            db=object(),
            host_services=fake_hs_appr_ok,
            event_services=mock_event_services,
            settings_services=mock_ss,
            agent_comm=_host_agent_comm,
            pack_services=fake_pack_services,
        )
        assert result == {"id": str(host_id)}
    assert fire.call_count == 2

    fake_hs_rej_false = SimpleNamespace(crud=SimpleNamespace(reject_host=AsyncMock(return_value=False)))
    with pytest.raises(HTTPException) as exc:
        await hosts.reject_host(host_id, db=object(), host_services=fake_hs_rej_false)
    assert exc.value.status_code == 404

    fake_hs_rej_true = SimpleNamespace(crud=SimpleNamespace(reject_host=AsyncMock(return_value=True)))
    assert await hosts.reject_host(host_id, db=object(), host_services=fake_hs_rej_true) is None

    fake_hs_create_err = SimpleNamespace(
        crud=SimpleNamespace(create_host=AsyncMock(side_effect=IntegrityError("", {}, None)))
    )
    with pytest.raises(HTTPException) as exc:
        await hosts.create_host(object(), db=object(), host_services=fake_hs_create_err, settings_services=mock_ss)  # type: ignore[arg-type]
    assert exc.value.status_code == 409

    fake_hs_create_ok = SimpleNamespace(crud=SimpleNamespace(create_host=AsyncMock(return_value=host)))
    with patch("app.hosts.router._serialize_host", new=Mock(return_value={"id": str(host_id)})):
        assert await hosts.create_host(
            object(), db=object(), host_services=fake_hs_create_ok, settings_services=mock_ss
        ) == {"id": str(host_id)}  # type: ignore[arg-type]

    fake_hs_list = SimpleNamespace(crud=SimpleNamespace(list_hosts=AsyncMock(return_value=[host])))
    with patch("app.hosts.router._serialize_host", new=Mock(return_value={"id": str(host_id)})):
        assert await hosts.list_hosts(db=object(), host_services=fake_hs_list, settings_services=mock_ss) == [
            {"id": str(host_id)}
        ]


async def test_hosts_router_detail_diagnostics_tools_and_discovery_paths() -> None:
    host_id = uuid.uuid4()
    device = SimpleNamespace(id=uuid.uuid4(), pack_id="pack", platform_id="android")
    host = SimpleNamespace(
        id=host_id,
        hostname="host-1",
        ip="10.0.0.1",
        agent_port=5100,
        status=SimpleNamespace(value="online"),
        devices=[device],
    )

    mock_ss = _mock_settings_svc()
    _tools_agent_comm = SimpleNamespace(circuit_breaker=Mock(), http_pool=None)
    _disc_pack_svc = SimpleNamespace(discovery=AsyncMock())
    fake_hs_none = SimpleNamespace(crud=SimpleNamespace(get_host=AsyncMock(return_value=None)))
    for call in (
        lambda: hosts.get_host_tool_status(
            host_id, db=object(), host_services=fake_hs_none, settings_services=mock_ss, agent_comm=_tools_agent_comm
        ),
        lambda: hosts.discover_devices(host_id, db=object(), host_services=fake_hs_none, pack_services=_disc_pack_svc),
        lambda: hosts.intake_candidates(host_id, db=object(), host_services=fake_hs_none, pack_services=_disc_pack_svc),
    ):
        with pytest.raises(HTTPException) as exc:
            await call()
        assert exc.value.status_code == 404

    _fake_ds_none: Any = SimpleNamespace(presenter=SimpleNamespace(serialize_device=AsyncMock(return_value={})))
    for call in (
        lambda: hosts.get_host(
            host_id, db=object(), host_services=fake_hs_none, device_services=_fake_ds_none, settings_services=mock_ss
        ),
        lambda: hosts.confirm_discovery(
            host_id,
            SimpleNamespace(add_identity_values=[], remove_identity_values=[]),
            db=object(),
            host_services=fake_hs_none,
            pack_services=_disc_pack_svc,
        ),
    ):
        with pytest.raises(HTTPException) as exc:
            await call()
        assert exc.value.status_code == 404

    fake_hs_host = SimpleNamespace(crud=SimpleNamespace(get_host=AsyncMock(return_value=host)))
    fake_ds_host: Any = SimpleNamespace(
        presenter=SimpleNamespace(serialize_device=AsyncMock(return_value={"id": str(device.id)}))
    )
    with (
        patch("app.hosts.router._serialize_host", new=Mock(return_value={"id": str(host_id)})),
        patch(
            "app.hosts.router.platform_label_service.load_platform_label_map",
            new=AsyncMock(return_value={("pack", "android"): "Android"}),
        ),
    ):
        detail = await hosts.get_host(
            host_id, db=object(), host_services=fake_hs_host, device_services=fake_ds_host, settings_services=mock_ss
        )
    assert detail["devices"] == [{"id": str(device.id)}]

    fake_hs_diag_none = SimpleNamespace(diagnostics=SimpleNamespace(get_host_diagnostics=AsyncMock(return_value=None)))
    with pytest.raises(HTTPException) as exc:
        await hosts.get_host_diagnostics(host_id, db=object(), host_services=fake_hs_diag_none)
    assert exc.value.status_code == 404
    fake_hs_diag_ok = SimpleNamespace(
        diagnostics=SimpleNamespace(get_host_diagnostics=AsyncMock(return_value={"ok": True}))
    )
    assert await hosts.get_host_diagnostics(host_id, db=object(), host_services=fake_hs_diag_ok) == {"ok": True}

    telemetry_svc = Mock()
    telemetry_svc.get = Mock(return_value=60)
    telemetry_ss = _mock_settings_svc(telemetry_svc)

    fake_hs_tel_err = SimpleNamespace(
        resource_telemetry=SimpleNamespace(fetch_host_resource_telemetry=AsyncMock(side_effect=ValueError("bad")))
    )
    with pytest.raises(HTTPException) as exc:
        await hosts.get_host_resource_telemetry(
            host_id, db=object(), host_services=fake_hs_tel_err, settings_services=telemetry_ss
        )
    assert exc.value.status_code == 400
    fake_hs_tel_none = SimpleNamespace(
        resource_telemetry=SimpleNamespace(fetch_host_resource_telemetry=AsyncMock(return_value=None))
    )
    with pytest.raises(HTTPException) as exc:
        await hosts.get_host_resource_telemetry(
            host_id, db=object(), host_services=fake_hs_tel_none, settings_services=telemetry_ss
        )
    assert exc.value.status_code == 404
    fake_hs_tel_ok = SimpleNamespace(
        resource_telemetry=SimpleNamespace(fetch_host_resource_telemetry=AsyncMock(return_value={"samples": []}))
    )
    assert await hosts.get_host_resource_telemetry(
        host_id, db=object(), host_services=fake_hs_tel_ok, settings_services=telemetry_ss
    ) == {"samples": []}

    offline = SimpleNamespace(status=SimpleNamespace(value="offline"))
    fake_hs_offline = SimpleNamespace(crud=SimpleNamespace(get_host=AsyncMock(return_value=offline)))
    with pytest.raises(HTTPException) as exc:
        await hosts.get_host_tool_status(
            host_id, db=object(), host_services=fake_hs_offline, settings_services=mock_ss, agent_comm=_tools_agent_comm
        )
    assert exc.value.status_code == 400
    with patch("app.hosts.router.get_agent_tool_status", new=AsyncMock(return_value={"host": {}, "packs": {}})):
        assert await hosts.get_host_tool_status(
            host_id, db=object(), host_services=fake_hs_host, settings_services=mock_ss, agent_comm=_tools_agent_comm
        ) == {
            "host": {},
            "packs": {},
        }

    for error, status_code in ((ValueError("busy"), 409), (None, 404)):
        del_mock = AsyncMock(side_effect=error) if error is not None else AsyncMock(return_value=False)
        fake_hs_del = SimpleNamespace(crud=SimpleNamespace(delete_host=del_mock))
        with pytest.raises(HTTPException) as exc:
            await hosts.delete_host(host_id, db=object(), host_services=fake_hs_del)
        assert exc.value.status_code == status_code
    fake_hs_del_ok = SimpleNamespace(crud=SimpleNamespace(delete_host=AsyncMock(return_value=True)))
    assert await hosts.delete_host(host_id, db=object(), host_services=fake_hs_del_ok) is None

    fake_disc_svc_ok = SimpleNamespace(
        discover_devices=AsyncMock(return_value="discovered"),
        list_intake_candidates=AsyncMock(return_value=["candidate"]),
    )
    fake_ps_ok = SimpleNamespace(discovery=fake_disc_svc_ok)
    assert (
        await hosts.discover_devices(
            host_id,
            db=object(),
            host_services=fake_hs_host,
            pack_services=fake_ps_ok,
        )
        == "discovered"
    )
    assert await hosts.intake_candidates(
        host_id,
        db=object(),
        host_services=fake_hs_host,
        pack_services=fake_ps_ok,
    ) == ["candidate"]

    body = SimpleNamespace(add_identity_values=["serial"], remove_identity_values=[])
    fake_disc_svc_conflict = SimpleNamespace(
        discover_devices=AsyncMock(return_value="fresh"),
        confirm_discovery=AsyncMock(side_effect=DeviceIdentityConflictError("dupe")),
    )
    fake_ps_conflict = SimpleNamespace(discovery=fake_disc_svc_conflict)
    with pytest.raises(HTTPException) as exc:
        await hosts.confirm_discovery(
            host_id, body, db=object(), host_services=fake_hs_host, pack_services=fake_ps_conflict
        )  # type: ignore[arg-type]
    assert exc.value.status_code == 409

    fake_disc_svc_ok2 = SimpleNamespace(
        discover_devices=AsyncMock(return_value="fresh"),
        confirm_discovery=AsyncMock(return_value="confirmed"),
    )
    fake_ps_ok2 = SimpleNamespace(discovery=fake_disc_svc_ok2)
    assert (
        await hosts.confirm_discovery(host_id, body, db=object(), host_services=fake_hs_host, pack_services=fake_ps_ok2)
        == "confirmed"
    )  # type: ignore[arg-type]


def _control_device(**overrides: object) -> SimpleNamespace:
    values: dict[str, Any] = {
        "id": uuid.uuid4(),
        "identity_value": "serial",
        "pack_id": "pack",
        "platform_id": "android",
        "device_type": DeviceType.real_device,
        "connection_type": ConnectionType.network,
        "ip_address": "10.0.0.2",
        "host": SimpleNamespace(ip="10.0.0.1", agent_port=5100),
        "host_id": uuid.uuid4(),
        "connection_target": "serial",
        "appium_node": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


async def test_devices_control_maintenance_config_session_and_refresh_paths() -> None:
    device_id = uuid.uuid4()
    device = _control_device(id=device_id)
    serialized = {"id": str(device_id)}

    for method_name, call_fn in (
        (
            "enter_maintenance",
            lambda ds: devices_control.enter_device_maintenance(device_id, object(), db=object(), device_services=ds),
        ),
        (
            "exit_maintenance",
            lambda ds: devices_control.exit_device_maintenance(device_id, db=object(), device_services=ds),
        ),
    ):
        mock_maintenance = AsyncMock(**{method_name: AsyncMock(side_effect=ValueError("bad"))})
        device_services_err = SimpleNamespace(maintenance=mock_maintenance)
        with patch("app.devices.routers.control.get_device_for_update_or_404", new=AsyncMock(return_value=device)):
            with pytest.raises(HTTPException) as exc:
                await call_fn(device_services_err)
        assert exc.value.status_code == 409

        mock_maintenance_ok = AsyncMock(**{method_name: AsyncMock(return_value=device)})
        device_services_ok = SimpleNamespace(
            maintenance=mock_maintenance_ok,
            presenter=SimpleNamespace(serialize_device=AsyncMock(return_value=serialized)),
        )
        with patch("app.devices.routers.control.get_device_for_update_or_404", new=AsyncMock(return_value=device)):
            assert await call_fn(device_services_ok) == serialized

    config = {"env": {"A": "B"}}
    config_ss = _mock_settings_svc(FakeSettingsReader({}))
    config_ss.config.merge_device_config = AsyncMock(return_value=config)
    _mock_ds_ctrl = SimpleNamespace(crud=AsyncMock())
    with (
        patch("app.devices.routers.control.get_device_or_404", new=AsyncMock(return_value=device)),
        patch("app.devices.routers.control.get_device_for_update_or_404", new=AsyncMock(return_value=device)),
        patch("app.devices.routers.control.config_service.get_device_config", new=AsyncMock(return_value=config)),
    ):
        assert (
            await devices_control.get_device_config(
                device_id, keys=" env , other ", db=object(), device_services=_mock_ds_ctrl
            )
            == config
        )
        assert (
            await devices_control.merge_device_config(device_id, {"env": {}}, db=object(), settings_services=config_ss)
            == config
        )

    audit_log = SimpleNamespace(
        id=uuid.uuid4(),
        previous_config={},
        new_config={"a": 1},
        changed_by="admin",
        changed_at=datetime(2026, 5, 1, tzinfo=UTC),
    )
    history_ss = _mock_settings_svc(FakeSettingsReader({}))
    history_ss.config.get_config_history = AsyncMock(return_value=[audit_log])
    with patch("app.devices.routers.control.get_device_or_404", new=AsyncMock(return_value=device)):
        history = await devices_control.get_config_history(
            device_id, db=object(), device_services=_mock_ds_ctrl, settings_services=history_ss
        )
    assert history[0]["changed_at"] == "2026-05-01T00:00:00+00:00"

    _viability_raises = AsyncMock(side_effect=ValueError("busy"))
    _session_svc_raises = SimpleNamespace(viability=SimpleNamespace(run_session_viability_probe=_viability_raises))
    with patch("app.devices.routers.control.get_device_for_update_or_404", new=AsyncMock(return_value=device)):
        with pytest.raises(HTTPException) as exc:
            await devices_control.device_session_test(
                device_id,
                db=object(),
                session_services=_session_svc_raises,
            )
    assert exc.value.status_code == 409

    _viability_ok = AsyncMock(return_value={"status": "passed"})
    _session_svc_ok = SimpleNamespace(viability=SimpleNamespace(run_session_viability_probe=_viability_ok))
    with patch("app.devices.routers.control.get_device_for_update_or_404", new=AsyncMock(return_value=device)):
        assert await devices_control.device_session_test(
            device_id,
            db=object(),
            session_services=_session_svc_ok,
        ) == {"status": "passed"}


async def test_devices_control_reconnect_lifecycle_health_and_logs_paths() -> None:
    device_id = uuid.uuid4()
    lifecycle_actions = [{"id": "reconnect"}, {"id": "state"}]
    resolved = SimpleNamespace(lifecycle_actions=lifecycle_actions)
    device = _control_device(id=device_id)
    settings_services = _mock_settings_svc(FakeSettingsReader({}))
    _reconnect_ac = SimpleNamespace(circuit_breaker=Mock(), http_pool=None)
    _mock_ds_reconnect = SimpleNamespace(crud=AsyncMock(), publisher=event_bus)

    _noop_appium_svc = SimpleNamespace(reconciler_agent=AsyncMock())
    with (
        patch("app.devices.routers.control.get_device_or_404", new=AsyncMock(return_value=device)),
        patch("app.devices.routers.control.resolve_pack_platform", new=AsyncMock(side_effect=LookupError("missing"))),
    ):
        with pytest.raises(HTTPException) as exc:
            await devices_control.reconnect_device(
                device_id,
                db=object(),
                device_services=_mock_ds_reconnect,
                settings_services=settings_services,
                agent_comm=_reconnect_ac,
                appium_services=_noop_appium_svc,
            )
    assert exc.value.status_code == 400

    for bad_device, detail in (
        (_control_device(connection_type=ConnectionType.usb), "network-connected"),
        (_control_device(ip_address=None), "no IP"),
        (_control_device(host=None), "no host"),
        (_control_device(connection_target=None), "no connection target"),
    ):
        with (
            patch("app.devices.routers.control.get_device_or_404", new=AsyncMock(return_value=bad_device)),
            patch("app.devices.routers.control.resolve_pack_platform", new=AsyncMock(return_value=resolved)),
            patch("app.devices.routers.control.platform_has_lifecycle_action", new=Mock(return_value=True)),
        ):
            with pytest.raises(HTTPException) as exc:
                await devices_control.reconnect_device(
                    device_id,
                    db=object(),
                    device_services=_mock_ds_reconnect,
                    settings_services=settings_services,
                    agent_comm=_reconnect_ac,
                    appium_services=_noop_appium_svc,
                )
        assert detail in str(exc.value.detail)

    with (
        patch("app.devices.routers.control.get_device_or_404", new=AsyncMock(return_value=device)),
        patch("app.devices.routers.control.resolve_pack_platform", new=AsyncMock(return_value=resolved)),
        patch("app.devices.routers.control.platform_has_lifecycle_action", new=Mock(return_value=False)),
    ):
        with pytest.raises(HTTPException) as exc:
            await devices_control.reconnect_device(
                device_id,
                db=object(),
                device_services=_mock_ds_reconnect,
                settings_services=settings_services,
                agent_comm=_reconnect_ac,
                appium_services=_noop_appium_svc,
            )
    assert "not supported" in str(exc.value.detail)

    # Phase 2: narrowed except — RuntimeError is NOT NodeManagerError and must bubble, not become 502
    auto_device = _control_device(appium_node=SimpleNamespace(observed_running=False))
    auto_db = SimpleNamespace(commit=AsyncMock(), flush=AsyncMock())
    ra_start_boom = AsyncMock()
    ra_start_boom.start_node = AsyncMock(side_effect=RuntimeError("boom"))
    with (
        patch("app.devices.routers.control.get_device_or_404", new=AsyncMock(return_value=auto_device)),
        patch("app.devices.routers.control.resolve_pack_platform", new=AsyncMock(return_value=resolved)),
        patch("app.devices.routers.control.platform_has_lifecycle_action", new=Mock(return_value=True)),
        patch(
            "app.devices.services.link_repair.pack_device_lifecycle_action",
            new=AsyncMock(return_value={"success": True}),
        ),
        patch.object(IntentService, "revoke_intents_and_reconcile", new=AsyncMock()),
    ):
        with pytest.raises(RuntimeError, match="boom"):
            await devices_control.reconnect_device(
                device_id,
                db=auto_db,
                device_services=_mock_ds_reconnect,
                settings_services=settings_services,
                agent_comm=_reconnect_ac,
                appium_services=SimpleNamespace(reconciler_agent=ra_start_boom),
            )  # type: ignore[arg-type]

    with (
        patch("app.devices.routers.control.get_device_or_404", new=AsyncMock(return_value=device)),
        patch("app.devices.routers.control.resolve_pack_platform", new=AsyncMock(return_value=resolved)),
        patch("app.devices.routers.control.platform_has_lifecycle_action", new=Mock(return_value=True)),
        patch(
            "app.devices.services.link_repair.pack_device_lifecycle_action",
            new=AsyncMock(return_value={"success": True}),
        ),
    ):
        reconnect = await devices_control.reconnect_device(
            device_id,
            db=object(),
            device_services=_mock_ds_reconnect,
            settings_services=settings_services,
            agent_comm=_reconnect_ac,
            appium_services=SimpleNamespace(reconciler_agent=AsyncMock()),
        )
    assert reconnect["message"] == "Reconnected"

    _ctrl_ss = _mock_settings_svc(FakeSettingsReader({}))
    _lifecycle_ac = SimpleNamespace(circuit_breaker=Mock(), http_pool=None)
    with (
        patch("app.devices.routers.control.get_device_for_update_or_404", new=AsyncMock(return_value=device)),
        patch("app.devices.routers.control.resolve_pack_platform", new=AsyncMock(side_effect=LookupError("missing"))),
    ):
        with pytest.raises(HTTPException) as exc:
            await devices_control.device_lifecycle_action(
                device_id,
                "state",
                db=object(),
                settings_services=_ctrl_ss,
                agent_comm=_lifecycle_ac,
                device_services=AsyncMock(),
            )
    assert exc.value.status_code == 400

    for bad_device, detail in (
        (_control_device(host=None), "no host"),
        (_control_device(connection_target=None), "no connection target"),
    ):
        with (
            patch("app.devices.routers.control.get_device_for_update_or_404", new=AsyncMock(return_value=bad_device)),
            patch("app.devices.routers.control.resolve_pack_platform", new=AsyncMock(return_value=resolved)),
            patch("app.devices.routers.control.platform_has_lifecycle_action", new=Mock(return_value=True)),
        ):
            with pytest.raises(HTTPException) as exc:
                await devices_control.device_lifecycle_action(
                    device_id,
                    "state",
                    db=object(),
                    settings_services=_ctrl_ss,
                    agent_comm=_lifecycle_ac,
                    device_services=AsyncMock(),
                )
        assert detail in str(exc.value.detail)

    db = SimpleNamespace(commit=AsyncMock())
    with (
        patch("app.devices.routers.control.get_device_for_update_or_404", new=AsyncMock(return_value=device)),
        patch("app.devices.routers.control.resolve_pack_platform", new=AsyncMock(return_value=resolved)),
        patch("app.devices.routers.control.platform_has_lifecycle_action", new=Mock(return_value=True)),
        patch(
            "app.devices.routers.control.pack_device_lifecycle_action", new=AsyncMock(return_value={"state": "running"})
        ),
    ):
        _ds_health = AsyncMock()
        _ds_health.update_emulator_state = AsyncMock()
        _ds_stub = AsyncMock()
        _ds_stub.health = _ds_health
        assert await devices_control.device_lifecycle_action(
            device_id,
            "state",
            db=db,
            settings_services=_ctrl_ss,
            agent_comm=_lifecycle_ac,
            device_services=_ds_stub,
        ) == {"state": "running"}
    db.commit.assert_awaited_once()

    node = SimpleNamespace(port=4731, observed_running=True, health_running=None, health_state=None)
    health_device = _control_device(appium_node=node)
    _mock_viability = AsyncMock()
    _mock_viability.get_session_viability = AsyncMock(return_value=None)
    _mock_session_services = SimpleNamespace(viability=_mock_viability)
    with (
        patch("app.devices.routers.control.get_device_or_404", new=AsyncMock(return_value=health_device)),
        patch("app.devices.routers.control.require_management_host", new=Mock(return_value=health_device.host)),
        patch("app.devices.routers.control.fetch_appium_status", new=AsyncMock(return_value={"running": False})),
        patch("app.devices.routers.control.fetch_pack_device_health", new=AsyncMock(return_value={"healthy": True})),
        patch(
            "app.devices.routers.control.lifecycle_policy_summary.build_lifecycle_policy",
            new=AsyncMock(return_value={}),
        ),
    ):
        health = await devices_control.device_health(
            device_id,
            db=object(),
            device_services=_mock_ds_reconnect,
            settings_services=_ctrl_ss,
            agent_comm=SimpleNamespace(circuit_breaker=Mock(), http_pool=None),
            session_services=_mock_session_services,
        )
    assert health["node"]["state"] == "error"
    assert health["healthy"] is False

    _logs_ac = SimpleNamespace(circuit_breaker=Mock(), http_pool=None)
    with (
        patch(
            "app.devices.routers.control.get_device_or_404",
            new=AsyncMock(return_value=_control_device(appium_node=None)),
        ),
        patch("app.devices.routers.control.require_management_host", new=Mock(return_value=device.host)),
    ):
        assert await devices_control.device_logs(
            device_id, db=object(), device_services=_mock_ds_reconnect, settings_services=_ctrl_ss, agent_comm=_logs_ac
        ) == {"lines": [], "count": 0}

    with (
        patch("app.devices.routers.control.get_device_or_404", new=AsyncMock(return_value=health_device)),
        patch("app.devices.routers.control.require_management_host", new=Mock(return_value=health_device.host)),
        patch("app.devices.routers.control.appium_logs", new=AsyncMock(side_effect=httpx.HTTPError("down"))),
    ):
        with pytest.raises(HTTPException) as exc:
            await devices_control.device_logs(
                device_id,
                db=object(),
                device_services=_mock_ds_reconnect,
                settings_services=_ctrl_ss,
                agent_comm=_logs_ac,
            )
    assert exc.value.status_code == 502

    plugin_id = uuid.uuid4()
    fake_ps_delete = SimpleNamespace(plugin=SimpleNamespace(delete_plugin=AsyncMock(return_value=False)))
    with pytest.raises(HTTPException) as exc:
        await plugins_router.delete_plugin(plugin_id, db=object(), plugin_services=fake_ps_delete)  # type: ignore[arg-type]
    assert exc.value.status_code == 404

    fake_hs_none2 = SimpleNamespace(crud=SimpleNamespace(get_host=AsyncMock(return_value=None)))
    fake_ps_none = SimpleNamespace(plugin=SimpleNamespace())
    with pytest.raises(HTTPException) as exc:
        await plugins_router.host_plugins(
            plugin_id, db=object(), plugin_services=fake_ps_none, host_services=fake_hs_none2
        )  # type: ignore[arg-type]
    assert exc.value.status_code == 404

    host = SimpleNamespace(id=plugin_id)
    fake_plugin_svc = SimpleNamespace(
        list_plugins=AsyncMock(return_value=["required"]),
        get_host_plugin_statuses=AsyncMock(return_value=[{"status": "ok"}]),
        sync_host_plugins=AsyncMock(return_value={"installed": []}),
    )
    fake_ps_full = SimpleNamespace(plugin=fake_plugin_svc)
    fake_hs_host2 = SimpleNamespace(crud=SimpleNamespace(get_host=AsyncMock(return_value=host)))
    statuses = await plugins_router.host_plugins(  # type: ignore[arg-type]
        plugin_id, db=object(), plugin_services=fake_ps_full, host_services=fake_hs_host2
    )
    assert statuses == [{"status": "ok"}]
    fake_hs_host3 = SimpleNamespace(crud=SimpleNamespace(get_host=AsyncMock(return_value=host)))
    sync_result = await plugins_router.sync_host_plugins(  # type: ignore[arg-type]
        plugin_id, db=object(), plugin_services=fake_ps_full, host_services=fake_hs_host3
    )
    assert sync_result == {"installed": []}


async def test_devices_control_reconnect_revokes_stale_recovery_intents() -> None:
    device_id = uuid.uuid4()
    resolved = SimpleNamespace(lifecycle_actions=[{"id": "reconnect"}])
    node = SimpleNamespace(observed_running=False)
    device = _control_device(
        id=device_id,
        appium_node=node,
        session_viability_status="failed",
        session_viability_error="Appium node is not running",
        recovery_allowed=False,
        recovery_blocked_reason="Node health failure",
    )
    db = SimpleNamespace(commit=AsyncMock(), flush=AsyncMock())
    revoke = AsyncMock()
    start_node = AsyncMock()
    mock_reconciler_agent_ctrl = AsyncMock()
    mock_reconciler_agent_ctrl.start_node = start_node

    with (
        patch("app.devices.routers.control.get_device_or_404", new=AsyncMock(return_value=device)),
        patch("app.devices.routers.control.resolve_pack_platform", new=AsyncMock(return_value=resolved)),
        patch("app.devices.routers.control.platform_has_lifecycle_action", new=Mock(return_value=True)),
        patch(
            "app.devices.services.link_repair.pack_device_lifecycle_action",
            new=AsyncMock(return_value={"success": True}),
        ),
        patch.object(IntentService, "revoke_intents_and_reconcile", new=revoke),
    ):
        reconnect = await devices_control.reconnect_device(
            device_id,
            db=db,
            device_services=SimpleNamespace(crud=AsyncMock(), publisher=event_bus),
            settings_services=_mock_settings_svc(FakeSettingsReader({})),
            agent_comm=SimpleNamespace(circuit_breaker=Mock(), http_pool=None),
            appium_services=SimpleNamespace(reconciler_agent=mock_reconciler_agent_ctrl),
        )  # type: ignore[arg-type]

    assert reconnect["message"] == "Reconnected"
    assert device.session_viability_status is None
    assert device.session_viability_error is None
    revoke.assert_awaited_once_with(
        device_id=device_id,
        sources=[
            f"connectivity:{device_id}",
            f"health_failure:node:{device_id}",
            f"health_failure:recovery:{device_id}",
        ],
        reason="Operator reconnect succeeded",
        publisher=event_bus,
    )
    assert device.recovery_allowed is False
    assert device.recovery_blocked_reason == "Node health failure"
    db.commit.assert_awaited_once()
    start_node.assert_awaited_once()


async def test_analytics_router_uses_defaults_csv_and_capacity_errors() -> None:
    summary = [
        analytics.SessionSummaryRow(
            group_key="android_mobile",
            total=1,
            passed=1,
            failed=0,
            error=0,
            avg_duration_sec=10.0,
        )
    ]
    utilization = [
        DeviceUtilizationRow(
            device_id=str(uuid.uuid4()),
            device_name="Pixel",
            platform_id="android_mobile",
            total_session_time_sec=60,
            idle_time_sec=0,
            busy_pct=100,
            session_count=1,
        )
    ]
    reliability = [
        DeviceReliabilityRow(
            device_id=str(uuid.uuid4()),
            device_name="Pixel",
            platform_id="android_mobile",
            health_check_failures=1,
            connectivity_losses=0,
            node_crashes=0,
            total_incidents=1,
        )
    ]

    mock_raising_timeline = AsyncMock(side_effect=ValueError("date_to must be after date_from"))
    mock_fleet_capacity = SimpleNamespace(get_fleet_capacity_timeline=mock_raising_timeline)
    mock_device_services = SimpleNamespace(fleet_capacity=mock_fleet_capacity)
    with (
        patch("app.analytics.router.analytics_service.get_session_summary", new=AsyncMock(return_value=summary)),
        patch("app.analytics.router.analytics_service.get_device_utilization", new=AsyncMock(return_value=utilization)),
        patch("app.analytics.router.analytics_service.get_device_reliability", new=AsyncMock(return_value=reliability)),
    ):
        assert await analytics.session_summary(db=object(), group_by=GroupByOption.platform) == summary
        csv_response = await analytics.session_summary(db=object(), group_by=GroupByOption.day, export_format="csv")
        assert csv_response.media_type == "text/csv"
        assert (await analytics.device_utilization(db=object(), export_format="csv")).media_type == "text/csv"
        assert (await analytics.device_reliability(db=object(), export_format="csv")).media_type == "text/csv"
        with pytest.raises(HTTPException) as exc:
            await analytics.fleet_capacity_timeline(db=object(), bucket_minutes=5, device_services=mock_device_services)
    assert exc.value.status_code == 422


async def test_grid_router_summarizes_registry_and_queue() -> None:
    device_one_id = uuid.uuid4()
    running_node = SimpleNamespace(port=4731, observed_running=True)
    stopped_node = SimpleNamespace(port=4732, observed_running=False)
    devices = [
        SimpleNamespace(
            id=device_one_id,
            identity_value="serial-1",
            connection_target="serial-1",
            name="Pixel",
            platform_id="android_mobile",
            operational_state=SimpleNamespace(value="available"),
            appium_node=running_node,
        ),
        SimpleNamespace(
            id=uuid.uuid4(),
            identity_value="serial-2",
            connection_target="serial-2",
            name="Tablet",
            platform_id="android_mobile",
            operational_state=SimpleNamespace(value="offline"),
            appium_node=stopped_node,
        ),
    ]
    run_id = uuid.uuid4()
    ticket = SimpleNamespace(
        id="queued",
        requested_body={"capabilities": {"alwaysMatch": {"platformName": "android"}}},
        created_at=datetime(2026, 6, 5, tzinfo=UTC),
        run_id=run_id,
    )

    fake_device_services = SimpleNamespace(crud=SimpleNamespace(list_devices=AsyncMock(return_value=devices)))
    db = object()
    with (
        patch.object(grid, "_live_sessions_by_device", AsyncMock(return_value={device_one_id: ["s1"]})),
        patch.object(grid, "_waiting_tickets", AsyncMock(return_value=[ticket])),
    ):
        status = await grid.grid_status(db=db, device_services=fake_device_services)
        queue = await grid.grid_queue(db=db)

    assert status["registry"]["device_count"] == 2
    assert status["registry"]["devices"][0]["node_state"] == "running"
    assert "hold" not in status["registry"]["devices"][1]
    assert status["registry"]["devices"][1]["operational_state"] == "offline"
    assert status["grid"]["value"]["nodes"] == [{"slots": [{"session": "s1"}]}]
    assert status["active_sessions"] == 1
    assert status["queue_size"] == 1
    assert queue["queue_size"] == 1
    assert queue["requests"][0]["requestId"] == "queued"
    assert queue["requests"][0]["capabilities"] == {"platformName": "android"}
    assert queue["requests"][0]["runId"] == str(run_id)


async def test_nodes_router_validation_branches() -> None:
    device_id = uuid.uuid4()
    device = SimpleNamespace(
        id=device_id, hold=None, appium_node=None, host_id=uuid.uuid4(), lifecycle_policy_state=None
    )

    with patch(
        "app.appium_nodes.routers.nodes.run_service.get_device_reservation",
        new=AsyncMock(return_value=SimpleNamespace(name="run", id="r1")),
    ):
        with pytest.raises(HTTPException) as exc:
            await nodes_router._assert_device_not_reserved(device, db=object())
    assert exc.value.status_code == 409

    device.lifecycle_policy_state = {"maintenance_reason": "manual"}
    with pytest.raises(HTTPException) as exc:
        nodes_router._assert_startable_outside_maintenance(device)
    assert exc.value.status_code == 409
    device.lifecycle_policy_state = None

    setup_required = SimpleNamespace(readiness_state="setup_required", missing_setup_fields=["identity_value"])
    with patch("app.appium_nodes.routers.nodes.assess_device_async", new=AsyncMock(return_value=setup_required)):
        with pytest.raises(HTTPException) as exc:
            await nodes_router._assert_device_verified(object(), device, action="start")
    assert "identity_value" in str(exc.value.detail)

    unverified = SimpleNamespace(readiness_state="failed", missing_setup_fields=[])
    with patch("app.appium_nodes.routers.nodes.assess_device_async", new=AsyncMock(return_value=unverified)):
        with pytest.raises(HTTPException) as exc:
            await nodes_router._assert_device_verified(object(), device, action="start")
    assert exc.value.status_code == 409

    running_node = SimpleNamespace(desired_state=AppiumDesiredState.running, observed_running=True)
    device.appium_node = running_node
    with (
        patch("app.appium_nodes.routers.nodes.get_device_for_update_or_404", new=AsyncMock(return_value=device)),
        patch("app.appium_nodes.routers.nodes.run_service.get_device_reservation", new=AsyncMock(return_value=None)),
        patch(
            "app.appium_nodes.routers.nodes.assess_device_async",
            new=AsyncMock(return_value=SimpleNamespace(readiness_state="verified", missing_setup_fields=[])),
        ),
    ):
        with pytest.raises(HTTPException) as exc:
            await nodes_router.start_node(
                device_id,
                db=object(),
                appium_services=SimpleNamespace(reconciler_agent=AsyncMock()),
            )
    assert exc.value.status_code == 409

    device.appium_node = None
    device.host_id = None
    with (
        patch("app.appium_nodes.routers.nodes.get_device_for_update_or_404", new=AsyncMock(return_value=device)),
        patch("app.appium_nodes.routers.nodes.run_service.get_device_reservation", new=AsyncMock(return_value=None)),
        patch(
            "app.appium_nodes.routers.nodes.assess_device_async",
            new=AsyncMock(return_value=SimpleNamespace(readiness_state="verified", missing_setup_fields=[])),
        ),
        patch("app.appium_nodes.routers.nodes.is_ready_for_use_async", new=AsyncMock(return_value=True)),
    ):
        with pytest.raises(HTTPException) as exc:
            await nodes_router.start_node(
                device_id,
                db=object(),
                appium_services=SimpleNamespace(reconciler_agent=AsyncMock()),
            )
    assert "no host assigned" in str(exc.value.detail)

    device.host_id = uuid.uuid4()
    started_node = SimpleNamespace(desired_state=AppiumDesiredState.running)
    mock_reconciler_agent = AsyncMock()
    mock_reconciler_agent.start_node = AsyncMock(return_value=started_node)
    with (
        patch("app.appium_nodes.routers.nodes.get_device_for_update_or_404", new=AsyncMock(return_value=device)),
        patch("app.appium_nodes.routers.nodes.run_service.get_device_reservation", new=AsyncMock(return_value=None)),
        patch(
            "app.appium_nodes.routers.nodes.assess_device_async",
            new=AsyncMock(return_value=SimpleNamespace(readiness_state="verified", missing_setup_fields=[])),
        ),
        patch("app.appium_nodes.routers.nodes.is_ready_for_use_async", new=AsyncMock(return_value=True)),
    ):
        assert (
            await nodes_router.start_node(
                device_id,
                db=object(),
                appium_services=SimpleNamespace(reconciler_agent=mock_reconciler_agent),
            )
            is started_node
        )


async def test_nodes_stop_and_restart_error_and_convergence_paths() -> None:
    device_id = uuid.uuid4()
    stopped_device = SimpleNamespace(id=device_id, hold=None, appium_node=None)
    with (
        patch(
            "app.appium_nodes.routers.nodes.get_device_for_update_or_404", new=AsyncMock(return_value=stopped_device)
        ),
        patch("app.appium_nodes.routers.nodes.run_service.get_device_reservation", new=AsyncMock(return_value=None)),
    ):
        with pytest.raises(HTTPException) as exc:
            await nodes_router.stop_node(
                device_id, db=object(), appium_services=SimpleNamespace(reconciler_agent=AsyncMock())
            )
    assert exc.value.status_code == 400

    running_node = SimpleNamespace(desired_state=AppiumDesiredState.running)
    running_device = SimpleNamespace(id=device_id, hold=None, appium_node=running_node, lifecycle_policy_state=None)
    restarted = SimpleNamespace(id=uuid.uuid4())
    fake_db = SimpleNamespace(refresh=AsyncMock())
    mock_ra_restart = AsyncMock()
    mock_ra_restart.restart_node = AsyncMock(return_value=restarted)
    with (
        patch(
            "app.appium_nodes.routers.nodes.get_device_for_update_or_404", new=AsyncMock(return_value=running_device)
        ),
        patch("app.appium_nodes.routers.nodes.run_service.get_device_reservation", new=AsyncMock(return_value=None)),
        patch(
            "app.appium_nodes.routers.nodes.assess_device_async",
            new=AsyncMock(return_value=SimpleNamespace(readiness_state="verified", missing_setup_fields=[])),
        ),
    ):
        assert (
            await nodes_router.restart_node(
                device_id,
                db=fake_db,
                appium_services=SimpleNamespace(
                    reconciler_agent=mock_ra_restart,
                    reconciler=SimpleNamespace(
                        converge_device_now=AsyncMock(side_effect=RuntimeError("converge failed"))
                    ),
                ),
            )
            is restarted
        )
    fake_db.refresh.assert_awaited_once_with(restarted)


async def test_nodes_router_additional_start_stop_restart_branches() -> None:
    device_id = uuid.uuid4()
    verified = SimpleNamespace(readiness_state="verified", missing_setup_fields=[])

    device = SimpleNamespace(
        id=device_id, hold=None, appium_node=None, host_id=uuid.uuid4(), lifecycle_policy_state=None
    )
    with (
        patch("app.appium_nodes.routers.nodes.get_device_for_update_or_404", new=AsyncMock(return_value=device)),
        patch("app.appium_nodes.routers.nodes.run_service.get_device_reservation", new=AsyncMock(return_value=None)),
        patch("app.appium_nodes.routers.nodes.assess_device_async", new=AsyncMock(return_value=verified)),
        patch("app.appium_nodes.routers.nodes.is_ready_for_use_async", new=AsyncMock(return_value=False)),
        patch("app.appium_nodes.routers.nodes.readiness_error_detail_async", new=AsyncMock(return_value="not ready")),
    ):
        with pytest.raises(HTTPException) as exc:
            await nodes_router.start_node(
                device_id,
                db=object(),
                appium_services=SimpleNamespace(reconciler_agent=AsyncMock()),
            )
    assert exc.value.status_code == 400
    assert exc.value.detail == "not ready"

    # Phase 2: narrowed except — RuntimeError is NOT NodeManagerError and must bubble, not become 400
    ra_boom = AsyncMock()
    ra_boom.start_node = AsyncMock(side_effect=RuntimeError("boom"))
    with (
        patch("app.appium_nodes.routers.nodes.get_device_for_update_or_404", new=AsyncMock(return_value=device)),
        patch("app.appium_nodes.routers.nodes.run_service.get_device_reservation", new=AsyncMock(return_value=None)),
        patch("app.appium_nodes.routers.nodes.assess_device_async", new=AsyncMock(return_value=verified)),
        patch("app.appium_nodes.routers.nodes.is_ready_for_use_async", new=AsyncMock(return_value=True)),
    ):
        with pytest.raises(RuntimeError, match="boom"):
            await nodes_router.start_node(
                device_id,
                db=object(),
                appium_services=SimpleNamespace(reconciler_agent=ra_boom),
            )

    running_node = SimpleNamespace(desired_state=AppiumDesiredState.running)
    running_device = SimpleNamespace(
        id=device_id, hold=None, appium_node=running_node, host_id=uuid.uuid4(), lifecycle_policy_state=None
    )
    stopped_node = SimpleNamespace(desired_state=AppiumDesiredState.stopped)
    ra_stop = AsyncMock()
    ra_stop.stop_node = AsyncMock(return_value=stopped_node)
    with (
        patch(
            "app.appium_nodes.routers.nodes.get_device_for_update_or_404", new=AsyncMock(return_value=running_device)
        ),
        patch("app.appium_nodes.routers.nodes.run_service.get_device_reservation", new=AsyncMock(return_value=None)),
    ):
        assert (
            await nodes_router.stop_node(
                device_id, db=object(), appium_services=SimpleNamespace(reconciler_agent=ra_stop)
            )
            is stopped_node
        )

    # Phase 2: narrowed except — RuntimeError is NOT NodeManagerError and must bubble, not become 400
    ra_stop_fail = AsyncMock()
    ra_stop_fail.stop_node = AsyncMock(side_effect=RuntimeError("stop failed"))
    with (
        patch(
            "app.appium_nodes.routers.nodes.get_device_for_update_or_404", new=AsyncMock(return_value=running_device)
        ),
        patch("app.appium_nodes.routers.nodes.run_service.get_device_reservation", new=AsyncMock(return_value=None)),
    ):
        with pytest.raises(RuntimeError, match="stop failed"):
            await nodes_router.stop_node(
                device_id, db=object(), appium_services=SimpleNamespace(reconciler_agent=ra_stop_fail)
            )

    fallback_started = SimpleNamespace(desired_state=AppiumDesiredState.running)
    non_running_device = SimpleNamespace(
        id=device_id,
        hold=None,
        appium_node=SimpleNamespace(desired_state=AppiumDesiredState.stopped),
        host_id=uuid.uuid4(),
        lifecycle_policy_state=None,
    )
    ra_fallback = AsyncMock()
    ra_fallback.start_node = AsyncMock(return_value=fallback_started)
    with (
        patch(
            "app.appium_nodes.routers.nodes.get_device_for_update_or_404",
            new=AsyncMock(return_value=non_running_device),
        ),
        patch("app.appium_nodes.routers.nodes.run_service.get_device_reservation", new=AsyncMock(return_value=None)),
        patch("app.appium_nodes.routers.nodes.assess_device_async", new=AsyncMock(return_value=verified)),
        patch("app.appium_nodes.routers.nodes.is_ready_for_use_async", new=AsyncMock(return_value=True)),
    ):
        assert (
            await nodes_router.restart_node(
                device_id,
                db=object(),
                appium_services=SimpleNamespace(
                    reconciler_agent=ra_fallback,
                    reconciler=SimpleNamespace(converge_device_now=AsyncMock(return_value=None)),
                ),
            )
            is fallback_started
        )

    restarted = SimpleNamespace(id=uuid.uuid4())
    converged = SimpleNamespace(id=uuid.uuid4())
    fake_db = SimpleNamespace(refresh=AsyncMock())
    ra_restart = AsyncMock()
    ra_restart.restart_node = AsyncMock(return_value=restarted)
    with (
        patch(
            "app.appium_nodes.routers.nodes.get_device_for_update_or_404", new=AsyncMock(return_value=running_device)
        ),
        patch("app.appium_nodes.routers.nodes.run_service.get_device_reservation", new=AsyncMock(return_value=None)),
        patch("app.appium_nodes.routers.nodes.assess_device_async", new=AsyncMock(return_value=verified)),
    ):
        assert (
            await nodes_router.restart_node(
                device_id,
                db=fake_db,
                appium_services=SimpleNamespace(
                    reconciler_agent=ra_restart,
                    reconciler=SimpleNamespace(converge_device_now=AsyncMock(return_value=converged)),
                ),
            )
            is converged
        )
    fake_db.refresh.assert_awaited_once_with(converged)


async def test_device_group_router_bulk_and_membership_branches() -> None:
    group_id = uuid.uuid4()
    device_ids = [uuid.uuid4()]

    ds_empty = SimpleNamespace(groups=SimpleNamespace(get_group_device_ids=AsyncMock(return_value=[])))
    with pytest.raises(HTTPException) as exc:
        await device_groups._group_device_ids_or_404(object(), group_id, ds_empty)
    assert exc.value.status_code == 404

    ds_dynamic = SimpleNamespace(groups=SimpleNamespace(get_group=AsyncMock(return_value={"group_type": "dynamic"})))
    with pytest.raises(HTTPException) as exc:
        await device_groups.add_members(
            group_id,
            body=SimpleNamespace(device_ids=device_ids),
            db=object(),
            device_services=ds_dynamic,
        )
    assert exc.value.status_code == 400
    with pytest.raises(HTTPException) as exc:
        await device_groups.remove_members(
            group_id,
            body=SimpleNamespace(device_ids=device_ids),
            db=object(),
            device_services=ds_dynamic,
        )
    assert exc.value.status_code == 400

    async def assert_bulk(
        call: Callable[..., Awaitable[dict[str, Any]]],
        bulk_method: str,
        *args: object,
    ) -> None:
        mock_bulk = AsyncMock(**{bulk_method: AsyncMock(return_value={"ok": 1})})
        ds = SimpleNamespace(
            groups=SimpleNamespace(get_group_device_ids=AsyncMock(return_value=device_ids)),
            bulk=mock_bulk,
        )
        assert await call(group_id, *args, db=object(), device_services=ds) == {"ok": 1}

    await assert_bulk(device_groups.group_bulk_start, "bulk_start_nodes")
    await assert_bulk(device_groups.group_bulk_stop, "bulk_stop_nodes")
    await assert_bulk(device_groups.group_bulk_restart, "bulk_restart_nodes")
    await assert_bulk(
        device_groups.group_bulk_enter_maintenance,
        "bulk_enter_maintenance",
        BulkMaintenanceEnter(device_ids=device_ids),
    )
    await assert_bulk(device_groups.group_bulk_exit_maintenance, "bulk_exit_maintenance")
    mock_bulk_reconnect = AsyncMock(bulk_reconnect=AsyncMock(return_value={"ok": 1}))
    ds_reconnect = SimpleNamespace(
        groups=SimpleNamespace(get_group_device_ids=AsyncMock(return_value=device_ids)),
        bulk=mock_bulk_reconnect,
    )
    assert await device_groups.group_bulk_reconnect(group_id, db=object(), device_services=ds_reconnect) == {"ok": 1}
    await assert_bulk(
        device_groups.group_bulk_update_tags,
        "bulk_update_tags",
        BulkTagsUpdate(device_ids=device_ids, tags={"lab": "east"}, merge=True),
    )
    await assert_bulk(device_groups.group_bulk_delete, "bulk_delete")


async def test_driver_pack_upload_export_error_mapping() -> None:
    assert await driver_pack_uploads._read_limited_upload(ChunkUpload([b"abc", b"def"])) == b"abcdef"

    with patch("app.packs.routers.uploads.MAX_PACK_TARBALL_BYTES", new=3):
        with pytest.raises(HTTPException) as exc:
            await driver_pack_uploads._read_limited_upload(ChunkUpload([b"abcd"]))
    assert exc.value.status_code == 413

    with pytest.raises(HTTPException) as exc:
        await driver_pack_uploads.upload(
            tarball=ChunkUpload([]),  # type: ignore[arg-type]
            username="admin",
            session=DummySession(),
            packs=SimpleNamespace(release=object()),
        )
    assert exc.value.status_code == 400

    mock_packs_none = SimpleNamespace(release=SimpleNamespace(list_releases=AsyncMock(return_value=None)))
    with pytest.raises(HTTPException) as exc:
        await driver_pack_uploads.list_releases("missing", session=object(), packs=mock_packs_none)
    assert exc.value.status_code == 404
    mock_packs_releases = SimpleNamespace(release=SimpleNamespace(list_releases=AsyncMock(return_value="releases")))
    assert await driver_pack_uploads.list_releases("pack", session=object(), packs=mock_packs_releases) == "releases"

    pack = SimpleNamespace(id="local/uploaded")
    session = DummySession()
    mock_packs_upload = SimpleNamespace(release=SimpleNamespace(upload=AsyncMock(return_value=pack)))
    with patch("app.packs.routers.uploads.build_pack_out", new=Mock(return_value={"id": pack.id})):
        assert await driver_pack_uploads.upload(
            tarball=ChunkUpload([b"tar"]),  # type: ignore[arg-type]
            username="admin",
            session=session,
            packs=mock_packs_upload,
        ) == {"id": "local/uploaded"}
    assert session.committed is True

    for error, status_code in (
        (driver_pack_uploads.PackUploadValidationError("bad manifest"), 400),
        (driver_pack_uploads.PackUploadConflictError("duplicate"), 409),
    ):
        mock_packs_err = SimpleNamespace(release=SimpleNamespace(upload=AsyncMock(side_effect=error)))
        with pytest.raises(HTTPException) as exc:
            await driver_pack_uploads.upload(
                tarball=ChunkUpload([b"tar"]),  # type: ignore[arg-type]
                username="admin",
                session=DummySession(),
                packs=mock_packs_err,
            )
        assert exc.value.status_code == status_code


async def test_driver_pack_upload_tarball_and_release_mutations(tmp_path: Path) -> None:
    missing_session = DummySession(execute_result=ScalarResult(None))
    with pytest.raises(HTTPException) as exc:
        await driver_pack_uploads.fetch_tarball("pack", "1.0.0", session=missing_session)
    assert exc.value.status_code == 404

    no_artifact = DummySession(execute_result=ScalarResult(SimpleNamespace(artifact_path=None)))
    with pytest.raises(HTTPException) as exc:
        await driver_pack_uploads.fetch_tarball("pack", "1.0.0", session=no_artifact)
    assert exc.value.status_code == 404

    missing_path = DummySession(execute_result=ScalarResult(SimpleNamespace(artifact_path=str(tmp_path / "nope.tgz"))))
    with pytest.raises(HTTPException) as exc:
        await driver_pack_uploads.fetch_tarball("pack", "1.0.0", session=missing_path)
    assert exc.value.status_code == 404

    artifact_path = tmp_path / "pack.tgz"
    artifact_path.write_bytes(b"tgz")
    response = await driver_pack_uploads.fetch_tarball(
        "pack",
        "1.0.0",
        session=DummySession(execute_result=ScalarResult(SimpleNamespace(artifact_path=str(artifact_path)))),
    )
    assert response.path == str(artifact_path)

    pack = SimpleNamespace(id="local/uploaded")
    session = DummySession()
    mock_packs_current = SimpleNamespace(release=SimpleNamespace(set_current_release=AsyncMock(return_value=pack)))
    with patch("app.packs.routers.uploads.build_pack_out", new=Mock(return_value={"id": pack.id})):
        assert await driver_pack_uploads.update_current_release(
            "pack",
            CurrentReleasePatch(release="1.0.0"),
            _username="admin",
            session=session,
            packs=mock_packs_current,
        ) == {"id": "local/uploaded"}
    assert session.committed is True

    for error, status_code in (
        (LookupError("missing"), 404),
        (ValueError("current"), 400),
        (RuntimeError("busy"), 409),
    ):
        mock_packs_del_err = SimpleNamespace(release=SimpleNamespace(delete_release=AsyncMock(side_effect=error)))
        with pytest.raises(HTTPException) as exc:
            await driver_pack_uploads.delete_release(
                "pack", "1.0.0", _username="admin", session=DummySession(), packs=mock_packs_del_err
            )
        assert exc.value.status_code == status_code

    delete_session = DummySession()
    mock_packs_del_ok = SimpleNamespace(release=SimpleNamespace(delete_release=AsyncMock(return_value=None)))
    response = await driver_pack_uploads.delete_release(
        "pack", "1.0.0", _username="admin", session=delete_session, packs=mock_packs_del_ok
    )
    assert response.status_code == 204
    assert delete_session.committed is True


async def test_driver_pack_router_error_mapping_and_success_paths() -> None:
    pack_id = "local/router-pack"
    pack_out = SimpleNamespace(id=pack_id)

    mock_catalog_none = SimpleNamespace(
        list_catalog=AsyncMock(return_value={"packs": []}),
        get_pack_detail=AsyncMock(side_effect=[None, pack_out, None, pack_out]),
    )
    mock_status_hosts = SimpleNamespace(
        get_driver_pack_host_status=AsyncMock(return_value={"pack_id": pack_id, "hosts": []}),
    )
    mock_packs_catalog = SimpleNamespace(catalog=mock_catalog_none, status=mock_status_hosts)
    assert await driver_packs.catalog(session=object(), packs=mock_packs_catalog) == {"packs": []}
    with pytest.raises(HTTPException) as exc:
        await driver_packs.get_pack(pack_id, session=object(), packs=mock_packs_catalog)
    assert exc.value.status_code == 404
    assert await driver_packs.get_pack(pack_id, session=object(), packs=mock_packs_catalog) is pack_out
    with pytest.raises(HTTPException) as exc:
        await driver_packs.hosts(pack_id, session=object(), packs=mock_packs_catalog)
    assert exc.value.status_code == 404
    assert (await driver_packs.hosts(pack_id, session=object(), packs=mock_packs_catalog)).hosts == []

    with pytest.raises(HTTPException) as exc:
        await driver_packs.update_pack(
            pack_id,
            driver_packs.PackPatch(state="not-a-state"),
            _username="admin",
            session=object(),
            packs=SimpleNamespace(lifecycle=SimpleNamespace()),
        )
    assert exc.value.status_code == 400

    for error, status_code in ((LookupError("missing"), 404), (ValueError("bad transition"), 400)):
        mock_packs_lc_err = SimpleNamespace(
            lifecycle=SimpleNamespace(transition_pack_state=AsyncMock(side_effect=error))
        )
        with pytest.raises(HTTPException) as exc:
            await driver_packs.update_pack(
                pack_id,
                driver_packs.PackPatch(state="enabled"),
                _username="admin",
                session=object(),
                packs=mock_packs_lc_err,
            )
        assert exc.value.status_code == status_code

    mock_packs_policy_err = SimpleNamespace(
        catalog=SimpleNamespace(set_runtime_policy=AsyncMock(side_effect=LookupError("missing")))
    )
    with pytest.raises(HTTPException) as exc:
        await driver_packs.update_runtime_policy(
            pack_id,
            driver_packs.RuntimePolicyPatch(runtime_policy=RuntimePolicy()),
            _username="admin",
            session=object(),
            packs=mock_packs_policy_err,
        )
    assert exc.value.status_code == 404

    dummy_session = DummySession()
    mock_packs_del_err = SimpleNamespace(
        catalog=SimpleNamespace(delete_pack=AsyncMock(side_effect=RuntimeError("in use")))
    )
    with pytest.raises(HTTPException) as exc:
        await driver_packs.delete_driver_pack(
            pack_id, _username="admin", session=dummy_session, packs=mock_packs_del_err
        )
    assert exc.value.status_code == 409

    mock_packs_del_ok = SimpleNamespace(catalog=SimpleNamespace(delete_pack=AsyncMock(return_value=None)))
    response = await driver_packs.delete_driver_pack(
        pack_id, _username="admin", session=dummy_session, packs=mock_packs_del_ok
    )
    assert response.status_code == 204
    assert dummy_session.committed is True


async def test_webhook_router_error_and_delivery_paths() -> None:
    webhook_id = uuid.uuid4()
    delivery_id = uuid.uuid4()
    webhook = SimpleNamespace(id=webhook_id, name="alerts")
    delivery = SimpleNamespace(
        id=delivery_id,
        webhook_id=webhook_id,
        event_type="webhook.test",
        status="pending",
        attempts=0,
        max_attempts=3,
        last_attempt_at=None,
        next_retry_at=None,
        last_error=None,
        last_http_status=None,
        created_at=datetime(2026, 5, 1, tzinfo=UTC),
        updated_at=datetime(2026, 5, 1, tzinfo=UTC),
    )

    mock_event_services_wh = SimpleNamespace(publisher=AsyncMock())
    none_crud = SimpleNamespace(
        get_webhook=AsyncMock(return_value=None),
        update_webhook=AsyncMock(return_value=None),
        delete_webhook=AsyncMock(return_value=False),
    )
    none_dispatch = SimpleNamespace(
        list_deliveries=AsyncMock(return_value=([], 0)),
        retry_delivery=AsyncMock(return_value=None),
    )
    none_wh_svc = SimpleNamespace(crud=none_crud, dispatch=none_dispatch)
    for call in (
        lambda: webhooks.get_webhook(webhook_id, db=object(), webhook_services=none_wh_svc),  # type: ignore[arg-type]
        lambda: webhooks.update_webhook(  # type: ignore[arg-type]
            webhook_id, data=webhooks.WebhookUpdate(enabled=False), db=object(), webhook_services=none_wh_svc
        ),
        lambda: webhooks.delete_webhook(webhook_id, db=object(), webhook_services=none_wh_svc),  # type: ignore[arg-type]
        lambda: webhooks.test_webhook(  # type: ignore[arg-type]
            webhook_id, db=object(), event_services=mock_event_services_wh, webhook_services=none_wh_svc
        ),
        lambda: webhooks.list_webhook_deliveries(webhook_id, db=object(), webhook_services=none_wh_svc),  # type: ignore[arg-type]
        lambda: webhooks.retry_webhook_delivery(  # type: ignore[arg-type]
            webhook_id, delivery_id, db=object(), webhook_services=none_wh_svc
        ),
    ):
        with pytest.raises(HTTPException) as exc:
            await call()
        assert exc.value.status_code == 404

    mock_event_services_wh = SimpleNamespace(publisher=AsyncMock())
    ok_crud = SimpleNamespace(get_webhook=AsyncMock(return_value=webhook))
    ok_dispatch = SimpleNamespace(
        list_deliveries=AsyncMock(return_value=([delivery], 1)),
        retry_delivery=AsyncMock(side_effect=[None, delivery]),
    )
    ok_wh_svc = SimpleNamespace(crud=ok_crud, dispatch=ok_dispatch)
    result = await webhooks.test_webhook(  # type: ignore[arg-type]
        webhook_id, db=object(), event_services=mock_event_services_wh, webhook_services=ok_wh_svc
    )
    assert result["webhook_name"] == "alerts"
    mock_event_services_wh.publisher.publish.assert_awaited_once()
    deliveries = await webhooks.list_webhook_deliveries(  # type: ignore[arg-type]
        webhook_id, db=object(), webhook_services=ok_wh_svc
    )
    assert deliveries["total"] == 1
    with pytest.raises(HTTPException) as exc:
        await webhooks.retry_webhook_delivery(  # type: ignore[arg-type]
            webhook_id, delivery_id, db=object(), webhook_services=ok_wh_svc
        )
    assert exc.value.status_code == 404
    retried = await webhooks.retry_webhook_delivery(  # type: ignore[arg-type]
        webhook_id, delivery_id, db=object(), webhook_services=ok_wh_svc
    )
    assert retried.id == delivery_id


async def test_runs_router_parses_filters_and_maps_service_errors() -> None:
    assert runs._parse_run_filter_datetime("2026-05-01") == datetime(2026, 5, 1, tzinfo=UTC)
    assert runs._parse_run_filter_datetime("2026-05-01", end_of_day=True).time().hour == 23
    assert runs._parse_run_filter_datetime("2026-05-01T12:00:00") == datetime(2026, 5, 1, 12, tzinfo=UTC)

    payload = RunCreate(name="ci", requirements=[{"pack_id": "pack", "platform_id": "android", "count": 1}])

    mock_rs = SimpleNamespace(
        allocator=AsyncMock(),
        lifecycle=AsyncMock(),
        failure=AsyncMock(),
        query=AsyncMock(),
    )

    with pytest.raises(HTTPException) as exc:
        await runs.create_run(
            payload,
            include="capabilities",
            db=object(),
            run_services=mock_rs,
        )
    assert exc.value.status_code == 422

    for error, status_code in (
        (PackUnavailableError("missing"), 422),
        (PackDisabledError("disabled"), 422),
        (ValueError("none"), 409),
    ):
        mock_rs.allocator.create_run = AsyncMock(side_effect=error)
        with pytest.raises(HTTPException) as exc:
            await runs.create_run(
                payload,
                include=None,
                db=object(),
                run_services=mock_rs,
            )
        assert exc.value.status_code == status_code

    run = _run_obj()
    device_info = runs.ReservedDeviceInfo(
        device_id=str(uuid.uuid4()),
        identity_value="serial",
        pack_id="pack",
        platform_id="android",
        os_version="14",
    )
    mock_rs.allocator.create_run = AsyncMock(return_value=(run, [device_info]))
    created = await runs.create_run(
        payload,
        include=None,
        db=object(),
        run_services=mock_rs,
    )
    assert created["id"] == run.id

    request = SimpleNamespace(query_params={})
    mock_rs_list = SimpleNamespace(
        allocator=AsyncMock(),
        lifecycle=AsyncMock(),
        failure=AsyncMock(),
        query=AsyncMock(),
    )
    with pytest.raises(HTTPException) as exc:
        await runs.list_runs(request, created_from="bad-date", created_to=None, db=object(), run_services=mock_rs_list)
    assert exc.value.status_code == 422

    read = _run_read(run)
    mock_rs_list.query.list_runs = AsyncMock(return_value=([run], 1))
    mock_rs_list.query.fetch_session_counts = AsyncMock(return_value={run.id: read.session_counts})
    with patch("app.runs.router.run_service.build_run_read", new=Mock(return_value=read)):
        listed = await runs.list_runs(
            request,
            state=None,
            created_from=None,
            created_to=None,
            limit=50,
            cursor=None,
            direction="older",
            offset=0,
            sort_by="created_at",
            sort_dir="desc",
            db=object(),
            run_services=mock_rs_list,
        )
    assert listed["total"] == 1
    assert listed["items"][0].id == run.id

    cursor_request = SimpleNamespace(query_params={"cursor": "bad"})
    mock_rs_list.query.list_runs_cursor = AsyncMock(side_effect=runs.CursorPaginationError("bad cursor"))
    with pytest.raises(HTTPException) as exc:
        await runs.list_runs(
            cursor_request,
            state=None,
            created_from=None,
            created_to=None,
            limit=50,
            cursor="bad",
            direction="older",
            offset=0,
            sort_by="created_at",
            sort_dir="desc",
            db=object(),
            run_services=mock_rs_list,
        )
    assert exc.value.status_code == 422


async def test_runs_router_state_transition_endpoints() -> None:
    run = _run_obj()
    read = _run_read(run)
    run_id = run.id
    device_id = uuid.uuid4()

    def _mock_rs() -> SimpleNamespace:
        rs = SimpleNamespace(
            allocator=AsyncMock(),
            lifecycle=AsyncMock(),
            failure=AsyncMock(),
            query=AsyncMock(),
        )
        rs.query.fetch_session_counts = AsyncMock(return_value={run.id: read.session_counts})
        return rs

    # signal_ready / signal_active conflict paths
    for lifecycle_method in ("signal_ready", "signal_active"):
        mock_rs = _mock_rs()
        setattr(mock_rs.lifecycle, lifecycle_method, AsyncMock(side_effect=ValueError("bad state")))
        fn = getattr(runs, lifecycle_method)
        with pytest.raises(HTTPException) as exc:
            await fn(run_id, db=object(), run_services=mock_rs)
        assert exc.value.status_code in {404, 409}

    # complete / cancel / force_release conflict paths
    for lifecycle_method in ("complete_run", "cancel_run", "force_release"):
        mock_rs = _mock_rs()
        setattr(mock_rs.lifecycle, lifecycle_method, AsyncMock(side_effect=ValueError("bad state")))
        fn = getattr(runs, lifecycle_method)
        with pytest.raises(HTTPException) as exc:
            await fn(run_id, db=object(), run_services=mock_rs)
        assert exc.value.status_code in {404, 409}

    # report_preparation_failed conflict
    mock_rs = _mock_rs()
    mock_rs.failure.report_preparation_failure = AsyncMock(side_effect=ValueError("bad"))
    with pytest.raises(HTTPException) as exc:
        await runs.report_preparation_failed(
            run_id,
            device_id,
            RunPreparationFailureReport(message="failed"),
            db=object(),
            run_services=mock_rs,
        )
    assert exc.value.status_code == 409

    # cooldown_device not found
    mock_rs = _mock_rs()
    mock_rs.failure.cooldown_device = AsyncMock(side_effect=ValueError("Run not found"))
    with pytest.raises(HTTPException) as exc:
        await runs.cooldown_device_endpoint(
            run_id,
            device_id,
            RunCooldownRequest(reason="flaky", ttl_seconds=30),
            db=object(),
            run_services=mock_rs,
        )
    assert exc.value.status_code == 404

    # cooldown_device success
    mock_rs = _mock_rs()
    mock_rs.failure.cooldown_device = AsyncMock(return_value=(datetime.now(UTC) + timedelta(seconds=30), 1, False, 3))
    cooldown = await runs.cooldown_device_endpoint(
        run_id,
        device_id,
        RunCooldownRequest(reason="flaky", ttl_seconds=30),
        db=object(),
        run_services=mock_rs,
    )
    assert cooldown.status == "cooldown_set"

    # heartbeat
    mock_rs = _mock_rs()
    mock_rs.lifecycle.heartbeat = AsyncMock(return_value=run)
    heartbeat = await runs.heartbeat(run_id, db=object(), run_services=mock_rs)
    assert heartbeat["state"] == run.state

    # signal_ready success path
    mock_rs = _mock_rs()
    mock_rs.lifecycle.signal_ready = AsyncMock(return_value=run)
    with patch("app.runs.router.run_service.build_run_read", new=Mock(return_value=read)):
        assert (await runs.signal_ready(run_id, db=object(), run_services=mock_rs)).id == run_id

    # complete_run success path
    mock_rs = _mock_rs()
    mock_rs.lifecycle.complete_run = AsyncMock(return_value=run)
    with patch("app.runs.router.run_service.build_run_read", new=Mock(return_value=read)):
        assert (await runs.complete_run(run_id, db=object(), run_services=mock_rs)).id == run_id


async def test_devices_core_router_branches() -> None:
    request = SimpleNamespace(
        query_params=SimpleNamespace(multi_items=Mock(return_value=[("tags.lab", "east"), ("tags.", "bad")]))
    )
    filters = devices_core.build_device_query_filters(
        request,
        pack_id=None,
        platform_id="android_mobile",
        status=None,
        host_id=None,
        identity_value=None,
        connection_target=None,
        device_type=None,
        connection_type=None,
        os_version=None,
        os_version_display=None,
        search=None,
        hardware_health_status=None,
        hardware_telemetry_state=None,
        needs_attention=None,
        sort_by="created_at",
        sort_dir="desc",
    )
    assert filters.tags == {"lab": "east"}

    device_id = uuid.uuid4()
    device = SimpleNamespace(id=device_id, pack_id="pack", platform_id="android", connection_target="serial")
    serialized = {"id": str(device_id)}
    with (
        patch("app.devices.routers.core.run_service.get_device_reservation_map", new=AsyncMock(return_value={})),
        patch("app.devices.routers.core.device_health.build_public_summary", new=Mock(return_value={"healthy": True})),
        patch(
            "app.devices.routers.core.platform_label_service.load_platform_label_map",
            new=AsyncMock(return_value={("pack", "android"): "Android"}),
        ),
        patch("app.devices.routers.core.run_service.get_reservation_context_for_device", new=Mock(return_value=None)),
    ):
        mock_ds_list = SimpleNamespace(
            crud=SimpleNamespace(list_devices_paginated=AsyncMock(return_value=([device], 1))),
            presenter=SimpleNamespace(serialize_device=AsyncMock(return_value=serialized)),
        )
        listed = await devices_core.list_devices(
            filters=filters,
            limit=10,
            offset=None,
            db=object(),
            device_services=mock_ds_list,
        )
    assert listed == {"items": [serialized], "total": 1, "limit": 10, "offset": 0}

    mock_ds_update_none = SimpleNamespace(
        crud=SimpleNamespace(update_device=AsyncMock(return_value=None)),
        presenter=SimpleNamespace(),
    )
    with pytest.raises(HTTPException) as exc:
        await devices_core.update_device(
            device_id,
            data=devices_core.DevicePatch(),
            db=object(),
            device_services=mock_ds_update_none,
        )
    assert exc.value.status_code == 404

    mock_ds_delete = SimpleNamespace(crud=SimpleNamespace(delete_device=AsyncMock(return_value=False)))
    with pytest.raises(HTTPException) as exc:
        await devices_core.delete_device(device_id, db=object(), device_services=mock_ds_delete)
    assert exc.value.status_code == 404

    mock_packs_cur_err = SimpleNamespace(
        release=SimpleNamespace(set_current_release=AsyncMock(side_effect=LookupError("missing")))
    )
    with pytest.raises(HTTPException) as exc:
        await driver_pack_uploads.update_current_release(
            "pack",
            CurrentReleasePatch(release="1.0.0"),
            _username="admin",
            session=DummySession(),
            packs=mock_packs_cur_err,
        )
    assert exc.value.status_code == 404

    mock_packs_export_ok = SimpleNamespace(release=SimpleNamespace(export=AsyncMock(return_value=(b"data", "sha"))))
    response = await driver_pack_export.export_release(
        "local/pack",
        "1.0.0+meta",
        _username="admin",
        session=object(),
        packs=mock_packs_export_ok,
    )
    assert response.headers["X-Pack-Sha256"] == "sha"
    assert "local_pack-1.0.0_meta.tar.gz" in response.headers["Content-Disposition"]

    mock_packs_export_err = SimpleNamespace(
        release=SimpleNamespace(export=AsyncMock(side_effect=LookupError("missing")))
    )
    with pytest.raises(HTTPException) as exc:
        await driver_pack_export.export_release(
            "pack", "1.0.0", _username="admin", session=object(), packs=mock_packs_export_err
        )
    assert exc.value.status_code == 404


async def test_devices_verification_router_error_and_success_branches(db_session: AsyncSession) -> None:
    create_payload = DeviceVerificationCreate(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value="router-verify",
        connection_target="router-verify",
        name="Router Verify",
        host_id=uuid.uuid4(),
    )
    mock_verification_services_pack_error = SimpleNamespace(
        service=SimpleNamespace(start_verification_job=AsyncMock(side_effect=PackUnavailableError("missing")))
    )
    with pytest.raises(HTTPException) as exc:
        await devices_verification_router.create_device_verification_job(
            create_payload, db=db_session, verification_services=mock_verification_services_pack_error
        )
    assert exc.value.status_code == 422

    device_id = uuid.uuid4()
    mock_device_services_none = SimpleNamespace(
        crud=SimpleNamespace(get_device=AsyncMock(return_value=None)),
    )
    with pytest.raises(HTTPException) as exc:
        await devices_verification_router.create_existing_device_verification_job(
            device_id,
            DeviceVerificationUpdate(name="verify", host_id=uuid.uuid4()),
            db=db_session,
            device_services=mock_device_services_none,
            verification_services=SimpleNamespace(service=SimpleNamespace()),
        )
    assert exc.value.status_code == 404


async def test_devices_verification_event_stream_terminal_initial_event(db_session: AsyncSession) -> None:
    job = {"job_id": "job-stream", "status": "completed", "current_stage": "save_device"}
    request = SimpleNamespace(is_disconnected=AsyncMock(return_value=False))
    unsubscribe = Mock()
    mock_event_services = SimpleNamespace(
        subscriber=SimpleNamespace(subscribe=Mock(return_value=object()), unsubscribe=unsubscribe)
    )
    mock_vs_terminal = SimpleNamespace(service=SimpleNamespace(get_verification_job=AsyncMock(return_value=job)))
    response = await devices_verification_router.stream_device_verification_job_events(
        "job-stream",
        request,  # type: ignore[arg-type]
        db=db_session,
        event_services=mock_event_services,
        verification_services=mock_vs_terminal,
    )
    first = await response.body_iterator.__anext__()
    await response.body_iterator.aclose()

    assert first["event"] == "device.verification.updated"
    unsubscribe.assert_called_once()

    device_id = uuid.uuid4()
    mock_vs_existing = SimpleNamespace(
        service=SimpleNamespace(
            start_existing_device_verification_job=AsyncMock(return_value={"id": "job", "status": "queued"})
        )
    )
    mock_ds_existing_with_crud = SimpleNamespace(
        crud=SimpleNamespace(get_device=AsyncMock(return_value=object())),
    )
    assert (
        await devices_verification_router.create_existing_device_verification_job(
            device_id,
            DeviceVerificationUpdate(name="verify", host_id=uuid.uuid4()),
            db=db_session,
            device_services=mock_ds_existing_with_crud,
            verification_services=mock_vs_existing,
        )
    )["id"] == "job"

    mock_vs_no_job = SimpleNamespace(service=SimpleNamespace(get_verification_job=AsyncMock(return_value=None)))
    with pytest.raises(HTTPException) as exc:
        await devices_verification_router.get_device_verification_job(
            "missing", db=db_session, verification_services=mock_vs_no_job
        )
    assert exc.value.status_code == 404


async def test_devices_control_health_and_reconnect_error_branches() -> None:
    device_id = uuid.uuid4()
    host = SimpleNamespace(ip="10.0.0.10", agent_port=5100)
    node = SimpleNamespace(
        port=4723,
        observed_running=True,
        health_running=True,
        health_state="running",
    )
    device = SimpleNamespace(
        id=device_id,
        platform_id="android_mobile",
        pack_id="appium-uiautomator2",
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
        ip_address=None,
        connection_target="router-health",
        appium_node=node,
        host=host,
    )
    _viability_failed = AsyncMock(return_value={"status": "failed"})
    _session_svc_failed = SimpleNamespace(viability=SimpleNamespace(get_session_viability=_viability_failed))
    with (
        patch.object(devices_control, "get_device_or_404", new=AsyncMock(return_value=device)),
        patch.object(devices_control, "require_management_host", new=Mock(return_value=host)),
        patch.object(devices_control, "fetch_appium_status", new=AsyncMock(side_effect=AgentCallError("h", "down"))),
        patch.object(
            devices_control, "fetch_pack_device_health", new=AsyncMock(side_effect=AgentCallError("h", "down"))
        ),
        patch.object(
            devices_control.lifecycle_policy_summary, "build_lifecycle_policy", new=AsyncMock(return_value={})
        ),
    ):
        health = await devices_control.device_health(
            device_id,
            db=object(),
            device_services=SimpleNamespace(crud=AsyncMock()),
            settings_services=_mock_settings_svc(FakeSettingsReader({})),
            agent_comm=SimpleNamespace(circuit_breaker=Mock(), http_pool=None),
            session_services=_session_svc_failed,
        )
    assert health["node"]["state"] == "error"
    assert health["device_checks"]["detail"] == "Agent unreachable: down"
    assert health["healthy"] is False

    reconnect_device = SimpleNamespace(
        id=device_id,
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.network,
        ip_address="10.0.0.20",
        host=host,
        host_id=None,
        connection_target="10.0.0.20:5555",
        identity_value="stable",
        appium_node=SimpleNamespace(observed_running=False),
    )
    reconnect_db = SimpleNamespace(commit=AsyncMock(), flush=AsyncMock())
    _health_ss = _mock_settings_svc(FakeSettingsReader({}))
    # Phase 2: inner HTTPException(400) propagates unchanged (was incorrectly wrapped as 502)
    with (
        patch.object(devices_control, "get_device_or_404", new=AsyncMock(return_value=reconnect_device)),
        patch.object(
            devices_control, "resolve_pack_platform", new=AsyncMock(return_value=SimpleNamespace(lifecycle_actions=[]))
        ),
        patch.object(devices_control, "platform_has_lifecycle_action", new=Mock(return_value=True)),
        patch(
            "app.devices.services.link_repair.pack_device_lifecycle_action",
            new=AsyncMock(return_value={"success": True}),
        ),
        patch.object(IntentService, "revoke_intents_and_reconcile", new=AsyncMock()),
    ):
        with pytest.raises(HTTPException) as exc:
            await devices_control.reconnect_device(
                device_id,
                db=reconnect_db,
                device_services=SimpleNamespace(crud=AsyncMock(), publisher=event_bus),
                settings_services=_health_ss,
                agent_comm=SimpleNamespace(circuit_breaker=Mock(), http_pool=None),
                appium_services=SimpleNamespace(reconciler_agent=AsyncMock()),
            )  # type: ignore[arg-type]
    assert exc.value.status_code == 400

    with (
        patch.object(devices_control, "get_device_for_update_or_404", new=AsyncMock(return_value=device)),
        patch.object(
            devices_control, "resolve_pack_platform", new=AsyncMock(return_value=SimpleNamespace(lifecycle_actions=[]))
        ),
        patch.object(devices_control, "platform_has_lifecycle_action", new=Mock(return_value=False)),
    ):
        with pytest.raises(HTTPException) as exc:
            await devices_control.device_lifecycle_action(
                device_id,
                "reboot",
                db=object(),
                settings_services=_health_ss,
                agent_comm=SimpleNamespace(circuit_breaker=Mock(), http_pool=None),
                device_services=AsyncMock(),
            )
    assert exc.value.status_code == 400


async def test_runs_router_cursor_detail_and_cooldown_error_branches() -> None:
    request = SimpleNamespace(query_params={"cursor": "bad"})
    mock_rs = SimpleNamespace(
        allocator=AsyncMock(),
        lifecycle=AsyncMock(),
        failure=AsyncMock(),
        query=AsyncMock(),
    )
    mock_rs.query.list_runs_cursor = AsyncMock(side_effect=CursorPaginationError("bad"))
    with pytest.raises(HTTPException) as exc:
        await runs.list_runs(
            request,
            state=None,
            created_from=None,
            created_to=None,
            limit=50,
            cursor="bad",
            direction="older",
            offset=0,
            sort_by="created_at",
            sort_dir="desc",
            db=object(),
            run_services=mock_rs,
        )
    assert exc.value.status_code == 422

    run_id = uuid.uuid4()
    run = SimpleNamespace(
        id=run_id,
        name="Run",
        state=RunState.active,
        reserved_devices=[],
        ttl_minutes=30,
        heartbeat_timeout_sec=60,
        created_at=datetime.now(UTC),
        started_at=None,
        completed_at=None,
        created_by="operator",
    )
    read = RunRead(
        id=run_id,
        name="Run",
        state=RunState.active,
        requirements=[],
        ttl_minutes=30,
        heartbeat_timeout_sec=60,
        session_counts=SessionCounts(total=0, running=0, passed=0, failed=0, error=0),
        created_at=run.created_at,
    )
    mock_rs.query.fetch_session_counts = AsyncMock(return_value={})
    with (
        patch.object(runs.run_service, "get_run", new=AsyncMock(return_value=run)),
        patch.object(runs.run_service, "build_run_read", new=Mock(return_value=read)),
    ):
        detail = await runs.get_run(run_id, db=object(), run_services=mock_rs)
    assert detail["id"] == run_id
    assert detail["devices"] == []

    for message, status_code in (
        ("run not found", 404),
        ("ttl_seconds must be <= 60", 422),
        ("not active", 409),
    ):
        mock_rs.failure.cooldown_device = AsyncMock(side_effect=ValueError(message))
        with pytest.raises(HTTPException) as exc:
            await runs.cooldown_device_endpoint(
                run_id,
                uuid.uuid4(),
                RunCooldownRequest(reason="bad", ttl_seconds=10),
                db=object(),
                run_services=mock_rs,
            )
        assert exc.value.status_code == status_code
