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

from app.errors import AgentCallError, PackDisabledError, PackUnavailableError
from app.models.appium_node import AppiumDesiredState
from app.models.device import ConnectionType, DeviceHold, DeviceType
from app.models.test_run import RunState
from app.routers import (
    admin_appium_nodes,
    agent_driver_packs,
    analytics,
    bulk,
    device_groups,
    device_route_helpers,
    devices_control,
    devices_core,
    devices_test_data,
    driver_pack_authoring,
    driver_pack_export,
    driver_pack_templates,
    driver_pack_uploads,
    driver_packs,
    events,
    grid,
    host_terminal,
    hosts,
    lifecycle,
    runs,
    sessions,
    webhooks,
)
from app.routers import devices_verification as devices_verification_router
from app.routers import nodes as nodes_router
from app.routers import plugins as plugins_router
from app.routers import (
    settings as settings_router,
)
from app.schemas.analytics import DeviceReliabilityRow, DeviceUtilizationRow, GroupByOption
from app.schemas.device import BulkMaintenanceEnter, BulkTagsUpdate, DeviceVerificationCreate, DeviceVerificationUpdate
from app.schemas.driver_pack import CurrentReleasePatch, RuntimePolicy
from app.schemas.plugin import PluginCreate, PluginUpdate
from app.schemas.run import (
    ReservedDeviceInfo,
    RunCooldownRequest,
    RunCreate,
    RunPreparationFailureReport,
    RunRead,
    SessionCounts,
)
from app.schemas.setting import SettingsBulkUpdate, SettingUpdate
from app.services.cursor_pagination import CursorPage, CursorPaginationError
from app.services.device_identity_conflicts import DeviceIdentityConflictError

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


async def test_settings_router_error_paths() -> None:
    with patch.object(settings_router.settings_service, "bulk_update", new=AsyncMock(side_effect=KeyError("missing"))):
        with pytest.raises(HTTPException) as caught:
            await settings_router.bulk_update_settings(SettingsBulkUpdate(settings={"missing": 1}), db=object())
    assert caught.value.status_code == 404

    with patch.object(settings_router.settings_service, "bulk_update", new=AsyncMock(side_effect=ValueError("bad"))):
        with pytest.raises(HTTPException) as caught:
            await settings_router.bulk_update_settings(SettingsBulkUpdate(settings={"bad": 1}), db=object())
    assert caught.value.status_code == 400

    with patch.object(settings_router.settings_service, "get_setting_response", side_effect=KeyError("missing")):
        with pytest.raises(HTTPException) as caught:
            await settings_router.get_setting("missing")
    assert caught.value.status_code == 404

    with patch.object(settings_router.settings_service, "update", new=AsyncMock(side_effect=KeyError("missing"))):
        with pytest.raises(HTTPException) as caught:
            await settings_router.update_setting("missing", SettingUpdate(value=1), db=object())
    assert caught.value.status_code == 404

    with patch.object(settings_router.settings_service, "update", new=AsyncMock(side_effect=ValueError("bad"))):
        with pytest.raises(HTTPException) as caught:
            await settings_router.update_setting("bad", SettingUpdate(value=1), db=object())
    assert caught.value.status_code == 400

    with patch.object(settings_router.settings_service, "reset", new=AsyncMock(side_effect=KeyError("missing"))):
        with pytest.raises(HTTPException) as caught:
            await settings_router.reset_setting("missing", db=object())
    assert caught.value.status_code == 404


async def test_agent_driver_pack_router_delegates_and_commits() -> None:
    host_id = uuid.uuid4()
    db = DummySession()
    desired_payload = {"host_id": str(host_id), "desired": []}

    with patch.object(agent_driver_packs, "compute_desired", new=AsyncMock(return_value=desired_payload)) as compute:
        response = await agent_driver_packs.desired(host_id=host_id, db=db)

    assert response == desired_payload
    compute.assert_awaited_once_with(db, host_id)

    status_payload: dict[str, object] = {"host_id": str(host_id), "packs": []}
    with patch.object(agent_driver_packs, "apply_status", new=AsyncMock()) as apply_status:
        response = await agent_driver_packs.status(payload=status_payload, db=db)

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
    with patch.object(analytics, "get_fleet_capacity_timeline", new=AsyncMock(return_value=capacity)) as timeline:
        response = await analytics.fleet_capacity_timeline(date_from=None, date_to=None, db=db)

    assert response is capacity
    assert timeline.await_args.args == (db,)
    assert timeline.await_args.kwargs["date_from"] is not None
    assert timeline.await_args.kwargs["date_to"] is not None


async def test_lifecycle_incidents_router_returns_paginated_response() -> None:
    db = object()
    with patch.object(
        lifecycle.lifecycle_incident_service,
        "list_lifecycle_incidents_paginated",
        new=AsyncMock(return_value=([], "next", "prev")),
    ) as list_incidents:
        response = await lifecycle.get_lifecycle_incidents(
            limit=5, device_id=None, cursor=None, direction="newer", db=db
        )

    assert response.items == []
    assert response.limit == 5
    assert response.next_cursor == "next"
    assert response.prev_cursor == "prev"
    list_incidents.assert_awaited_once_with(db, limit=5, device_id=None, cursor=None, direction="newer")


