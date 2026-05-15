import asyncio
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from starlette.datastructures import QueryParams

from app.core.errors import PackUnavailableError
from app.core.pagination import CursorPage, CursorPaginationError
from app.devices.routers import core as devices_core
from app.devices.routers import groups as device_groups
from app.devices.routers import verification as devices_verification
from app.devices.schemas.device import BulkMaintenanceEnter, DevicePatch, DeviceVerificationCreate
from app.devices.schemas.group import DeviceGroupCreate, DeviceGroupUpdate, GroupMembershipUpdate
from app.devices.services.identity_conflicts import DeviceIdentityConflictError
from app.runs import router as runs
from app.runs.models import RunState
from app.runs.schemas import ReservedDeviceInfo, RunCooldownRequest, RunCreate, RunRead, SessionCounts


def _run(state: RunState = RunState.active) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        name="run",
        state=state,
        requirements=[],
        ttl_minutes=10,
        heartbeat_timeout_sec=30,
        reserved_devices=[],
        error=None,
        created_at=datetime.now(UTC),
        started_at=datetime.now(UTC),
        completed_at=None,
        created_by=None,
        last_heartbeat=datetime.now(UTC),
    )


def _run_read(run: SimpleNamespace, counts: SessionCounts | None = None) -> RunRead:
    return RunRead(
        id=run.id,
        name=run.name,
        state=run.state,
        requirements=[],
        ttl_minutes=run.ttl_minutes,
        heartbeat_timeout_sec=run.heartbeat_timeout_sec,
        reserved_devices=[],
        created_at=run.created_at,
        started_at=run.started_at,
        completed_at=run.completed_at,
        created_by=run.created_by,
        last_heartbeat=run.last_heartbeat,
        session_counts=counts or SessionCounts(),
    )


async def test_runs_router_error_and_list_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    db = MagicMock()
    monkeypatch.setattr(
        runs.run_service, "parse_includes", lambda include, allowed: {"capabilities"} if include else set()
    )
    with pytest.raises(HTTPException) as unsupported:
        await runs.create_run(RunCreate(name="r", requirements=[]), include="capabilities", db=db)
    assert unsupported.value.status_code == 422

    monkeypatch.setattr(runs.run_service, "parse_includes", lambda include, allowed: set())
    monkeypatch.setattr(runs.run_service, "create_run", AsyncMock(side_effect=PackUnavailableError("pack")))
    with pytest.raises(HTTPException) as pack_error:
        await runs.create_run(RunCreate(name="r", requirements=[]), db=db)
    assert pack_error.value.status_code == 422

    monkeypatch.setattr(
        runs.run_service, "list_runs_cursor", AsyncMock(side_effect=CursorPaginationError("bad cursor"))
    )
    with pytest.raises(HTTPException) as cursor_error:
        await runs.list_runs(
            SimpleNamespace(query_params={"cursor": "bad"}),
            state=None,
            created_from=None,
            created_to=None,
            limit=50,
            cursor="bad",
            direction="older",
            offset=0,
            sort_by="created_at",
            sort_dir="desc",
            db=db,
        )
    assert cursor_error.value.status_code == 422

    run = _run()
    monkeypatch.setattr(
        runs.run_service,
        "list_runs_cursor",
        AsyncMock(return_value=CursorPage(items=[run], limit=1, next_cursor="next", prev_cursor="prev")),
    )
    monkeypatch.setattr(
        runs.run_service, "fetch_session_counts", AsyncMock(return_value={run.id: SessionCounts(running=1, total=1)})
    )
    monkeypatch.setattr(runs.run_service, "build_run_read", _run_read)
    page = await runs.list_runs(
        SimpleNamespace(query_params={"direction": "newer"}),
        state=None,
        created_from=None,
        created_to=None,
        limit=1,
        cursor=None,
        direction="newer",
        offset=0,
        sort_by="created_at",
        sort_dir="desc",
        db=db,
    )
    assert page["next_cursor"] == "next"
    assert page["items"][0].session_counts.running == 1

    monkeypatch.setattr(runs.run_service, "list_runs", AsyncMock(return_value=([run], 1)))
    offset_page = await runs.list_runs(
        SimpleNamespace(query_params={}),
        state=None,
        created_from=None,
        created_to=None,
        limit=5,
        cursor=None,
        direction="older",
        offset=2,
        sort_by="created_at",
        sort_dir="desc",
        db=db,
    )
    assert offset_page["total"] == 1
    assert offset_page["offset"] == 2

    with pytest.raises(HTTPException) as invalid_date:
        await runs.list_runs(
            SimpleNamespace(query_params={}),
            state=None,
            created_from="not-a-date",
            created_to=None,
            limit=50,
            cursor=None,
            direction="older",
            offset=0,
            sort_by="created_at",
            sort_dir="desc",
            db=db,
        )
    assert invalid_date.value.status_code == 422
    assert runs._parse_run_filter_datetime("2026-05-13", end_of_day=True).tzinfo is not None