async def test_more_router_success_and_not_found_branches(monkeypatch: pytest.MonkeyPatch) -> None:
    group_id = uuid.uuid4()
    device_id = uuid.uuid4()

    with patch.object(device_groups.device_group_service, "update_group", new=AsyncMock(return_value=None)):
        with pytest.raises(HTTPException) as exc:
            await device_groups.update_group(group_id, device_groups.DeviceGroupUpdate(name="new"), db=object())
    assert exc.value.status_code == 404
    updated_group = SimpleNamespace(id=group_id)
    with (
        patch.object(device_groups.device_group_service, "update_group", new=AsyncMock(return_value=updated_group)),
        patch.object(device_groups.device_group_service, "get_group", new=AsyncMock(return_value={"id": group_id})),
    ):
        assert await device_groups.update_group(group_id, device_groups.DeviceGroupUpdate(name="new"), db=object()) == {
            "id": group_id
        }

    with patch.object(device_groups.device_group_service, "get_group", new=AsyncMock(return_value=None)):
        with pytest.raises(HTTPException) as exc:
            await device_groups.add_members(
                group_id,
                device_groups.GroupMembershipUpdate(device_ids=[device_id]),
                db=object(),
            )
        assert exc.value.status_code == 404
        with pytest.raises(HTTPException) as exc:
            await device_groups.remove_members(
                group_id,
                device_groups.GroupMembershipUpdate(device_ids=[device_id]),
                db=object(),
            )
        assert exc.value.status_code == 404

    with patch.object(devices_core.device_service, "update_device", new=AsyncMock(return_value=object())):
        with patch.object(devices_core.device_presenter, "serialize_device", new=AsyncMock(return_value={"id": "ok"})):
            assert await devices_core.update_device(device_id, devices_core.DevicePatch(name="new"), db=object()) == {
                "id": "ok"
            }

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
    with (
        patch.object(runs.run_service, "create_run", new=AsyncMock(return_value=(run, [info]))),
        patch.object(runs.run_service, "hydrate_reserved_device_infos", new=AsyncMock()) as hydrate,
        patch.object(runs.settings_service, "get", new=Mock(return_value="http://grid:4444")),
    ):
        created = await runs.create_run(
            RunCreate(name="ci", requirements=[{"pack_id": "pack", "platform_id": "android"}]),
            include="config",
            db=db,
        )
    assert created.id == run.id
    hydrate.assert_awaited_once()

    with patch.object(driver_pack_export, "PackStorageService") as storage_cls:
        assert driver_pack_export.get_pack_storage() is storage_cls.return_value

    pack_id = "appium-demo"
    pack = SimpleNamespace(id=pack_id)
    with patch("app.routers.driver_packs.transition_pack_state", new=AsyncMock(return_value=pack)):
        with patch("app.routers.driver_packs.build_pack_out", new=Mock(return_value={"id": pack_id})):
            assert (
                await driver_packs.update_pack(
                    pack_id,
                    driver_packs.PackPatch(state="enabled"),
                    _username="admin",
                    session=object(),
                )
            ) == {"id": pack_id}

    with patch("app.routers.driver_packs.set_runtime_policy", new=AsyncMock(return_value=pack)):
        with patch("app.routers.driver_packs.build_pack_out", new=Mock(return_value={"id": pack_id})):
            assert (
                await driver_packs.update_runtime_policy(
                    pack_id,
                    driver_packs.RuntimePolicyPatch(runtime_policy=RuntimePolicy()),
                    _username="admin",
                    session=object(),
                )
            ) == {"id": pack_id}

    with patch("app.routers.driver_packs.delete_pack", new=AsyncMock(side_effect=LookupError("missing"))):
        with pytest.raises(HTTPException) as exc:
            await driver_packs.delete_driver_pack(pack_id, _username="admin", session=DummySession())
    assert exc.value.status_code == 404

    plugin_id = uuid.uuid4()
    plugin = SimpleNamespace(id=plugin_id)
    with patch.object(plugins_router.plugin_service, "update_plugin", new=AsyncMock(return_value=plugin)):
        assert await plugins_router.update_plugin(plugin_id, PluginUpdate(enabled=False), db=object()) is plugin
    with patch.object(
        plugins_router.plugin_service, "sync_all_host_plugins", new=AsyncMock(return_value={"synced": 1})
    ):
        assert await plugins_router.sync_all_plugins(db=object()) == {"synced": 1}

    with patch.object(settings_router.settings_service, "reset_all", new=AsyncMock()) as reset_all:
        assert await settings_router.reset_all_settings(db=object()) == {"status": "all settings reset to defaults"}
    reset_all.assert_awaited_once()

    webhook_id = uuid.uuid4()
    webhook = SimpleNamespace(id=webhook_id)
    with patch.object(webhooks.webhook_service, "get_webhook", new=AsyncMock(return_value=webhook)):
        assert await webhooks.get_webhook(webhook_id, db=object()) is webhook
    with patch.object(webhooks.webhook_service, "update_webhook", new=AsyncMock(return_value=webhook)):
        assert await webhooks.update_webhook(webhook_id, webhooks.WebhookUpdate(enabled=True), db=object()) is webhook

    device = object()
    with patch.object(device_route_helpers.device_service, "get_device", new=AsyncMock(return_value=None)):
        with pytest.raises(HTTPException) as exc:
            await device_route_helpers.get_device_or_404(device_id, object())
    assert exc.value.status_code == 404
    with patch.object(device_route_helpers.device_service, "get_device", new=AsyncMock(return_value=device)):
        assert await device_route_helpers.get_device_or_404(device_id, object()) is device

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
    event_response = await events.event_stream(
        SimpleNamespace(is_disconnected=AsyncMock(return_value=False)),
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

    monkeypatch.setattr(
        devices_verification_router.device_verification,
        "get_verification_job",
        AsyncMock(return_value=None),
    )
    with pytest.raises(HTTPException) as exc:
        await devices_verification_router.stream_device_verification_job_events(
            "missing", request, db=SimpleNamespace(bind=None)
        )
    assert exc.value.status_code == 404

    monkeypatch.setattr(
        devices_verification_router.device_verification,
        "get_verification_job",
        AsyncMock(return_value=initial_job),
    )
    monkeypatch.setattr(devices_verification_router.event_bus, "subscribe", Mock(return_value=queue))
    monkeypatch.setattr(devices_verification_router.event_bus, "unsubscribe", Mock())

    response = await devices_verification_router.stream_device_verification_job_events(
        "job-stream",
        request,
        db=SimpleNamespace(bind=None),
    )
    assert (await response.body_iterator.__anext__())["event"] == "device.verification.updated"
    with pytest.raises(StopAsyncIteration):
        await response.body_iterator.__anext__()

    empty_queue = SimpleNamespace(get=lambda: object())

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
    with patch.object(plugins_router.plugin_service, "update_plugin", new=AsyncMock(return_value=None)):
        with pytest.raises(HTTPException) as caught:
            await plugins_router.update_plugin(plugin_id, PluginUpdate(enabled=True), db=object())
    assert caught.value.status_code == 404

    with patch.object(plugins_router.plugin_service, "delete_plugin", new=AsyncMock(return_value=False)):
        with pytest.raises(HTTPException) as caught:
            await plugins_router.delete_plugin(plugin_id, db=object())
    assert caught.value.status_code == 404

    with patch.object(plugins_router.host_service, "get_host", new=AsyncMock(return_value=None)):
        with pytest.raises(HTTPException) as caught:
            await plugins_router.host_plugins(uuid.uuid4(), db=object())
    assert caught.value.status_code == 404

    with patch.object(plugins_router.host_service, "get_host", new=AsyncMock(return_value=None)):
        with pytest.raises(HTTPException) as caught:
            await plugins_router.sync_host_plugins(uuid.uuid4(), db=object())
    assert caught.value.status_code == 404


async def test_device_verification_router_error_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        devices_verification_router.device_verification,
        "start_verification_job",
        AsyncMock(side_effect=PackUnavailableError("pack")),
    )
    db = SimpleNamespace(bind=None)
    with pytest.raises(HTTPException) as caught:
        await devices_verification_router.create_device_verification_job(Mock(), db=db)
    assert caught.value.status_code == 422

    monkeypatch.setattr(devices_verification_router.device_service, "get_device", AsyncMock(return_value=None))
    with pytest.raises(HTTPException) as caught:
        await devices_verification_router.create_existing_device_verification_job(uuid.uuid4(), Mock(), db=db)
    assert caught.value.status_code == 404

    monkeypatch.setattr(
        devices_verification_router.device_verification,
        "get_verification_job",
        AsyncMock(return_value=None),
    )
    with pytest.raises(HTTPException) as caught:
        await devices_verification_router.get_device_verification_job("missing", db=db)
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
    with (
        patch.object(hosts, "async_session", return_value=SessionCtx()),
        patch.object(hosts.host_service, "get_host", new=AsyncMock(return_value=host)),
        patch.object(hosts.pack_discovery_service, "discover_devices", new=AsyncMock(return_value=discovery_result)),
        patch.object(hosts.event_bus, "publish", new=AsyncMock()) as publish,
    ):
        await hosts._auto_discover(host_id)
    publish.assert_awaited_once()

    with (
        patch.object(hosts, "async_session", return_value=SessionCtx()),
        patch.object(hosts.host_service, "get_host", new=AsyncMock(return_value=host)),
        patch.object(hosts.plugin_service, "list_plugins", new=AsyncMock(return_value=[object()])),
        patch.object(hosts.plugin_service, "auto_sync_host_plugins", new=AsyncMock()) as sync,
    ):
        await hosts._auto_prepare_host_diagnostics(host_id)
    sync.assert_awaited_once()

    with (
        patch.object(hosts, "async_session", return_value=SessionCtx()),
        patch.object(hosts.host_service, "get_host", new=AsyncMock(return_value=None)),
    ):
        await hosts._auto_discover(host_id)
        await hosts._auto_prepare_host_diagnostics(host_id)

    with (
        patch.object(hosts, "async_session", return_value=SessionCtx()),
        patch.object(hosts.host_service, "get_host", new=AsyncMock(side_effect=RuntimeError("db"))),
        patch.object(hosts.logger, "exception", new=Mock()) as log_exception,
    ):
        await hosts._auto_discover(host_id)
        await hosts._auto_prepare_host_diagnostics(host_id)
    assert log_exception.call_count == 2

    with pytest.raises(HTTPException) as caught:
        await hosts.host_driver_packs(host_id, db=DummySession(get_result=None))
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
    with (
        patch.object(runs.run_service, "create_run", new=AsyncMock(return_value=(run, [info]))),
        patch.object(runs.run_service, "mark_reserved_device_info_includes_unavailable") as mark,
        patch.object(runs.run_service, "hydrate_reserved_device_infos", new=AsyncMock()) as hydrate,
        patch.object(runs.settings_service, "get", return_value="http://grid"),
        patch.object(runs, "select") as select_mock,
    ):
        select_mock.return_value.where.return_value = object()
        db = DummySession(execute_result=SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [])))
        response = await runs.create_run(RunCreate(name="r", requirements=[]), include="config", db=db)
    assert response.id == run.id
    mark.assert_called_once()
    hydrate.assert_awaited_once()

    with patch.object(runs.run_service, "get_run", new=AsyncMock(return_value=None)):
        with pytest.raises(HTTPException) as caught:
            await runs.get_run(uuid.uuid4(), db=object())
    assert caught.value.status_code == 404

    with patch.object(runs.run_service, "cooldown_device", new=AsyncMock(side_effect=ValueError("Run not found"))):
        with pytest.raises(HTTPException) as caught:
            await runs.cooldown_device_endpoint(
                uuid.uuid4(), uuid.uuid4(), RunCooldownRequest(reason="bad", ttl_seconds=1), db=object()
            )
    assert caught.value.status_code == 404

    with patch.object(
        runs.run_service,
        "cooldown_device",
        new=AsyncMock(side_effect=ValueError("ttl_seconds must be <= 30")),
    ):
        with pytest.raises(HTTPException) as caught:
            await runs.cooldown_device_endpoint(
                uuid.uuid4(), uuid.uuid4(), RunCooldownRequest(reason="bad", ttl_seconds=1), db=object()
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
        patch("app.routers.admin_appium_nodes.device_locking.lock_device", new=AsyncMock(return_value=object())),
        patch(
            "app.routers.admin_appium_nodes.appium_node_locking.lock_appium_node_for_device",
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
        patch("app.routers.admin_appium_nodes.device_locking.lock_device", new=AsyncMock(return_value=object())),
        patch(
            "app.routers.admin_appium_nodes.appium_node_locking.lock_appium_node_for_device",
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
        patch("app.routers.admin_appium_nodes.device_locking.lock_device", new=AsyncMock(return_value=object())),
        patch(
            "app.routers.admin_appium_nodes.appium_node_locking.lock_appium_node_for_device",
            new=AsyncMock(return_value=locked),
        ),
        patch("app.routers.admin_appium_nodes.record_event", new=AsyncMock()) as record_event,
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
    auto_body = SimpleNamespace(device_ids=device_ids, auto_manage=True)

    for call, service_name, payload in (
        (bulk.bulk_start_nodes, "bulk_start_nodes", body),
        (bulk.bulk_stop_nodes, "bulk_stop_nodes", body),
        (bulk.bulk_restart_nodes, "bulk_restart_nodes", body),
        (bulk.bulk_delete, "bulk_delete", body),
        (bulk.bulk_enter_maintenance, "bulk_enter_maintenance", body),
        (bulk.bulk_exit_maintenance, "bulk_exit_maintenance", body),
        (bulk.bulk_reconnect, "bulk_reconnect", body),
        (bulk.bulk_update_tags, "bulk_update_tags", tags_body),
        (bulk.bulk_set_auto_manage, "bulk_set_auto_manage", auto_body),
    ):
        with patch(f"app.routers.bulk.bulk_service.{service_name}", new=AsyncMock(return_value={"ok": service_name})):
            assert await call(payload, db=object()) == {"ok": service_name}


async def test_devices_test_data_router_paths() -> None:
    device_id = uuid.uuid4()
    device = SimpleNamespace(id=device_id)
    payload = SimpleNamespace(root={"token": "abc"})

    with (
        patch("app.routers.devices_test_data.get_device_or_404", new=AsyncMock(return_value=device)),
        patch("app.routers.devices_test_data.get_device_for_update_or_404", new=AsyncMock(return_value=device)),
        patch(
            "app.routers.devices_test_data.test_data_service.get_device_test_data",
            new=AsyncMock(return_value={"a": 1}),
        ),
        patch(
            "app.routers.devices_test_data.test_data_service.replace_device_test_data",
            new=AsyncMock(return_value={"token": "abc"}),
        ),
        patch(
            "app.routers.devices_test_data.test_data_service.merge_device_test_data",
            new=AsyncMock(return_value={"merged": True}),
        ),
    ):
        assert await devices_test_data.get_test_data(device_id, db=object()) == {"a": 1}
        assert await devices_test_data.replace_test_data(device_id, payload, db=object()) == {"token": "abc"}  # type: ignore[arg-type]
        assert await devices_test_data.merge_test_data(device_id, payload, db=object()) == {"merged": True}  # type: ignore[arg-type]

    audit_log = SimpleNamespace(
        id=uuid.uuid4(),
        previous_test_data={},
        new_test_data={"a": 1},
        changed_by="admin",
        changed_at=datetime(2026, 5, 1, tzinfo=UTC),
    )
    with (
        patch("app.routers.devices_test_data.get_device_or_404", new=AsyncMock(return_value=device)),
        patch(
            "app.routers.devices_test_data.test_data_service.get_test_data_history",
            new=AsyncMock(return_value=[audit_log]),
        ),
    ):
        history = await devices_test_data.get_history(device_id, db=object())
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

    with (
        patch("app.routers.sessions.session_service.list_sessions", new=AsyncMock(return_value=([session_obj], 1))),
        patch("app.routers.sessions._session_details_with_labels", new=AsyncMock(return_value=[detail])),
    ):
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
        )
    assert listed.total == 1
    assert listed.items[0].session_id == "s1"

    cursor_request = SimpleNamespace(query_params={"cursor": "bad"})
    with patch(
        "app.routers.sessions.session_service.list_sessions_cursor",
        new=AsyncMock(side_effect=CursorPaginationError("bad cursor")),
    ):
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
            )
    assert exc.value.status_code == 422

    page = CursorPage(items=[session_obj], limit=50, next_cursor="next", prev_cursor="prev")
    with (
        patch("app.routers.sessions.session_service.list_sessions_cursor", new=AsyncMock(return_value=page)),
        patch("app.routers.sessions._session_details_with_labels", new=AsyncMock(return_value=[detail])),
    ):
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
        )
    assert listed.next_cursor == "next"
    assert listed.prev_cursor == "prev"

    with patch("app.routers.sessions.session_service.get_session", new=AsyncMock(return_value=None)):
        with pytest.raises(HTTPException) as exc:
            await sessions.get_session("missing", db=object())
    assert exc.value.status_code == 404

    with (
        patch("app.routers.sessions.session_service.get_session", new=AsyncMock(return_value=session_obj)),
        patch("app.routers.sessions._session_details_with_labels", new=AsyncMock(return_value=[detail])),
    ):
        assert (await sessions.get_session("s1", db=object()))["session_id"] == "s1"

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
    with patch(
        "app.routers.sessions.session_service.register_session",
        new=AsyncMock(side_effect=ValueError("missing")),
    ):
        with pytest.raises(HTTPException) as exc:
            await sessions.register_session(create_payload, db=object())  # type: ignore[arg-type]
    assert exc.value.status_code == 404
    with patch("app.routers.sessions.session_service.register_session", new=AsyncMock(return_value=session_obj)):
        assert await sessions.register_session(create_payload, db=object()) is session_obj  # type: ignore[arg-type]

    status_payload = SimpleNamespace(status="passed")
    with patch("app.routers.sessions.session_service.update_session_status", new=AsyncMock(return_value=None)):
        with pytest.raises(HTTPException) as exc:
            await sessions.update_session_status("missing", status_payload, db=object())  # type: ignore[arg-type]
    assert exc.value.status_code == 404
    with patch("app.routers.sessions.session_service.update_session_status", new=AsyncMock(return_value=session_obj)):
        assert await sessions.update_session_status("s1", status_payload, db=object()) is session_obj  # type: ignore[arg-type]

    with patch("app.routers.sessions.session_service.mark_session_finished", new=AsyncMock(return_value=None)):
        with pytest.raises(HTTPException) as exc:
            await sessions.post_session_finished("missing", db=object())
    assert exc.value.status_code == 404
    with patch("app.routers.sessions.session_service.mark_session_finished", new=AsyncMock(return_value=session_obj)):
        assert (await sessions.post_session_finished("s1", db=object())).status_code == 204


async def test_plugins_router_maps_service_conflicts_and_missing_resources() -> None:
    plugin_id = uuid.uuid4()
    body = PluginCreate(name="images", version="1.0.0", source="npm:images")

    with patch(
        "app.routers.plugins.plugin_service.create_plugin",
        new=AsyncMock(side_effect=IntegrityError("insert", {}, Exception("dupe"))),
    ):
        with pytest.raises(HTTPException) as exc:
            await plugins_router.create_plugin(body, db=object())
    assert exc.value.status_code == 409

    with patch("app.routers.plugins.plugin_service.update_plugin", new=AsyncMock(return_value=None)):
        with pytest.raises(HTTPException) as exc:
            await plugins_router.update_plugin(plugin_id, PluginUpdate(version="2.0.0"), db=object())
    assert exc.value.status_code == 404


async def test_hosts_router_registration_and_basic_crud_paths() -> None:
    host_id = uuid.uuid4()
    host = SimpleNamespace(id=host_id, hostname="host-1", devices=[])
    response = SimpleNamespace(status_code=200)

    with patch("app.routers.hosts.host_service.register_host", new=AsyncMock(side_effect=IntegrityError("", {}, None))):
        with pytest.raises(HTTPException) as exc:
            await hosts.register_host(object(), response, db=object())  # type: ignore[arg-type]
    assert exc.value.status_code == 409

    with (
        patch("app.routers.hosts.host_service.register_host", new=AsyncMock(return_value=(host, True))),
        patch("app.routers.hosts.settings_service.get", new=Mock(return_value=True)),
        patch("app.routers.hosts._fire_and_forget", new=Mock()) as fire,
        patch("app.routers.hosts._serialize_host", new=Mock(return_value={"id": str(host_id)})),
    ):
        assert await hosts.register_host(object(), response, db=object()) == {"id": str(host_id)}  # type: ignore[arg-type]
    assert response.status_code == 201
    assert fire.call_count == 2

    with patch("app.routers.hosts.host_service.approve_host", new=AsyncMock(return_value=None)):
        with pytest.raises(HTTPException) as exc:
            await hosts.approve_host(host_id, db=object())
    assert exc.value.status_code == 404

    with (
        patch("app.routers.hosts.host_service.approve_host", new=AsyncMock(return_value=host)),
        patch("app.routers.hosts._fire_and_forget", new=Mock()) as fire,
        patch("app.routers.hosts._serialize_host", new=Mock(return_value={"id": str(host_id)})),
    ):
        assert await hosts.approve_host(host_id, db=object()) == {"id": str(host_id)}
    assert fire.call_count == 2

    with patch("app.routers.hosts.host_service.reject_host", new=AsyncMock(return_value=False)):
        with pytest.raises(HTTPException) as exc:
            await hosts.reject_host(host_id, db=object())
    assert exc.value.status_code == 404

    with patch("app.routers.hosts.host_service.reject_host", new=AsyncMock(return_value=True)):
        assert await hosts.reject_host(host_id, db=object()) is None

    with patch("app.routers.hosts.host_service.create_host", new=AsyncMock(side_effect=IntegrityError("", {}, None))):
        with pytest.raises(HTTPException) as exc:
            await hosts.create_host(object(), db=object())  # type: ignore[arg-type]
    assert exc.value.status_code == 409

    with (
        patch("app.routers.hosts.host_service.create_host", new=AsyncMock(return_value=host)),
        patch("app.routers.hosts._serialize_host", new=Mock(return_value={"id": str(host_id)})),
    ):
        assert await hosts.create_host(object(), db=object()) == {"id": str(host_id)}  # type: ignore[arg-type]

    with (
        patch("app.routers.hosts.host_service.list_hosts", new=AsyncMock(return_value=[host])),
        patch("app.routers.hosts._serialize_host", new=Mock(return_value={"id": str(host_id)})),
    ):
        assert await hosts.list_hosts(db=object()) == [{"id": str(host_id)}]

    with patch("app.routers.hosts.settings_service.get", new=Mock(return_value=True)):
        assert await hosts.host_capabilities() == {"web_terminal_enabled": True}


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

    with patch("app.routers.hosts.host_service.get_host", new=AsyncMock(return_value=None)):
        for call in (
            lambda: hosts.get_host(host_id, db=object()),
            lambda: hosts.get_host_tool_status(host_id, db=object()),
            lambda: hosts.discover_devices(host_id, db=object()),
            lambda: hosts.intake_candidates(host_id, db=object()),
            lambda: hosts.confirm_discovery(
                host_id, SimpleNamespace(add_identity_values=[], remove_identity_values=[]), db=object()
            ),
        ):
            with pytest.raises(HTTPException) as exc:
                await call()
            assert exc.value.status_code == 404

    with (
        patch("app.routers.hosts.host_service.get_host", new=AsyncMock(return_value=host)),
        patch("app.routers.hosts._serialize_host", new=Mock(return_value={"id": str(host_id)})),
        patch(
            "app.routers.hosts.platform_label_service.load_platform_label_map",
            new=AsyncMock(return_value={("pack", "android"): "Android"}),
        ),
        patch(
            "app.routers.hosts.device_presenter.serialize_device", new=AsyncMock(return_value={"id": str(device.id)})
        ),
    ):
        detail = await hosts.get_host(host_id, db=object())
    assert detail["devices"] == [{"id": str(device.id)}]

    with patch("app.routers.hosts.host_diagnostics.get_host_diagnostics", new=AsyncMock(return_value=None)):
        with pytest.raises(HTTPException) as exc:
            await hosts.get_host_diagnostics(host_id, db=object())
    assert exc.value.status_code == 404
    with patch("app.routers.hosts.host_diagnostics.get_host_diagnostics", new=AsyncMock(return_value={"ok": True})):
        assert await hosts.get_host_diagnostics(host_id, db=object()) == {"ok": True}

    with (
        patch(
            "app.routers.hosts.host_resource_telemetry.fetch_host_resource_telemetry",
            new=AsyncMock(side_effect=ValueError("bad")),
        ),
        patch("app.routers.hosts.settings_service.get", new=Mock(return_value=60)),
    ):
        with pytest.raises(HTTPException) as exc:
            await hosts.get_host_resource_telemetry(host_id, db=object())
    assert exc.value.status_code == 400
    with (
        patch(
            "app.routers.hosts.host_resource_telemetry.fetch_host_resource_telemetry", new=AsyncMock(return_value=None)
        ),
        patch("app.routers.hosts.settings_service.get", new=Mock(return_value=60)),
    ):
        with pytest.raises(HTTPException) as exc:
            await hosts.get_host_resource_telemetry(host_id, db=object())
    assert exc.value.status_code == 404
    with (
        patch(
            "app.routers.hosts.host_resource_telemetry.fetch_host_resource_telemetry",
            new=AsyncMock(return_value={"samples": []}),
        ),
        patch("app.routers.hosts.settings_service.get", new=Mock(return_value=60)),
    ):
        assert await hosts.get_host_resource_telemetry(host_id, db=object()) == {"samples": []}

    offline = SimpleNamespace(status=SimpleNamespace(value="offline"))
    with patch("app.routers.hosts.host_service.get_host", new=AsyncMock(return_value=offline)):
        with pytest.raises(HTTPException) as exc:
            await hosts.get_host_tool_status(host_id, db=object())
    assert exc.value.status_code == 400
    with (
        patch("app.routers.hosts.host_service.get_host", new=AsyncMock(return_value=host)),
        patch("app.routers.hosts.get_agent_tool_status", new=AsyncMock(return_value={"tools": []})),
    ):
        assert await hosts.get_host_tool_status(host_id, db=object()) == {"tools": []}

    for error, status_code in ((ValueError("busy"), 409), (None, 404)):
        result = AsyncMock(side_effect=error) if error is not None else AsyncMock(return_value=False)
        with patch("app.routers.hosts.host_service.delete_host", new=result):
            with pytest.raises(HTTPException) as exc:
                await hosts.delete_host(host_id, db=object())
        assert exc.value.status_code == status_code
    with patch("app.routers.hosts.host_service.delete_host", new=AsyncMock(return_value=True)):
        assert await hosts.delete_host(host_id, db=object()) is None

    with (
        patch("app.routers.hosts.host_service.get_host", new=AsyncMock(return_value=host)),
        patch("app.routers.hosts.pack_discovery_service.discover_devices", new=AsyncMock(return_value="discovered")),
        patch(
            "app.routers.hosts.pack_discovery_service.list_intake_candidates", new=AsyncMock(return_value=["candidate"])
        ),
    ):
        assert await hosts.discover_devices(host_id, db=object()) == "discovered"
        assert await hosts.intake_candidates(host_id, db=object()) == ["candidate"]

    body = SimpleNamespace(add_identity_values=["serial"], remove_identity_values=[])
    with (
        patch("app.routers.hosts.host_service.get_host", new=AsyncMock(return_value=host)),
        patch("app.routers.hosts.pack_discovery_service.discover_devices", new=AsyncMock(return_value="fresh")),
        patch(
            "app.routers.hosts.pack_discovery_service.confirm_discovery",
            new=AsyncMock(side_effect=DeviceIdentityConflictError("dupe")),
        ),
    ):
        with pytest.raises(HTTPException) as exc:
            await hosts.confirm_discovery(host_id, body, db=object())  # type: ignore[arg-type]
    assert exc.value.status_code == 409

    with (
        patch("app.routers.hosts.host_service.get_host", new=AsyncMock(return_value=host)),
        patch("app.routers.hosts.pack_discovery_service.discover_devices", new=AsyncMock(return_value="fresh")),
        patch("app.routers.hosts.pack_discovery_service.confirm_discovery", new=AsyncMock(return_value="confirmed")),
    ):
        assert await hosts.confirm_discovery(host_id, body, db=object()) == "confirmed"  # type: ignore[arg-type]


def test_host_terminal_helpers() -> None:
    with (
        patch("app.routers.host_terminal.settings_service.get", new=Mock(return_value="")),
        patch("app.routers.host_terminal.auth.is_auth_enabled", new=Mock(return_value=False)),
    ):
        assert host_terminal._origin_allowed("https://example.test") is True
        assert host_terminal._resolve_browser_username(SimpleNamespace(headers={})) is None  # type: ignore[arg-type]

    with patch("app.routers.host_terminal.settings_service.get", new=Mock(return_value="https://ok.test")):
        assert host_terminal._origin_allowed("https://ok.test") is True
        assert host_terminal._origin_allowed("https://bad.test") is False

    state = SimpleNamespace(authenticated=True, username="admin")
    with (
        patch("app.routers.host_terminal.auth.is_auth_enabled", new=Mock(return_value=True)),
        patch("app.routers.host_terminal.auth.resolve_browser_session_from_headers", new=Mock(return_value=state)),
    ):
        assert host_terminal._resolve_browser_username(SimpleNamespace(headers={})) == "admin"  # type: ignore[arg-type]

    state = SimpleNamespace(authenticated=False, username=None)
    with (
        patch("app.routers.host_terminal.auth.is_auth_enabled", new=Mock(return_value=True)),
        patch("app.routers.host_terminal.auth.resolve_browser_session_from_headers", new=Mock(return_value=state)),
    ):
        assert host_terminal._resolve_browser_username(SimpleNamespace(headers={})) is None  # type: ignore[arg-type]

    assert host_terminal._agent_terminal_url("10.0.0.1", 5100).endswith("://10.0.0.1:5100/agent/terminal")
    assert host_terminal._agent_terminal_url("fd00::1", 5100).endswith("://[fd00::1]:5100/agent/terminal")


class _FakeHostTerminalSessionScope:
    async def __aenter__(self) -> object:
        return object()

    async def __aexit__(self, *args: object) -> None:
        return None