async def test_runs_router_lifecycle_and_cooldown_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    db = MagicMock()
    run = _run()
    monkeypatch.setattr(runs.run_service, "fetch_session_counts", AsyncMock(return_value={}))
    monkeypatch.setattr(runs.run_service, "build_run_read", _run_read)

    for endpoint_name in ("signal_ready", "signal_active", "report_preparation_failure", "complete_run", "cancel_run"):
        monkeypatch.setattr(runs.run_service, endpoint_name, AsyncMock(side_effect=ValueError("bad state")))

    with pytest.raises(HTTPException) as ready_error:
        await runs.signal_ready(run.id, db=db)
    assert ready_error.value.status_code == 409
    with pytest.raises(HTTPException):
        await runs.signal_active(run.id, db=db)
    with pytest.raises(HTTPException):
        await runs.report_preparation_failed(
            run.id, uuid.uuid4(), runs.RunPreparationFailureReport(message="bad"), db=db
        )
    with pytest.raises(HTTPException):
        await runs.complete_run(run.id, db=db)
    with pytest.raises(HTTPException):
        await runs.cancel_run(run.id, db=db)

    monkeypatch.setattr(runs.run_service, "force_release", AsyncMock(side_effect=ValueError("missing")))
    with pytest.raises(HTTPException) as force_error:
        await runs.force_release(run.id, db=db)
    assert force_error.value.status_code == 404

    monkeypatch.setattr(runs.run_service, "heartbeat", AsyncMock(side_effect=ValueError("missing")))
    with pytest.raises(HTTPException) as heartbeat_error:
        await runs.heartbeat(run.id, db=db)
    assert heartbeat_error.value.status_code == 404

    monkeypatch.setattr(runs.run_service, "cooldown_device", AsyncMock(side_effect=ValueError("run not found")))
    with pytest.raises(HTTPException) as not_found:
        await runs.cooldown_device_endpoint(
            run.id, uuid.uuid4(), RunCooldownRequest(reason="bad", ttl_seconds=1), db=db
        )
    assert not_found.value.status_code == 404

    monkeypatch.setattr(
        runs.run_service, "cooldown_device", AsyncMock(side_effect=ValueError("ttl_seconds must be <= 30"))
    )
    with pytest.raises(HTTPException) as invalid_ttl:
        await runs.cooldown_device_endpoint(
            run.id, uuid.uuid4(), RunCooldownRequest(reason="bad", ttl_seconds=1), db=db
        )
    assert invalid_ttl.value.status_code == 422

    monkeypatch.setattr(runs.run_service, "cooldown_device", AsyncMock(return_value=(None, 2, True, 2)))
    escalated = await runs.cooldown_device_endpoint(
        run.id,
        uuid.uuid4(),
        RunCooldownRequest(reason="bad", ttl_seconds=1),
        db=db,
    )
    assert escalated.status == "maintenance_escalated"

    monkeypatch.setattr(runs.run_service, "cooldown_device", AsyncMock(return_value=(None, 1, False, 2)))
    with pytest.raises(HTTPException) as no_expiry:
        await runs.cooldown_device_endpoint(
            run.id, uuid.uuid4(), RunCooldownRequest(reason="bad", ttl_seconds=1), db=db
        )
    assert no_expiry.value.status_code == 500