class _FakeHostTerminalWebSocket:
    def __init__(self, *, origin: str | None = None, inbound: str = "browser-input") -> None:
        self.headers = {"origin": origin} if origin is not None else {}
        self.client = SimpleNamespace(host="127.0.0.1")
        self.inbound = inbound
        self.accepted = False
        self.sent: list[str] = []
        self.close_codes: list[int] = []

    async def close(self, code: int = 1000) -> None:
        self.close_codes.append(code)

    async def accept(self) -> None:
        self.accepted = True

    async def send_text(self, data: str) -> None:
        self.sent.append(data)

    async def receive_text(self) -> str:
        return self.inbound


async def test_host_terminal_rejects_origin_and_unauthenticated_browser() -> None:
    host_id = uuid.uuid4()

    bad_origin_ws = _FakeHostTerminalWebSocket(origin="https://bad.test")
    with (
        patch("app.routers.host_terminal.settings_service.get", side_effect=[True, "https://ok.test"]),
        patch("app.routers.host_terminal.auth.is_auth_enabled", new=Mock(return_value=False)),
    ):
        await host_terminal.host_terminal(bad_origin_ws, host_id)  # type: ignore[arg-type]
    assert bad_origin_ws.close_codes == [1008]

    unauthenticated_ws = _FakeHostTerminalWebSocket(origin="https://ok.test")
    session_state = SimpleNamespace(authenticated=False, username=None)
    with (
        patch("app.routers.host_terminal.settings_service.get", side_effect=[True, "https://ok.test"]),
        patch("app.routers.host_terminal.auth.is_auth_enabled", new=Mock(return_value=True)),
        patch(
            "app.routers.host_terminal.auth.resolve_browser_session_from_headers",
            new=Mock(return_value=session_state),
        ),
    ):
        await host_terminal.host_terminal(unauthenticated_ws, host_id)  # type: ignore[arg-type]
    assert unauthenticated_ws.close_codes == [1008]


async def test_host_terminal_adapter_methods_and_proxy_error_path() -> None:
    host_id = uuid.uuid4()
    ws = _FakeHostTerminalWebSocket(origin="https://ok.test")
    host = SimpleNamespace(
        id=host_id,
        status=SimpleNamespace(value="online"),
        ip="10.0.0.10",
        agent_port=5100,
    )
    session_id = uuid.uuid4()

    async def proxy_terminal(*, browser: object, agent_url: str, agent_token: str) -> str:
        assert agent_url.endswith("://10.0.0.10:5100/agent/terminal")
        assert agent_token == "token"
        await browser.send_text("agent-output")
        assert await browser.receive_text() == "browser-input"
        await browser.close(code=1001)
        raise RuntimeError("proxy exploded")

    close_session = AsyncMock()
    with (
        patch("app.routers.host_terminal.settings_service.get", side_effect=[True, "https://ok.test"]),
        patch("app.routers.host_terminal.auth.is_auth_enabled", new=Mock(return_value=False)),
        patch("app.routers.host_terminal.async_session", new=Mock(return_value=_FakeHostTerminalSessionScope())),
        patch("app.routers.host_terminal.host_service.get_host", new=AsyncMock(return_value=host)),
        patch("app.routers.host_terminal.host_terminal_audit.open_session", new=AsyncMock(return_value=session_id)),
        patch("app.routers.host_terminal.host_terminal_audit.close_session", new=close_session),
        patch("app.routers.host_terminal.proxy_terminal_session", new=proxy_terminal),
        patch("app.routers.host_terminal.settings.agent_terminal_token", "token"),
    ):
        await host_terminal.host_terminal(ws, host_id)  # type: ignore[arg-type]

    assert ws.accepted is True
    assert ws.sent == ["agent-output"]
    assert ws.close_codes == [1001, 1000]
    close_session.assert_awaited_once()
    assert close_session.await_args.kwargs["session_id"] == session_id
    assert close_session.await_args.kwargs["close_reason"] == "proxy_error"


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
        "auto_manage": False,
        "appium_node": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


async def test_devices_control_maintenance_config_session_and_refresh_paths() -> None:
    device_id = uuid.uuid4()
    device = _control_device(id=device_id)
    serialized = {"id": str(device_id)}

    for call, service_name in (
        (lambda: devices_control.enter_device_maintenance(device_id, object(), db=object()), "enter_maintenance"),
        (lambda: devices_control.exit_device_maintenance(device_id, db=object()), "exit_maintenance"),
    ):
        with (
            patch("app.routers.devices_control.get_device_for_update_or_404", new=AsyncMock(return_value=device)),
            patch(
                f"app.routers.devices_control.maintenance_service.{service_name}",
                new=AsyncMock(side_effect=ValueError("bad")),
            ),
        ):
            with pytest.raises(HTTPException) as exc:
                await call()
        assert exc.value.status_code == 409

        with (
            patch("app.routers.devices_control.get_device_for_update_or_404", new=AsyncMock(return_value=device)),
            patch(
                f"app.routers.devices_control.maintenance_service.{service_name}", new=AsyncMock(return_value=device)
            ),
            patch(
                "app.routers.devices_control.device_presenter.serialize_device", new=AsyncMock(return_value=serialized)
            ),
        ):
            assert await call() == serialized

    config = {"env": {"A": "B"}}
    with (
        patch("app.routers.devices_control.get_device_or_404", new=AsyncMock(return_value=device)),
        patch("app.routers.devices_control.get_device_for_update_or_404", new=AsyncMock(return_value=device)),
        patch("app.routers.devices_control.config_service.get_device_config", new=AsyncMock(return_value=config)),
        patch("app.routers.devices_control.config_service.replace_device_config", new=AsyncMock(return_value=config)),
        patch("app.routers.devices_control.config_service.merge_device_config", new=AsyncMock(return_value=config)),
    ):
        assert await devices_control.get_device_config(device_id, keys=" env , other ", db=object()) == config
        assert await devices_control.replace_device_config(device_id, {"env": {}}, db=object()) == config
        assert await devices_control.merge_device_config(device_id, {"env": {}}, db=object()) == config

    audit_log = SimpleNamespace(
        id=uuid.uuid4(),
        previous_config={},
        new_config={"a": 1},
        changed_by="admin",
        changed_at=datetime(2026, 5, 1, tzinfo=UTC),
    )
    with (
        patch("app.routers.devices_control.get_device_or_404", new=AsyncMock(return_value=device)),
        patch("app.routers.devices_control.config_service.get_config_history", new=AsyncMock(return_value=[audit_log])),
    ):
        history = await devices_control.get_config_history(device_id, db=object())
    assert history[0]["changed_at"] == "2026-05-01T00:00:00+00:00"

    with (
        patch("app.routers.devices_control.get_device_for_update_or_404", new=AsyncMock(return_value=device)),
        patch(
            "app.routers.devices_control.session_viability.run_session_viability_probe",
            new=AsyncMock(side_effect=ValueError("busy")),
        ),
    ):
        with pytest.raises(HTTPException) as exc:
            await devices_control.device_session_test(device_id, db=object())
    assert exc.value.status_code == 409

    with (
        patch("app.routers.devices_control.get_device_for_update_or_404", new=AsyncMock(return_value=device)),
        patch(
            "app.routers.devices_control.session_viability.run_session_viability_probe",
            new=AsyncMock(return_value={"status": "passed"}),
        ),
    ):
        assert await devices_control.device_session_test(device_id, db=object()) == {"status": "passed"}

    missing_host = _control_device(host_id=None)
    with patch("app.routers.devices_control.get_device_or_404", new=AsyncMock(return_value=missing_host)):
        with pytest.raises(HTTPException) as exc:
            await devices_control.refresh_device_properties(device_id, db=object())
    assert exc.value.status_code == 400

    db = SimpleNamespace(refresh=AsyncMock())
    with (
        patch("app.routers.devices_control.get_device_or_404", new=AsyncMock(return_value=device)),
        patch("app.routers.devices_control.pack_discovery_service.refresh_device_properties", new=AsyncMock()),
        patch("app.routers.devices_control.device_presenter.serialize_device", new=AsyncMock(return_value=serialized)),
    ):
        assert await devices_control.refresh_device_properties(device_id, db=db) == serialized
    db.refresh.assert_awaited_once_with(device)


async def test_devices_control_reconnect_lifecycle_health_and_logs_paths() -> None:
    device_id = uuid.uuid4()
    lifecycle_actions = [{"id": "reconnect"}, {"id": "state"}]
    resolved = SimpleNamespace(lifecycle_actions=lifecycle_actions)
    device = _control_device(id=device_id)

    with (
        patch("app.routers.devices_control.get_device_or_404", new=AsyncMock(return_value=device)),
        patch("app.routers.devices_control.resolve_pack_platform", new=AsyncMock(side_effect=LookupError("missing"))),
    ):
        with pytest.raises(HTTPException) as exc:
            await devices_control.reconnect_device(device_id, db=object())
    assert exc.value.status_code == 400

    for bad_device, detail in (
        (_control_device(connection_type=ConnectionType.usb), "network-connected"),
        (_control_device(ip_address=None), "no IP"),
        (_control_device(host=None), "no host"),
        (_control_device(connection_target=None), "no connection target"),
    ):
        with (
            patch("app.routers.devices_control.get_device_or_404", new=AsyncMock(return_value=bad_device)),
            patch("app.routers.devices_control.resolve_pack_platform", new=AsyncMock(return_value=resolved)),
            patch("app.routers.devices_control.platform_has_lifecycle_action", new=Mock(return_value=True)),
        ):
            with pytest.raises(HTTPException) as exc:
                await devices_control.reconnect_device(device_id, db=object())
        assert detail in str(exc.value.detail)

    with (
        patch("app.routers.devices_control.get_device_or_404", new=AsyncMock(return_value=device)),
        patch("app.routers.devices_control.resolve_pack_platform", new=AsyncMock(return_value=resolved)),
        patch("app.routers.devices_control.platform_has_lifecycle_action", new=Mock(return_value=False)),
    ):
        with pytest.raises(HTTPException) as exc:
            await devices_control.reconnect_device(device_id, db=object())
    assert "not supported" in str(exc.value.detail)

    # Phase 2: narrowed except — RuntimeError is NOT NodeManagerError and must bubble, not become 502
    auto_device = _control_device(auto_manage=True, appium_node=SimpleNamespace(observed_running=False))
    with (
        patch("app.routers.devices_control.get_device_or_404", new=AsyncMock(return_value=auto_device)),
        patch("app.routers.devices_control.resolve_pack_platform", new=AsyncMock(return_value=resolved)),
        patch("app.routers.devices_control.platform_has_lifecycle_action", new=Mock(return_value=True)),
        patch(
            "app.routers.devices_control.pack_device_lifecycle_action", new=AsyncMock(return_value={"success": True})
        ),
        patch("app.routers.devices_control.node_manager.start_node", new=AsyncMock(side_effect=RuntimeError("boom"))),
    ):
        with pytest.raises(RuntimeError, match="boom"):
            await devices_control.reconnect_device(device_id, db=object())

    with (
        patch("app.routers.devices_control.get_device_or_404", new=AsyncMock(return_value=device)),
        patch("app.routers.devices_control.resolve_pack_platform", new=AsyncMock(return_value=resolved)),
        patch("app.routers.devices_control.platform_has_lifecycle_action", new=Mock(return_value=True)),
        patch(
            "app.routers.devices_control.pack_device_lifecycle_action", new=AsyncMock(return_value={"success": True})
        ),
    ):
        reconnect = await devices_control.reconnect_device(device_id, db=object())
    assert reconnect["message"] == "Reconnected"

    with (
        patch("app.routers.devices_control.get_device_for_update_or_404", new=AsyncMock(return_value=device)),
        patch("app.routers.devices_control.resolve_pack_platform", new=AsyncMock(side_effect=LookupError("missing"))),
    ):
        with pytest.raises(HTTPException) as exc:
            await devices_control.device_lifecycle_action(device_id, "state", db=object())
    assert exc.value.status_code == 400

    for bad_device, detail in (
        (_control_device(host=None), "no host"),
        (_control_device(connection_target=None), "no connection target"),
    ):
        with (
            patch("app.routers.devices_control.get_device_for_update_or_404", new=AsyncMock(return_value=bad_device)),
            patch("app.routers.devices_control.resolve_pack_platform", new=AsyncMock(return_value=resolved)),
            patch("app.routers.devices_control.platform_has_lifecycle_action", new=Mock(return_value=True)),
        ):
            with pytest.raises(HTTPException) as exc:
                await devices_control.device_lifecycle_action(device_id, "state", db=object())
        assert detail in str(exc.value.detail)

    db = SimpleNamespace(commit=AsyncMock())
    with (
        patch("app.routers.devices_control.get_device_for_update_or_404", new=AsyncMock(return_value=device)),
        patch("app.routers.devices_control.resolve_pack_platform", new=AsyncMock(return_value=resolved)),
        patch("app.routers.devices_control.platform_has_lifecycle_action", new=Mock(return_value=True)),
        patch(
            "app.routers.devices_control.pack_device_lifecycle_action", new=AsyncMock(return_value={"state": "running"})
        ),
        patch("app.routers.devices_control.device_health_service.update_emulator_state", new=AsyncMock()),
    ):
        assert await devices_control.device_lifecycle_action(device_id, "state", db=db) == {"state": "running"}
    db.commit.assert_awaited_once()

    node = SimpleNamespace(port=4731, observed_running=True, health_running=None, health_state=None)
    health_device = _control_device(appium_node=node)
    with (
        patch("app.routers.devices_control.get_device_or_404", new=AsyncMock(return_value=health_device)),
        patch("app.routers.devices_control.require_management_host", new=Mock(return_value=health_device.host)),
        patch("app.routers.devices_control.fetch_appium_status", new=AsyncMock(return_value={"running": False})),
        patch("app.routers.devices_control.fetch_pack_device_health", new=AsyncMock(return_value={"healthy": True})),
        patch("app.routers.devices_control.session_viability.get_session_viability", new=AsyncMock(return_value=None)),
        patch("app.routers.devices_control.lifecycle_policy.build_lifecycle_policy", new=AsyncMock(return_value={})),
    ):
        health = await devices_control.device_health(device_id, db=object())
    assert health["node"]["state"] == "error"
    assert health["healthy"] is False

    with (
        patch(
            "app.routers.devices_control.get_device_or_404",
            new=AsyncMock(return_value=_control_device(appium_node=None)),
        ),
        patch("app.routers.devices_control.require_management_host", new=Mock(return_value=device.host)),
    ):
        assert await devices_control.device_logs(device_id, db=object()) == {"lines": [], "count": 0}

    with (
        patch("app.routers.devices_control.get_device_or_404", new=AsyncMock(return_value=health_device)),
        patch("app.routers.devices_control.require_management_host", new=Mock(return_value=health_device.host)),
        patch("app.routers.devices_control.appium_logs", new=AsyncMock(side_effect=httpx.HTTPError("down"))),
    ):
        with pytest.raises(HTTPException) as exc:
            await devices_control.device_logs(device_id, db=object())
    assert exc.value.status_code == 502

    plugin_id = uuid.uuid4()
    with patch("app.routers.plugins.plugin_service.delete_plugin", new=AsyncMock(return_value=False)):
        with pytest.raises(HTTPException) as exc:
            await plugins_router.delete_plugin(plugin_id, db=object())
    assert exc.value.status_code == 404

    with patch("app.routers.plugins.host_service.get_host", new=AsyncMock(return_value=None)):
        with pytest.raises(HTTPException) as exc:
            await plugins_router.host_plugins(plugin_id, db=object())
    assert exc.value.status_code == 404

    host = SimpleNamespace(id=plugin_id)
    with (
        patch("app.routers.plugins.host_service.get_host", new=AsyncMock(return_value=host)),
        patch("app.routers.plugins.plugin_service.list_plugins", new=AsyncMock(return_value=["required"])),
        patch(
            "app.routers.plugins.plugin_service.get_host_plugin_statuses",
            new=AsyncMock(return_value=[{"status": "ok"}]),
        ),
        patch("app.routers.plugins.plugin_service.sync_host_plugins", new=AsyncMock(return_value={"installed": []})),
    ):
        assert await plugins_router.host_plugins(plugin_id, db=object()) == [{"status": "ok"}]
        assert await plugins_router.sync_host_plugins(plugin_id, db=object()) == {"installed": []}


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

    with (
        patch("app.routers.analytics.analytics_service.get_session_summary", new=AsyncMock(return_value=summary)),
        patch(
            "app.routers.analytics.analytics_service.get_device_utilization", new=AsyncMock(return_value=utilization)
        ),
        patch(
            "app.routers.analytics.analytics_service.get_device_reliability", new=AsyncMock(return_value=reliability)
        ),
        patch(
            "app.routers.analytics.get_fleet_capacity_timeline",
            new=AsyncMock(side_effect=ValueError("date_to must be after date_from")),
        ),
    ):
        assert await analytics.session_summary(db=object(), group_by=GroupByOption.platform) == summary
        csv_response = await analytics.session_summary(db=object(), group_by=GroupByOption.day, export_format="csv")
        assert csv_response.media_type == "text/csv"
        assert (await analytics.device_utilization(db=object(), export_format="csv")).media_type == "text/csv"
        assert (await analytics.device_reliability(db=object(), export_format="csv")).media_type == "text/csv"
        with pytest.raises(HTTPException) as exc:
            await analytics.fleet_capacity_timeline(db=object(), bucket_minutes=5)
    assert exc.value.status_code == 422


async def test_grid_router_summarizes_registry_and_queue() -> None:
    running_node = SimpleNamespace(port=4731, observed_running=True)
    stopped_node = SimpleNamespace(port=4732, observed_running=False)
    devices = [
        SimpleNamespace(
            id=uuid.uuid4(),
            identity_value="serial-1",
            connection_target="serial-1",
            name="Pixel",
            platform_id="android_mobile",
            operational_state=SimpleNamespace(value="available"),
            hold=None,
            appium_node=running_node,
        ),
        SimpleNamespace(
            id=uuid.uuid4(),
            identity_value="serial-2",
            connection_target="serial-2",
            name="Tablet",
            platform_id="android_mobile",
            operational_state=SimpleNamespace(value="offline"),
            hold=SimpleNamespace(value="maintenance"),
            appium_node=stopped_node,
        ),
    ]
    grid_data = {
        "value": {
            "nodes": [{"slots": [{"session": {"id": "s1"}}, {"session": None}]}],
            "sessionQueueRequests": [{"requestId": "queued"}],
        }
    }

    with (
        patch("app.routers.grid.grid_service.get_grid_status", new=AsyncMock(return_value=grid_data)),
        patch("app.routers.grid.device_service.list_devices", new=AsyncMock(return_value=devices)),
    ):
        status = await grid.grid_status(db=object())
        queue = await grid.grid_queue()

    assert status["registry"]["device_count"] == 2
    assert status["registry"]["devices"][0]["node_state"] == "running"
    assert status["registry"]["devices"][1]["hold"] == "maintenance"
    assert status["active_sessions"] == 1
    assert status["queue_size"] == 1
    assert queue == {"queue_size": 1, "requests": [{"requestId": "queued"}]}


async def test_nodes_router_validation_branches() -> None:
    device_id = uuid.uuid4()
    device = SimpleNamespace(id=device_id, hold=None, appium_node=None, host_id=uuid.uuid4())

    with patch(
        "app.routers.nodes.run_service.get_device_reservation",
        new=AsyncMock(return_value=SimpleNamespace(name="run", id="r1")),
    ):
        with pytest.raises(HTTPException) as exc:
            await nodes_router._assert_device_not_reserved(device, db=object())
    assert exc.value.status_code == 409

    device.hold = DeviceHold.maintenance
    with pytest.raises(HTTPException) as exc:
        nodes_router._assert_startable_outside_maintenance(device)
    assert exc.value.status_code == 409
    device.hold = None

    setup_required = SimpleNamespace(readiness_state="setup_required", missing_setup_fields=["identity_value"])
    with patch("app.routers.nodes.assess_device_async", new=AsyncMock(return_value=setup_required)):
        with pytest.raises(HTTPException) as exc:
            await nodes_router._assert_device_verified(object(), device, action="start")
    assert "identity_value" in str(exc.value.detail)

    unverified = SimpleNamespace(readiness_state="failed", missing_setup_fields=[])
    with patch("app.routers.nodes.assess_device_async", new=AsyncMock(return_value=unverified)):
        with pytest.raises(HTTPException) as exc:
            await nodes_router._assert_device_verified(object(), device, action="start")
    assert exc.value.status_code == 409

    running_node = SimpleNamespace(desired_state=AppiumDesiredState.running)
    device.appium_node = running_node
    with (
        patch("app.routers.nodes.get_device_for_update_or_404", new=AsyncMock(return_value=device)),
        patch("app.routers.nodes.run_service.get_device_reservation", new=AsyncMock(return_value=None)),
        patch(
            "app.routers.nodes.assess_device_async",
            new=AsyncMock(return_value=SimpleNamespace(readiness_state="verified", missing_setup_fields=[])),
        ),
    ):
        with pytest.raises(HTTPException) as exc:
            await nodes_router.start_node(device_id, db=object())
    assert exc.value.status_code == 400

    device.appium_node = None
    device.host_id = None
    with (
        patch("app.routers.nodes.get_device_for_update_or_404", new=AsyncMock(return_value=device)),
        patch("app.routers.nodes.run_service.get_device_reservation", new=AsyncMock(return_value=None)),
        patch(
            "app.routers.nodes.assess_device_async",
            new=AsyncMock(return_value=SimpleNamespace(readiness_state="verified", missing_setup_fields=[])),
        ),
        patch("app.routers.nodes.is_ready_for_use_async", new=AsyncMock(return_value=True)),
    ):
        with pytest.raises(HTTPException) as exc:
            await nodes_router.start_node(device_id, db=object())
    assert "no host assigned" in str(exc.value.detail)

    device.host_id = uuid.uuid4()
    started_node = SimpleNamespace(desired_state=AppiumDesiredState.running)
    with (
        patch("app.routers.nodes.get_device_for_update_or_404", new=AsyncMock(return_value=device)),
        patch("app.routers.nodes.run_service.get_device_reservation", new=AsyncMock(return_value=None)),
        patch(
            "app.routers.nodes.assess_device_async",
            new=AsyncMock(return_value=SimpleNamespace(readiness_state="verified", missing_setup_fields=[])),
        ),
        patch("app.routers.nodes.is_ready_for_use_async", new=AsyncMock(return_value=True)),
        patch("app.routers.nodes.node_manager.start_node", new=AsyncMock(return_value=started_node)),
    ):
        assert await nodes_router.start_node(device_id, db=object()) is started_node


async def test_nodes_stop_and_restart_error_and_convergence_paths() -> None:
    device_id = uuid.uuid4()
    stopped_device = SimpleNamespace(id=device_id, hold=None, appium_node=None)
    with (
        patch("app.routers.nodes.get_device_for_update_or_404", new=AsyncMock(return_value=stopped_device)),
        patch("app.routers.nodes.run_service.get_device_reservation", new=AsyncMock(return_value=None)),
    ):
        with pytest.raises(HTTPException) as exc:
            await nodes_router.stop_node(device_id, db=object())
    assert exc.value.status_code == 400

    running_node = SimpleNamespace(desired_state=AppiumDesiredState.running)
    running_device = SimpleNamespace(id=device_id, hold=None, appium_node=running_node)
    restarted = SimpleNamespace(id=uuid.uuid4())
    fake_db = SimpleNamespace(refresh=AsyncMock())
    with (
        patch("app.routers.nodes.get_device_for_update_or_404", new=AsyncMock(return_value=running_device)),
        patch("app.routers.nodes.run_service.get_device_reservation", new=AsyncMock(return_value=None)),
        patch(
            "app.routers.nodes.assess_device_async",
            new=AsyncMock(return_value=SimpleNamespace(readiness_state="verified", missing_setup_fields=[])),
        ),
        patch("app.routers.nodes.node_manager.restart_node", new=AsyncMock(return_value=restarted)),
        patch("app.routers.nodes.converge_device_now", new=AsyncMock(side_effect=RuntimeError("converge failed"))),
    ):
        assert await nodes_router.restart_node(device_id, db=fake_db) is restarted
    fake_db.refresh.assert_awaited_once_with(restarted)