async def test_runs_router_create_include_and_success_lifecycle_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    db = MagicMock()
    run = _run(RunState.preparing)
    info = ReservedDeviceInfo(
        device_id=str(uuid.uuid4()),
        identity_value="device-1",
        name="Device 1",
        connection_target="device-1",
        pack_id="pack",
        platform_id="platform",
        os_version="14",
    )
    monkeypatch.setattr(runs.run_service, "parse_includes", lambda include, allowed: {"config"})
    monkeypatch.setattr(runs.run_service, "create_run", AsyncMock(return_value=(run, [info])))
    monkeypatch.setattr(runs.settings_service, "get", lambda key: "http://grid")
    db.execute = AsyncMock(return_value=SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [])))

    created = await runs.create_run(RunCreate(name="r", requirements=[]), include="config", db=db)
    assert created["devices"][0].unavailable_includes[0].reason == "device_not_found"

    active = _run(RunState.active)
    monkeypatch.setattr(
        runs.run_service, "fetch_session_counts", AsyncMock(return_value={active.id: SessionCounts(total=1)})
    )
    monkeypatch.setattr(runs.run_service, "build_run_read", _run_read)
    for endpoint_name in (
        "signal_ready",
        "signal_active",
        "report_preparation_failure",
        "complete_run",
        "cancel_run",
        "force_release",
    ):
        monkeypatch.setattr(runs.run_service, endpoint_name, AsyncMock(return_value=active))

    assert (await runs.signal_ready(active.id, db=db)).session_counts.total == 1
    assert (await runs.signal_active(active.id, db=db)).state == RunState.active
    assert (
        await runs.report_preparation_failed(
            active.id,
            uuid.uuid4(),
            runs.RunPreparationFailureReport(message="bad"),
            db=db,
        )
    ).state == RunState.active
    assert (await runs.complete_run(active.id, db=db)).state == RunState.active
    assert (await runs.cancel_run(active.id, db=db)).state == RunState.active
    assert (await runs.force_release(active.id, db=db)).state == RunState.active


async def test_device_groups_router_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    db = MagicMock()
    group_id = uuid.uuid4()
    device_id = uuid.uuid4()
    monkeypatch.setattr(device_groups.device_group_service, "get_group_device_ids", AsyncMock(return_value=[]))
    with pytest.raises(HTTPException):
        await device_groups._group_device_ids_or_404(db, group_id)

    monkeypatch.setattr(
        device_groups.device_group_service, "create_group", AsyncMock(return_value=SimpleNamespace(id=group_id))
    )
    monkeypatch.setattr(
        device_groups.device_group_service, "get_group", AsyncMock(return_value={"id": str(group_id), "devices": []})
    )
    assert await device_groups.create_group(DeviceGroupCreate(name="g"), db=db) == {"id": str(group_id), "devices": []}
    assert await device_groups.get_group(group_id, db=db) == {"id": str(group_id), "devices": []}

    monkeypatch.setattr(device_groups.device_group_service, "get_group", AsyncMock(return_value=None))
    with pytest.raises(HTTPException):
        await device_groups.get_group(group_id, db=db)

    monkeypatch.setattr(device_groups.device_group_service, "update_group", AsyncMock(return_value=None))
    with pytest.raises(HTTPException):
        await device_groups.update_group(group_id, DeviceGroupUpdate(name="new"), db=db)

    monkeypatch.setattr(device_groups.device_group_service, "delete_group", AsyncMock(return_value=False))
    with pytest.raises(HTTPException):
        await device_groups.delete_group(group_id, db=db)

    monkeypatch.setattr(
        device_groups.device_group_service, "get_group", AsyncMock(return_value={"group_type": "dynamic"})
    )
    with pytest.raises(HTTPException):
        await device_groups.add_members(group_id, GroupMembershipUpdate(device_ids=[device_id]), db=db)
    with pytest.raises(HTTPException):
        await device_groups.remove_members(group_id, GroupMembershipUpdate(device_ids=[device_id]), db=db)

    monkeypatch.setattr(
        device_groups.device_group_service, "get_group", AsyncMock(return_value={"group_type": "static"})
    )
    monkeypatch.setattr(device_groups.device_group_service, "add_members", AsyncMock(return_value=1))
    monkeypatch.setattr(device_groups.device_group_service, "remove_members", AsyncMock(return_value=1))
    assert await device_groups.add_members(group_id, GroupMembershipUpdate(device_ids=[device_id]), db=db) == {
        "added": 1
    }
    assert await device_groups.remove_members(group_id, GroupMembershipUpdate(device_ids=[device_id]), db=db) == {
        "removed": 1
    }

    monkeypatch.setattr(device_groups.device_group_service, "get_group_device_ids", AsyncMock(return_value=[device_id]))
    monkeypatch.setattr(device_groups.bulk_service, "bulk_start_nodes", AsyncMock(return_value={"ok": "start"}))
    monkeypatch.setattr(device_groups.bulk_service, "bulk_stop_nodes", AsyncMock(return_value={"ok": "stop"}))
    monkeypatch.setattr(device_groups.bulk_service, "bulk_restart_nodes", AsyncMock(return_value={"ok": "restart"}))
    monkeypatch.setattr(device_groups.bulk_service, "bulk_enter_maintenance", AsyncMock(return_value={"ok": "enter"}))
    assert await device_groups.group_bulk_start(group_id, db=db) == {"ok": "start"}
    assert await device_groups.group_bulk_stop(group_id, db=db) == {"ok": "stop"}
    assert await device_groups.group_bulk_restart(group_id, db=db) == {"ok": "restart"}
    assert await device_groups.group_bulk_enter_maintenance(
        group_id,
        BulkMaintenanceEnter(device_ids=[device_id]),
        db=db,
    ) == {"ok": "enter"}


async def test_devices_core_router_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    db = MagicMock()
    device_id = uuid.uuid4()
    device = SimpleNamespace(id=device_id, pack_id="pack", platform_id="platform")
    request = SimpleNamespace(query_params=QueryParams("tags.pool=smoke&tags.=ignored"))
    filters = devices_core.build_device_query_filters(
        request,
        pack_id=None,
        platform_id=None,
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
    assert filters.tags == {"pool": "smoke"}

    monkeypatch.setattr(devices_core.device_service, "list_devices_paginated", AsyncMock(return_value=([device], 1)))
    monkeypatch.setattr(devices_core.run_service, "get_device_reservation_map", AsyncMock(return_value={}))
    monkeypatch.setattr(devices_core.device_health, "build_public_summary", lambda device: {"healthy": True})
    monkeypatch.setattr(
        devices_core.platform_label_service,
        "load_platform_label_map",
        AsyncMock(return_value={("pack", "platform"): "Android"}),
    )
    monkeypatch.setattr(
        devices_core.device_presenter, "serialize_device", AsyncMock(return_value={"id": str(device_id)})
    )
    listed = await devices_core.list_devices(filters, limit=10, db=db)
    assert listed["total"] == 1

    monkeypatch.setattr(devices_core.device_service, "list_devices_by_filters", AsyncMock(return_value=[device]))
    listed_plain = await devices_core.list_devices(filters, limit=None, offset=None, db=db)
    assert listed_plain == [{"id": str(device_id)}]

    db.execute = AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: None))
    with pytest.raises(HTTPException):
        await devices_core.get_device_by_connection_target("missing", db=db)

    monkeypatch.setattr(
        devices_core.device_service, "update_device", AsyncMock(side_effect=DeviceIdentityConflictError("conflict"))
    )
    with pytest.raises(HTTPException) as conflict:
        await devices_core.update_device(device_id, DevicePatch(name="new"), db=db)
    assert conflict.value.status_code == 409
    monkeypatch.setattr(devices_core.device_service, "update_device", AsyncMock(side_effect=ValueError("bad")))
    with pytest.raises(HTTPException) as invalid:
        await devices_core.update_device(device_id, DevicePatch(name="new"), db=db)
    assert invalid.value.status_code == 422
    monkeypatch.setattr(devices_core.device_service, "update_device", AsyncMock(return_value=None))
    with pytest.raises(HTTPException) as missing:
        await devices_core.update_device(device_id, DevicePatch(name="new"), db=db)
    assert missing.value.status_code == 404

    monkeypatch.setattr(devices_core.device_service, "delete_device", AsyncMock(return_value=False))
    with pytest.raises(HTTPException):
        await devices_core.delete_device(device_id, db=db)

    found_result = SimpleNamespace(
        scalar_one_or_none=lambda: SimpleNamespace(
            id=device_id,
            pack_id="pack",
            platform_id="platform",
        )
    )
    db.execute = AsyncMock(return_value=found_result)
    monkeypatch.setattr(devices_core.platform_label_service, "load_platform_label", AsyncMock(return_value="Android"))
    monkeypatch.setattr(
        devices_core.device_presenter, "serialize_device", AsyncMock(return_value={"id": str(device_id)})
    )
    assert await devices_core.get_device_by_connection_target("target", db=db) == {"id": str(device_id)}

    monkeypatch.setattr(devices_core, "get_device_or_404", AsyncMock(return_value=device))
    monkeypatch.setattr(
        devices_core.device_presenter, "serialize_device_detail", AsyncMock(return_value={"detail": str(device_id)})
    )
    assert await devices_core.get_device(device_id, db=db) == {"detail": str(device_id)}
    monkeypatch.setattr(
        devices_core.capability_service, "get_device_capabilities", AsyncMock(return_value={"caps": True})
    )
    assert await devices_core.device_capabilities(device_id, db=db) == {"caps": True}
    monkeypatch.setattr(devices_core.session_service, "get_device_sessions", AsyncMock(return_value=["session"]))
    assert await devices_core.device_sessions(device_id, limit=1, db=db) == ["session"]
    monkeypatch.setattr(
        devices_core.session_service,
        "get_device_session_outcome_heatmap_rows",
        AsyncMock(return_value=[(datetime.now(UTC), "passed")]),
    )
    heatmap = await devices_core.device_session_outcome_heatmap(device_id, days=1, db=db)
    assert heatmap[0].status == "passed"