async def test_nodes_router_additional_start_stop_restart_branches() -> None:
    device_id = uuid.uuid4()
    verified = SimpleNamespace(readiness_state="verified", missing_setup_fields=[])

    device = SimpleNamespace(id=device_id, hold=None, appium_node=None, host_id=uuid.uuid4())
    with (
        patch("app.routers.nodes.get_device_for_update_or_404", new=AsyncMock(return_value=device)),
        patch("app.routers.nodes.run_service.get_device_reservation", new=AsyncMock(return_value=None)),
        patch("app.routers.nodes.assess_device_async", new=AsyncMock(return_value=verified)),
        patch("app.routers.nodes.is_ready_for_use_async", new=AsyncMock(return_value=False)),
        patch("app.routers.nodes.readiness_error_detail_async", new=AsyncMock(return_value="not ready")),
    ):
        with pytest.raises(HTTPException) as exc:
            await nodes_router.start_node(device_id, db=object())
    assert exc.value.status_code == 400
    assert exc.value.detail == "not ready"

    # Phase 2: narrowed except — RuntimeError is NOT NodeManagerError and must bubble, not become 400
    with (
        patch("app.routers.nodes.get_device_for_update_or_404", new=AsyncMock(return_value=device)),
        patch("app.routers.nodes.run_service.get_device_reservation", new=AsyncMock(return_value=None)),
        patch("app.routers.nodes.assess_device_async", new=AsyncMock(return_value=verified)),
        patch("app.routers.nodes.is_ready_for_use_async", new=AsyncMock(return_value=True)),
        patch("app.routers.nodes.node_manager.start_node", new=AsyncMock(side_effect=RuntimeError("boom"))),
    ):
        with pytest.raises(RuntimeError, match="boom"):
            await nodes_router.start_node(device_id, db=object())

    running_node = SimpleNamespace(desired_state=AppiumDesiredState.running)
    running_device = SimpleNamespace(id=device_id, hold=None, appium_node=running_node, host_id=uuid.uuid4())
    stopped_node = SimpleNamespace(desired_state=AppiumDesiredState.stopped)
    with (
        patch("app.routers.nodes.get_device_for_update_or_404", new=AsyncMock(return_value=running_device)),
        patch("app.routers.nodes.run_service.get_device_reservation", new=AsyncMock(return_value=None)),
        patch("app.routers.nodes.node_manager.stop_node", new=AsyncMock(return_value=stopped_node)),
    ):
        assert await nodes_router.stop_node(device_id, db=object()) is stopped_node

    # Phase 2: narrowed except — RuntimeError is NOT NodeManagerError and must bubble, not become 400
    with (
        patch("app.routers.nodes.get_device_for_update_or_404", new=AsyncMock(return_value=running_device)),
        patch("app.routers.nodes.run_service.get_device_reservation", new=AsyncMock(return_value=None)),
        patch("app.routers.nodes.node_manager.stop_node", new=AsyncMock(side_effect=RuntimeError("stop failed"))),
    ):
        with pytest.raises(RuntimeError, match="stop failed"):
            await nodes_router.stop_node(device_id, db=object())

    fallback_started = SimpleNamespace(desired_state=AppiumDesiredState.running)
    non_running_device = SimpleNamespace(
        id=device_id,
        hold=None,
        appium_node=SimpleNamespace(desired_state=AppiumDesiredState.stopped),
        host_id=uuid.uuid4(),
    )
    with (
        patch("app.routers.nodes.get_device_for_update_or_404", new=AsyncMock(return_value=non_running_device)),
        patch("app.routers.nodes.run_service.get_device_reservation", new=AsyncMock(return_value=None)),
        patch("app.routers.nodes.assess_device_async", new=AsyncMock(return_value=verified)),
        patch("app.routers.nodes.is_ready_for_use_async", new=AsyncMock(return_value=True)),
        patch("app.routers.nodes.node_manager.start_node", new=AsyncMock(return_value=fallback_started)),
    ):
        assert await nodes_router.restart_node(device_id, db=object()) is fallback_started

    restarted = SimpleNamespace(id=uuid.uuid4())
    converged = SimpleNamespace(id=uuid.uuid4())
    fake_db = SimpleNamespace(refresh=AsyncMock())
    with (
        patch("app.routers.nodes.get_device_for_update_or_404", new=AsyncMock(return_value=running_device)),
        patch("app.routers.nodes.run_service.get_device_reservation", new=AsyncMock(return_value=None)),
        patch("app.routers.nodes.assess_device_async", new=AsyncMock(return_value=verified)),
        patch("app.routers.nodes.node_manager.restart_node", new=AsyncMock(return_value=restarted)),
        patch("app.routers.nodes.converge_device_now", new=AsyncMock(return_value=converged)),
    ):
        assert await nodes_router.restart_node(device_id, db=fake_db) is converged
    fake_db.refresh.assert_awaited_once_with(converged)


async def test_device_group_router_bulk_and_membership_branches() -> None:
    group_id = uuid.uuid4()
    device_ids = [uuid.uuid4()]

    with patch("app.routers.device_groups.device_group_service.get_group_device_ids", new=AsyncMock(return_value=[])):
        with pytest.raises(HTTPException) as exc:
            await device_groups._group_device_ids_or_404(object(), group_id)
    assert exc.value.status_code == 404

    with (
        patch(
            "app.routers.device_groups.device_group_service.get_group",
            new=AsyncMock(return_value={"group_type": "dynamic"}),
        ),
    ):
        with pytest.raises(HTTPException) as exc:
            await device_groups.add_members(group_id, body=SimpleNamespace(device_ids=device_ids), db=object())
        assert exc.value.status_code == 400
        with pytest.raises(HTTPException) as exc:
            await device_groups.remove_members(group_id, body=SimpleNamespace(device_ids=device_ids), db=object())
        assert exc.value.status_code == 400

    async def assert_bulk(
        call: Callable[..., Awaitable[dict[str, Any]]],
        service_name: str,
        *args: object,
    ) -> None:
        with (
            patch(
                "app.routers.device_groups.device_group_service.get_group_device_ids",
                new=AsyncMock(return_value=device_ids),
            ),
            patch(f"app.routers.device_groups.bulk_service.{service_name}", new=AsyncMock(return_value={"ok": 1})),
        ):
            assert await call(group_id, *args, db=object()) == {"ok": 1}

    await assert_bulk(device_groups.group_bulk_start, "bulk_start_nodes")
    await assert_bulk(device_groups.group_bulk_stop, "bulk_stop_nodes")
    await assert_bulk(device_groups.group_bulk_restart, "bulk_restart_nodes")
    await assert_bulk(
        device_groups.group_bulk_enter_maintenance,
        "bulk_enter_maintenance",
        BulkMaintenanceEnter(device_ids=device_ids),
    )
    await assert_bulk(device_groups.group_bulk_exit_maintenance, "bulk_exit_maintenance")
    await assert_bulk(device_groups.group_bulk_reconnect, "bulk_reconnect")
    await assert_bulk(
        device_groups.group_bulk_update_tags,
        "bulk_update_tags",
        BulkTagsUpdate(device_ids=device_ids, tags={"lab": "east"}, merge=True),
    )
    await assert_bulk(device_groups.group_bulk_delete, "bulk_delete")


async def test_driver_pack_upload_export_and_template_error_mapping() -> None:
    assert await driver_pack_uploads._read_limited_upload(ChunkUpload([b"abc", b"def"])) == b"abcdef"

    with patch("app.routers.driver_pack_uploads.MAX_PACK_TARBALL_BYTES", new=3):
        with pytest.raises(HTTPException) as exc:
            await driver_pack_uploads._read_limited_upload(ChunkUpload([b"abcd"]))
    assert exc.value.status_code == 413

    with pytest.raises(HTTPException) as exc:
        await driver_pack_uploads.upload(
            tarball=ChunkUpload([]),  # type: ignore[arg-type]
            username="admin",
            session=DummySession(),
            storage=object(),
        )
    assert exc.value.status_code == 400

    with patch("app.routers.driver_pack_uploads.pack_release_service.list_releases", new=AsyncMock(return_value=None)):
        with pytest.raises(HTTPException) as exc:
            await driver_pack_uploads.list_releases("missing", session=object())
    assert exc.value.status_code == 404
    with patch(
        "app.routers.driver_pack_uploads.pack_release_service.list_releases",
        new=AsyncMock(return_value="releases"),
    ):
        assert await driver_pack_uploads.list_releases("pack", session=object()) == "releases"

    with patch("app.routers.driver_pack_uploads.PackStorageService", new=Mock(return_value="storage")):
        assert driver_pack_uploads.get_pack_storage() == "storage"

    pack = SimpleNamespace(id="local/uploaded")
    session = DummySession()
    with (
        patch("app.routers.driver_pack_uploads.upload_pack", new=AsyncMock(return_value=pack)),
        patch("app.routers.driver_pack_uploads.build_pack_out", new=Mock(return_value={"id": pack.id})),
    ):
        assert await driver_pack_uploads.upload(
            tarball=ChunkUpload([b"tar"]),  # type: ignore[arg-type]
            username="admin",
            session=session,
            storage=object(),
        ) == {"id": "local/uploaded"}
    assert session.committed is True

    for error, status_code in (
        (driver_pack_uploads.PackUploadValidationError("bad manifest"), 400),
        (driver_pack_uploads.PackUploadConflictError("duplicate"), 409),
    ):
        with patch("app.routers.driver_pack_uploads.upload_pack", new=AsyncMock(side_effect=error)):
            with pytest.raises(HTTPException) as exc:
                await driver_pack_uploads.upload(
                    tarball=ChunkUpload([b"tar"]),  # type: ignore[arg-type]
                    username="admin",
                    session=DummySession(),
                    storage=object(),
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
    with (
        patch(
            "app.routers.driver_pack_uploads.pack_release_service.set_current_release", new=AsyncMock(return_value=pack)
        ),
        patch("app.routers.driver_pack_uploads.build_pack_out", new=Mock(return_value={"id": pack.id})),
    ):
        assert await driver_pack_uploads.update_current_release(
            "pack",
            CurrentReleasePatch(release="1.0.0"),
            _username="admin",
            session=session,
        ) == {"id": "local/uploaded"}
    assert session.committed is True

    for error, status_code in (
        (LookupError("missing"), 404),
        (ValueError("current"), 400),
        (RuntimeError("busy"), 409),
    ):
        with patch(
            "app.routers.driver_pack_uploads.pack_release_service.delete_release", new=AsyncMock(side_effect=error)
        ):
            with pytest.raises(HTTPException) as exc:
                await driver_pack_uploads.delete_release("pack", "1.0.0", _username="admin", session=DummySession())
        assert exc.value.status_code == status_code

    delete_session = DummySession()
    with patch("app.routers.driver_pack_uploads.pack_release_service.delete_release", new=AsyncMock(return_value=None)):
        response = await driver_pack_uploads.delete_release("pack", "1.0.0", _username="admin", session=delete_session)
    assert response.status_code == 204
    assert delete_session.committed is True


async def test_driver_pack_authoring_fork_error_mapping_and_success() -> None:
    body = driver_pack_authoring.ForkPackBody(new_pack_id="local/fork", display_name="Fork")

    with pytest.raises(HTTPException) as exc:
        await driver_pack_authoring.fork("source", body, _username="admin", session=DummySession(get_result=object()))
    assert exc.value.status_code == 409

    with pytest.raises(HTTPException) as exc:
        await driver_pack_authoring.fork(
            "source",
            body,
            _username="admin",
            session=DummySession(execute_result=ScalarResult(None)),
        )
    assert exc.value.status_code == 404

    source = SimpleNamespace(id="source", current_release="1.0.0", releases=[])
    with patch("app.routers.driver_pack_authoring.selected_release", new=Mock(return_value=None)):
        with pytest.raises(HTTPException) as exc:
            await driver_pack_authoring.fork(
                "source",
                body,
                _username="admin",
                session=DummySession(execute_result=ScalarResult(source)),
            )
    assert exc.value.status_code == 400

    release = SimpleNamespace(release="1.0.0", manifest_json={"id": "source", "release": "1.0.0"})
    pack = SimpleNamespace(id="local/fork")
    session = DummySession(execute_result=ScalarResult(source))
    with (
        patch("app.routers.driver_pack_authoring.selected_release", new=Mock(return_value=release)),
        patch("app.routers.driver_pack_authoring.PackStorageService", new=Mock(return_value=object())),
        patch("app.routers.driver_pack_authoring.ingest_pack_tarball", new=AsyncMock(return_value=pack)),
        patch("app.routers.driver_pack_authoring.build_pack_out", new=Mock(return_value={"id": "local/fork"})),
    ):
        assert await driver_pack_authoring.fork("source", body, _username="admin", session=session) == {
            "id": "local/fork"
        }
    assert session.committed is True

    for error, status_code in (
        (driver_pack_authoring.PackIngestConflictError("duplicate"), 409),
        (driver_pack_authoring.PackIngestValidationError("bad manifest"), 400),
    ):
        with (
            patch("app.routers.driver_pack_authoring.selected_release", new=Mock(return_value=release)),
            patch("app.routers.driver_pack_authoring.PackStorageService", new=Mock(return_value=object())),
            patch("app.routers.driver_pack_authoring.ingest_pack_tarball", new=AsyncMock(side_effect=error)),
        ):
            with pytest.raises(HTTPException) as exc:
                await driver_pack_authoring.fork(
                    "source",
                    body,
                    _username="admin",
                    session=DummySession(execute_result=ScalarResult(source)),
                )
        assert exc.value.status_code == status_code


async def test_driver_pack_router_error_mapping_and_success_paths() -> None:
    pack_id = "local/router-pack"
    pack_out = SimpleNamespace(id=pack_id)
    with (
        patch("app.routers.driver_packs.list_catalog", new=AsyncMock(return_value={"packs": []})),
        patch("app.routers.driver_packs.get_pack_detail", new=AsyncMock(side_effect=[None, pack_out, None, pack_out])),
        patch("app.routers.driver_packs.get_platforms", new=AsyncMock(side_effect=[None, {"platforms": []}])),
        patch(
            "app.routers.driver_packs.get_driver_pack_host_status",
            new=AsyncMock(return_value={"pack_id": pack_id, "hosts": []}),
        ),
    ):
        assert await driver_packs.catalog(session=object()) == {"packs": []}
        with pytest.raises(HTTPException) as exc:
            await driver_packs.get_pack(pack_id, session=object())
        assert exc.value.status_code == 404
        assert await driver_packs.get_pack(pack_id, session=object()) is pack_out
        with pytest.raises(HTTPException) as exc:
            await driver_packs.platforms(pack_id, session=object())
        assert exc.value.status_code == 404
        assert await driver_packs.platforms(pack_id, session=object()) == {"platforms": []}
        with pytest.raises(HTTPException) as exc:
            await driver_packs.hosts(pack_id, session=object())
        assert exc.value.status_code == 404
        assert (await driver_packs.hosts(pack_id, session=object())).hosts == []

    with pytest.raises(HTTPException) as exc:
        await driver_packs.update_pack(
            pack_id,
            driver_packs.PackPatch(state="not-a-state"),
            _username="admin",
            session=object(),
        )
    assert exc.value.status_code == 400

    for error, status_code in ((LookupError("missing"), 404), (ValueError("bad transition"), 400)):
        with patch("app.routers.driver_packs.transition_pack_state", new=AsyncMock(side_effect=error)):
            with pytest.raises(HTTPException) as exc:
                await driver_packs.update_pack(
                    pack_id,
                    driver_packs.PackPatch(state="enabled"),
                    _username="admin",
                    session=object(),
                )
        assert exc.value.status_code == status_code

    with patch("app.routers.driver_packs.set_runtime_policy", new=AsyncMock(side_effect=LookupError("missing"))):
        with pytest.raises(HTTPException) as exc:
            await driver_packs.update_runtime_policy(
                pack_id,
                driver_packs.RuntimePolicyPatch(runtime_policy=RuntimePolicy()),
                _username="admin",
                session=object(),
            )
    assert exc.value.status_code == 404

    dummy_session = DummySession()
    with patch("app.routers.driver_packs.delete_pack", new=AsyncMock(side_effect=RuntimeError("in use"))):
        with pytest.raises(HTTPException) as exc:
            await driver_packs.delete_driver_pack(pack_id, _username="admin", session=dummy_session)
    assert exc.value.status_code == 409

    with patch("app.routers.driver_packs.delete_pack", new=AsyncMock(return_value=None)):
        response = await driver_packs.delete_driver_pack(pack_id, _username="admin", session=dummy_session)
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

    with patch("app.routers.webhooks.webhook_service.get_webhook", new=AsyncMock(return_value=None)):
        for call in (
            lambda: webhooks.get_webhook(webhook_id, db=object()),
            lambda: webhooks.update_webhook(webhook_id, data=webhooks.WebhookUpdate(enabled=False), db=object()),
            lambda: webhooks.delete_webhook(webhook_id, db=object()),
            lambda: webhooks.test_webhook(webhook_id, db=object()),
            lambda: webhooks.list_webhook_deliveries(webhook_id, db=object()),
            lambda: webhooks.retry_webhook_delivery(webhook_id, delivery_id, db=object()),
        ):
            with pytest.raises(HTTPException) as exc:
                await call()
            assert exc.value.status_code == 404

    with (
        patch("app.routers.webhooks.webhook_service.get_webhook", new=AsyncMock(return_value=webhook)),
        patch("app.routers.webhooks.event_bus.publish", new=AsyncMock()) as publish,
        patch("app.routers.webhooks.webhook_dispatcher.list_deliveries", new=AsyncMock(return_value=([delivery], 1))),
        patch("app.routers.webhooks.webhook_dispatcher.retry_delivery", new=AsyncMock(side_effect=[None, delivery])),
    ):
        assert (await webhooks.test_webhook(webhook_id, db=object()))["webhook_name"] == "alerts"
        publish.assert_awaited_once()
        deliveries = await webhooks.list_webhook_deliveries(webhook_id, db=object())
        assert deliveries.total == 1
        with pytest.raises(HTTPException) as exc:
            await webhooks.retry_webhook_delivery(webhook_id, delivery_id, db=object())
        assert exc.value.status_code == 404
        retried = await webhooks.retry_webhook_delivery(webhook_id, delivery_id, db=object())
        assert retried.id == delivery_id


async def test_runs_router_parses_filters_and_maps_service_errors() -> None:
    assert runs._parse_run_filter_datetime("2026-05-01") == datetime(2026, 5, 1, tzinfo=UTC)
    assert runs._parse_run_filter_datetime("2026-05-01", end_of_day=True).time().hour == 23
    assert runs._parse_run_filter_datetime("2026-05-01T12:00:00") == datetime(2026, 5, 1, 12, tzinfo=UTC)

    payload = RunCreate(name="ci", requirements=[{"pack_id": "pack", "platform_id": "android", "count": 1}])
    with pytest.raises(HTTPException) as exc:
        await runs.create_run(payload, include="capabilities", db=object())
    assert exc.value.status_code == 422

    for error, status_code in (
        (PackUnavailableError("missing"), 422),
        (PackDisabledError("disabled"), 422),
        (ValueError("none"), 409),
    ):
        with patch("app.routers.runs.run_service.create_run", new=AsyncMock(side_effect=error)):
            with pytest.raises(HTTPException) as exc:
                await runs.create_run(payload, include=None, db=object())
        assert exc.value.status_code == status_code

    run = _run_obj()
    device_info = runs.ReservedDeviceInfo(
        device_id=str(uuid.uuid4()),
        identity_value="serial",
        pack_id="pack",
        platform_id="android",
        os_version="14",
    )
    with (
        patch("app.routers.runs.run_service.create_run", new=AsyncMock(return_value=(run, [device_info]))),
        patch("app.routers.runs.settings_service.get", new=Mock(return_value="http://grid:4444")),
    ):
        created = await runs.create_run(payload, include=None, db=object())
    assert created.id == run.id
    assert created.grid_url == "http://grid:4444"

    request = SimpleNamespace(query_params={})
    with pytest.raises(HTTPException) as exc:
        await runs.list_runs(request, created_from="bad-date", created_to=None, db=object())
    assert exc.value.status_code == 422

    read = _run_read(run)
    with (
        patch("app.routers.runs.run_service.list_runs", new=AsyncMock(return_value=([run], 1))),
        patch(
            "app.routers.runs.run_service.fetch_session_counts",
            new=AsyncMock(return_value={run.id: read.session_counts}),
        ),
        patch("app.routers.runs.run_service.build_run_read", new=Mock(return_value=read)),
    ):
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
        )
    assert listed.total == 1
    assert listed.items[0].id == run.id

    cursor_request = SimpleNamespace(query_params={"cursor": "bad"})
    with patch(
        "app.routers.runs.run_service.list_runs_cursor",
        new=AsyncMock(side_effect=runs.CursorPaginationError("bad cursor")),
    ):
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
            )
    assert exc.value.status_code == 422