async def test_device_verification_router_error_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    db = MagicMock(bind=object())
    device_id = uuid.uuid4()
    monkeypatch.setattr(
        devices_verification.device_verification,
        "start_verification_job",
        AsyncMock(side_effect=PackUnavailableError("pack")),
    )
    with pytest.raises(HTTPException) as pack_error:
        await devices_verification.create_device_verification_job(
            DeviceVerificationCreate(
                name="Device",
                connection_target="target",
                pack_id="pack",
                platform_id="platform",
                os_version="1",
                host_id=uuid.uuid4(),
            ),
            db=db,
        )
    assert pack_error.value.status_code == 422

    monkeypatch.setattr(devices_verification.device_service, "get_device", AsyncMock(return_value=None))
    with pytest.raises(HTTPException):
        await devices_verification.create_existing_device_verification_job(
            device_id,
            devices_verification.DeviceVerificationUpdate(host_id=uuid.uuid4()),
            db=db,
        )

    monkeypatch.setattr(devices_verification.device_verification, "get_verification_job", AsyncMock(return_value=None))
    with pytest.raises(HTTPException):
        await devices_verification.get_device_verification_job("job", db=db)

    queue: asyncio.Queue[devices_verification.Event] = asyncio.Queue()
    task = asyncio.create_task(devices_verification._read_queue_event(queue))
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_device_verification_event_stream_initial_completed_job(monkeypatch: pytest.MonkeyPatch) -> None:
    db = MagicMock(bind=object())
    queue: asyncio.Queue[devices_verification.Event] = asyncio.Queue()
    monkeypatch.setattr(
        devices_verification.device_verification,
        "get_verification_job",
        AsyncMock(return_value={"job_id": "job", "status": "completed"}),
    )
    monkeypatch.setattr(devices_verification.event_bus, "subscribe", MagicMock(return_value=queue))
    unsubscribe = MagicMock()
    monkeypatch.setattr(devices_verification.event_bus, "unsubscribe", unsubscribe)
    response = await devices_verification.stream_device_verification_job_events(
        "job",
        SimpleNamespace(is_disconnected=AsyncMock(return_value=False)),
        db=db,
    )

    first = await response.body_iterator.__anext__()
    assert first["event"] == "device.verification.updated"
    with pytest.raises(StopAsyncIteration):
        await response.body_iterator.__anext__()
    unsubscribe.assert_called_once_with(queue)