async def test_runs_router_state_transition_endpoints() -> None:
    run = _run_obj()
    read = _run_read(run)
    run_id = run.id
    device_id = uuid.uuid4()

    async def assert_conflict(
        call: Callable[..., Awaitable[Any]],
        service_name: str,
        *args: object,
    ) -> None:
        with patch(f"app.routers.runs.run_service.{service_name}", new=AsyncMock(side_effect=ValueError("bad state"))):
            with pytest.raises(HTTPException) as exc:
                await call(*args, db=object())
        assert exc.value.status_code in {404, 409}

    for call, service_name in (
        (runs.signal_ready, "signal_ready"),
        (runs.signal_active, "signal_active"),
        (runs.complete_run, "complete_run"),
        (runs.cancel_run, "cancel_run"),
        (runs.force_release, "force_release"),
    ):
        await assert_conflict(call, service_name, run_id)

    with (
        patch("app.routers.runs.run_service.report_preparation_failure", new=AsyncMock(side_effect=ValueError("bad"))),
    ):
        with pytest.raises(HTTPException) as exc:
            await runs.report_preparation_failed(
                run_id,
                device_id,
                RunPreparationFailureReport(message="failed"),
                db=object(),
            )
    assert exc.value.status_code == 409

    with patch("app.routers.runs.run_service.cooldown_device", new=AsyncMock(side_effect=ValueError("Run not found"))):
        with pytest.raises(HTTPException) as exc:
            await runs.cooldown_device_endpoint(
                run_id,
                device_id,
                RunCooldownRequest(reason="flaky", ttl_seconds=30),
                db=object(),
            )
    assert exc.value.status_code == 404

    with patch(
        "app.routers.runs.run_service.cooldown_device",
        new=AsyncMock(return_value=(datetime.now(UTC) + timedelta(seconds=30), 1, False, 3)),
    ):
        cooldown = await runs.cooldown_device_endpoint(
            run_id,
            device_id,
            RunCooldownRequest(reason="flaky", ttl_seconds=30),
            db=object(),
        )
    assert cooldown.status == "cooldown_set"

    with patch("app.routers.runs.run_service.heartbeat", new=AsyncMock(return_value=run)):
        heartbeat = await runs.heartbeat(run_id, db=object())
    assert heartbeat.state == run.state

    for call, service_name in ((runs.signal_ready, "signal_ready"), (runs.complete_run, "complete_run")):
        with (
            patch(f"app.routers.runs.run_service.{service_name}", new=AsyncMock(return_value=run)),
            patch(
                "app.routers.runs.run_service.fetch_session_counts",
                new=AsyncMock(return_value={run.id: read.session_counts}),
            ),
            patch("app.routers.runs.run_service.build_run_read", new=Mock(return_value=read)),
        ):
            assert (await call(run_id, db=object())).id == run_id


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
        patch(
            "app.routers.devices_core.device_service.list_devices_paginated", new=AsyncMock(return_value=([device], 1))
        ),
        patch("app.routers.devices_core.run_service.get_device_reservation_map", new=AsyncMock(return_value={})),
        patch("app.routers.devices_core.device_health.build_public_summary", new=Mock(return_value={"healthy": True})),
        patch(
            "app.routers.devices_core.platform_label_service.load_platform_label_map",
            new=AsyncMock(return_value={("pack", "android"): "Android"}),
        ),
        patch("app.routers.devices_core.run_service.get_reservation_context_for_device", new=Mock(return_value=None)),
        patch("app.routers.devices_core.device_presenter.serialize_device", new=AsyncMock(return_value=serialized)),
    ):
        listed = await devices_core.list_devices(filters=filters, limit=10, offset=None, db=object())
    assert listed == {"items": [serialized], "total": 1, "limit": 10, "offset": 0}

    with patch("app.routers.devices_core.device_service.update_device", new=AsyncMock(return_value=None)):
        with pytest.raises(HTTPException) as exc:
            await devices_core.update_device(device_id, data=devices_core.DevicePatch(), db=object())
    assert exc.value.status_code == 404

    with patch("app.routers.devices_core.device_service.delete_device", new=AsyncMock(return_value=False)):
        with pytest.raises(HTTPException) as exc:
            await devices_core.delete_device(device_id, db=object())
    assert exc.value.status_code == 404

    with patch(
        "app.routers.driver_pack_uploads.pack_release_service.set_current_release",
        new=AsyncMock(side_effect=LookupError("missing")),
    ):
        with pytest.raises(HTTPException) as exc:
            await driver_pack_uploads.update_current_release(
                "pack",
                CurrentReleasePatch(release="1.0.0"),
                _username="admin",
                session=DummySession(),
            )
    assert exc.value.status_code == 404

    with patch("app.routers.driver_pack_export.export_pack", new=AsyncMock(return_value=(b"data", "sha"))):
        response = await driver_pack_export.export_release(
            "local/pack",
            "1.0.0+meta",
            _username="admin",
            session=object(),
            storage=object(),
        )
    assert response.headers["X-Pack-Sha256"] == "sha"
    assert "local_pack-1.0.0_meta.tar.gz" in response.headers["Content-Disposition"]

    with patch("app.routers.driver_pack_export.export_pack", new=AsyncMock(side_effect=LookupError("missing"))):
        with pytest.raises(HTTPException) as exc:
            await driver_pack_export.export_release(
                "pack", "1.0.0", _username="admin", session=object(), storage=object()
            )
    assert exc.value.status_code == 404

    descriptor = SimpleNamespace(
        id="android-real",
        display_name="Android Real",
        target_driver_summary="UiAutomator2",
        source_pack_id="appium-uiautomator2",
        prerequisite_host_tools=("adb",),
    )
    with patch("app.routers.driver_pack_templates.list_templates", new=Mock(return_value=[descriptor])):
        templates = await driver_pack_templates.get_templates(_username="admin")
    assert templates.templates[0].template_id == "android-real"

    with pytest.raises(HTTPException) as exc:
        await driver_pack_templates.create_from_template(
            "missing",
            driver_pack_templates.FromTemplateBody(pack_id="local/new", release="1.0.0"),
            _username="admin",
            session=DummySession(get_result=object()),
        )
    assert exc.value.status_code == 409

    with patch("app.routers.driver_pack_templates.load_template", new=Mock(side_effect=LookupError("missing"))):
        with pytest.raises(HTTPException) as exc:
            await driver_pack_templates.create_from_template(
                "missing",
                driver_pack_templates.FromTemplateBody(pack_id="local/new", release="1.0.0"),
                _username="admin",
                session=DummySession(),
            )
    assert exc.value.status_code == 404

    body = driver_pack_templates.FromTemplateBody(pack_id="local/new", release="1.0.0", display_name="New")
    for error, status_code in (
        (driver_pack_templates.PackIngestConflictError("duplicate"), 409),
        (driver_pack_templates.PackIngestValidationError("bad manifest"), 400),
    ):
        with (
            patch("app.routers.driver_pack_templates.load_template", new=Mock(return_value=object())),
            patch("app.routers.driver_pack_templates.build_tarball_from_template", new=Mock(return_value=b"tar")),
            patch("app.routers.driver_pack_templates.PackStorageService", new=Mock(return_value=object())),
            patch("app.routers.driver_pack_templates.ingest_pack_tarball", new=AsyncMock(side_effect=error)),
        ):
            with pytest.raises(HTTPException) as exc:
                await driver_pack_templates.create_from_template(
                    "android-real",
                    body,
                    _username="admin",
                    session=DummySession(),
                )
        assert exc.value.status_code == status_code

    template_session = DummySession()
    pack = SimpleNamespace(id="local/new")
    with (
        patch("app.routers.driver_pack_templates.load_template", new=Mock(return_value=object())),
        patch("app.routers.driver_pack_templates.build_tarball_from_template", new=Mock(return_value=b"tar")),
        patch("app.routers.driver_pack_templates.PackStorageService", new=Mock(return_value=object())),
        patch("app.routers.driver_pack_templates.ingest_pack_tarball", new=AsyncMock(return_value=pack)),
        patch("app.routers.driver_pack_templates.build_pack_out", new=Mock(return_value={"id": pack.id})),
    ):
        assert await driver_pack_templates.create_from_template(
            "android-real",
            body,
            _username="admin",
            session=template_session,
        ) == {"id": "local/new"}
    assert template_session.committed is True


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
    with patch.object(
        devices_verification_router.device_verification,
        "start_verification_job",
        new=AsyncMock(side_effect=PackUnavailableError("missing")),
    ):
        with pytest.raises(HTTPException) as exc:
            await devices_verification_router.create_device_verification_job(create_payload, db=db_session)
    assert exc.value.status_code == 422

    device_id = uuid.uuid4()
    with patch.object(devices_verification_router.device_service, "get_device", new=AsyncMock(return_value=None)):
        with pytest.raises(HTTPException) as exc:
            await devices_verification_router.create_existing_device_verification_job(
                device_id,
                DeviceVerificationUpdate(name="verify", host_id=uuid.uuid4()),
                db=db_session,
            )
    assert exc.value.status_code == 404


async def test_devices_verification_event_stream_terminal_initial_event(db_session: AsyncSession) -> None:
    job = {"job_id": "job-stream", "status": "completed", "current_stage": "save_device"}
    request = SimpleNamespace(is_disconnected=AsyncMock(return_value=False))
    with (
        patch.object(
            devices_verification_router.device_verification,
            "get_verification_job",
            new=AsyncMock(return_value=job),
        ),
        patch.object(devices_verification_router.event_bus, "subscribe", new=Mock(return_value=object())),
        patch.object(devices_verification_router.event_bus, "unsubscribe", new=Mock()) as unsubscribe,
    ):
        response = await devices_verification_router.stream_device_verification_job_events(
            "job-stream",
            request,  # type: ignore[arg-type]
            db=db_session,
        )
        first = await response.body_iterator.__anext__()
        await response.body_iterator.aclose()

    assert first["event"] == "device.verification.updated"
    unsubscribe.assert_called_once()

    device_id = uuid.uuid4()
    with (
        patch.object(devices_verification_router.device_service, "get_device", new=AsyncMock(return_value=object())),
        patch.object(
            devices_verification_router.device_verification,
            "start_existing_device_verification_job",
            new=AsyncMock(return_value={"id": "job", "status": "queued"}),
        ),
    ):
        assert (
            await devices_verification_router.create_existing_device_verification_job(
                device_id,
                DeviceVerificationUpdate(name="verify", host_id=uuid.uuid4()),
                db=db_session,
            )
        )["id"] == "job"

    with patch.object(
        devices_verification_router.device_verification,
        "get_verification_job",
        new=AsyncMock(return_value=None),
    ):
        with pytest.raises(HTTPException) as exc:
            await devices_verification_router.get_device_verification_job("missing", db=db_session)
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
    with (
        patch.object(devices_control, "get_device_or_404", new=AsyncMock(return_value=device)),
        patch.object(devices_control, "require_management_host", new=Mock(return_value=host)),
        patch.object(devices_control, "fetch_appium_status", new=AsyncMock(side_effect=AgentCallError("h", "down"))),
        patch.object(
            devices_control, "fetch_pack_device_health", new=AsyncMock(side_effect=AgentCallError("h", "down"))
        ),
        patch.object(
            devices_control.session_viability,
            "get_session_viability",
            new=AsyncMock(return_value={"status": "failed"}),
        ),
        patch.object(devices_control.lifecycle_policy, "build_lifecycle_policy", new=AsyncMock(return_value={})),
    ):
        health = await devices_control.device_health(device_id, db=object())
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
        auto_manage=True,
        appium_node=SimpleNamespace(observed_running=False),
    )
    # Phase 2: inner HTTPException(400) propagates unchanged (was incorrectly wrapped as 502)
    with (
        patch.object(devices_control, "get_device_or_404", new=AsyncMock(return_value=reconnect_device)),
        patch.object(
            devices_control, "resolve_pack_platform", new=AsyncMock(return_value=SimpleNamespace(lifecycle_actions=[]))
        ),
        patch.object(devices_control, "platform_has_lifecycle_action", new=Mock(return_value=True)),
        patch.object(devices_control, "pack_device_lifecycle_action", new=AsyncMock(return_value={"success": True})),
    ):
        with pytest.raises(HTTPException) as exc:
            await devices_control.reconnect_device(device_id, db=object())
    assert exc.value.status_code == 400

    with (
        patch.object(devices_control, "get_device_for_update_or_404", new=AsyncMock(return_value=device)),
        patch.object(
            devices_control, "resolve_pack_platform", new=AsyncMock(return_value=SimpleNamespace(lifecycle_actions=[]))
        ),
        patch.object(devices_control, "platform_has_lifecycle_action", new=Mock(return_value=False)),
    ):
        with pytest.raises(HTTPException) as exc:
            await devices_control.device_lifecycle_action(device_id, "reboot", db=object())
    assert exc.value.status_code == 400


async def test_runs_router_cursor_detail_and_cooldown_error_branches() -> None:
    request = SimpleNamespace(query_params={"cursor": "bad"})
    with patch.object(runs.run_service, "list_runs_cursor", new=AsyncMock(side_effect=CursorPaginationError("bad"))):
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
    with (
        patch.object(runs.run_service, "get_run", new=AsyncMock(return_value=run)),
        patch.object(runs.run_service, "fetch_session_counts", new=AsyncMock(return_value={})),
        patch.object(runs.run_service, "build_run_read", new=Mock(return_value=read)),
    ):
        detail = await runs.get_run(run_id, db=object())
    assert detail.id == run_id
    assert detail.devices == []

    for message, status_code in (
        ("run not found", 404),
        ("ttl_seconds must be <= 60", 422),
        ("not active", 409),
    ):
        with patch.object(runs.run_service, "cooldown_device", new=AsyncMock(side_effect=ValueError(message))):
            with pytest.raises(HTTPException) as exc:
                await runs.cooldown_device_endpoint(
                    run_id,
                    uuid.uuid4(),
                    RunCooldownRequest(reason="bad", ttl_seconds=10),
                    db=object(),
                )
        assert exc.value.status_code == status_code
