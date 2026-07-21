import asyncio
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from app.core.errors import PackUnavailableError
from app.core.pagination import CursorPage, CursorPaginationError
from app.devices.models import GroupType
from app.devices.routers import core as devices_core
from app.devices.routers import groups as device_groups
from app.devices.schemas.device import BulkDeviceIds, DevicePatch, DeviceVerificationCreate
from app.devices.schemas.group import DeviceGroupCreate, DeviceGroupUpdate, GroupMembershipUpdate
from app.devices.services.identity_conflicts import DeviceIdentityConflictError
from app.runs import router as runs
from app.runs.models import RunState
from app.runs.schemas import ReservedDeviceInfo, RunCooldownRequest, RunCreate, RunRead, SessionCounts
from app.verification import router as devices_verification


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

    mock_rs = SimpleNamespace(
        allocator=AsyncMock(),
        lifecycle=AsyncMock(),
        failure=AsyncMock(),
        query=AsyncMock(),
    )

    mock_rs.allocator.create_run = AsyncMock(side_effect=PackUnavailableError("pack"))
    with pytest.raises(HTTPException) as pack_error:
        await runs.create_run(RunCreate(name="r", requirements=[]), db=db, run_services=mock_rs)
    assert pack_error.value.status_code == 422

    run = _run()
    mock_rs_list = SimpleNamespace(
        allocator=AsyncMock(),
        lifecycle=AsyncMock(),
        failure=AsyncMock(),
        query=AsyncMock(),
    )
    mock_rs_list.query.list_runs_cursor = AsyncMock(side_effect=CursorPaginationError("bad cursor"))
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
            db=db,
            run_services=mock_rs_list,
        )
    assert cursor_error.value.status_code == 422

    mock_rs_list.query.list_runs_cursor = AsyncMock(
        return_value=CursorPage(items=[run], limit=1, next_cursor="next", prev_cursor="prev")
    )
    mock_rs_list.query.fetch_session_counts = AsyncMock(return_value={run.id: SessionCounts(running=1, total=1)})
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
        db=db,
        run_services=mock_rs_list,
    )
    assert page["next_cursor"] == "next"
    assert page["items"][0].session_counts.running == 1

    mock_rs_list.query.list_runs = AsyncMock(return_value=([run], 1))
    mock_rs_list.query.fetch_session_counts = AsyncMock(return_value={run.id: SessionCounts()})
    offset_page = await runs.list_runs(
        SimpleNamespace(query_params={}),
        state=None,
        created_from=None,
        created_to=None,
        limit=5,
        cursor=None,
        direction="older",
        offset=2,
        db=db,
        run_services=mock_rs_list,
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
            db=db,
            run_services=mock_rs_list,
        )
    assert invalid_date.value.status_code == 422
    assert runs._parse_run_filter_datetime("2026-05-13", end_of_day=True).tzinfo is not None


async def test_runs_router_lifecycle_and_cooldown_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    db = MagicMock()
    run = _run()

    def _mk_rs() -> SimpleNamespace:
        rs = SimpleNamespace(
            allocator=AsyncMock(),
            lifecycle=AsyncMock(),
            failure=AsyncMock(),
            query=AsyncMock(),
        )
        rs.query.fetch_session_counts = AsyncMock(return_value={})
        return rs

    monkeypatch.setattr(runs.run_service, "build_run_read", _run_read)

    # signal_ready error
    mock_rs = _mk_rs()
    mock_rs.lifecycle.signal_ready = AsyncMock(side_effect=ValueError("bad state"))
    with pytest.raises(HTTPException) as ready_error:
        await runs.signal_ready(run.id, db=db, run_services=mock_rs)
    assert ready_error.value.status_code == 409

    # signal_active error
    mock_rs.lifecycle.signal_active = AsyncMock(side_effect=ValueError("bad state"))
    with pytest.raises(HTTPException):
        await runs.signal_active(run.id, db=db, run_services=mock_rs)

    # report_preparation_failed error
    mock_rs.failure.report_preparation_failure = AsyncMock(side_effect=ValueError("bad state"))
    with pytest.raises(HTTPException):
        await runs.report_preparation_failed(
            run.id, uuid.uuid4(), runs.RunPreparationFailureReport(message="bad"), db=db, run_services=mock_rs
        )

    # complete_run error
    mock_rs.lifecycle.complete_run = AsyncMock(side_effect=ValueError("bad state"))
    with pytest.raises(HTTPException):
        await runs.complete_run(run.id, db=db, run_services=mock_rs)

    # cancel_run error
    mock_rs.lifecycle.cancel_run = AsyncMock(side_effect=ValueError("bad state"))
    with pytest.raises(HTTPException):
        await runs.cancel_run(run.id, db=db, run_services=mock_rs)

    # force_release error
    mock_rs.lifecycle.force_release = AsyncMock(side_effect=ValueError("missing"))
    with pytest.raises(HTTPException) as force_error:
        await runs.force_release(run.id, db=db, run_services=mock_rs)
    assert force_error.value.status_code == 404

    # heartbeat error
    mock_rs.lifecycle.heartbeat = AsyncMock(side_effect=ValueError("missing"))
    with pytest.raises(HTTPException) as heartbeat_error:
        await runs.heartbeat(run.id, db=db, run_services=mock_rs)
    assert heartbeat_error.value.status_code == 404

    # cooldown errors
    mock_rs.failure.cooldown_device = AsyncMock(side_effect=ValueError("run not found"))
    with pytest.raises(HTTPException) as not_found:
        await runs.cooldown_device_endpoint(
            run.id, uuid.uuid4(), RunCooldownRequest(reason="bad", ttl_seconds=1), db=db, run_services=mock_rs
        )
    assert not_found.value.status_code == 404

    mock_rs.failure.cooldown_device = AsyncMock(side_effect=ValueError("ttl_seconds must be <= 30"))
    with pytest.raises(HTTPException) as invalid_ttl:
        await runs.cooldown_device_endpoint(
            run.id, uuid.uuid4(), RunCooldownRequest(reason="bad", ttl_seconds=1), db=db, run_services=mock_rs
        )
    assert invalid_ttl.value.status_code == 422

    mock_rs.failure.cooldown_device = AsyncMock(return_value=(None, 2, True, 2, True))
    escalated = await runs.cooldown_device_endpoint(
        run.id, uuid.uuid4(), RunCooldownRequest(reason="bad", ttl_seconds=1), db=db, run_services=mock_rs
    )
    assert escalated.status == "maintenance_escalated"

    mock_rs.failure.cooldown_device = AsyncMock(return_value=(None, 1, False, 2, False))
    with pytest.raises(HTTPException) as no_expiry:
        await runs.cooldown_device_endpoint(
            run.id, uuid.uuid4(), RunCooldownRequest(reason="bad", ttl_seconds=1), db=db, run_services=mock_rs
        )
    assert no_expiry.value.status_code == 500


async def test_runs_router_create_and_success_lifecycle_paths(monkeypatch: pytest.MonkeyPatch) -> None:
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

    mock_rs = SimpleNamespace(
        allocator=AsyncMock(),
        lifecycle=AsyncMock(),
        failure=AsyncMock(),
        query=AsyncMock(),
    )
    mock_rs.allocator.create_run = AsyncMock(return_value=(run, [info]))
    created = await runs.create_run(RunCreate(name="r", requirements=[]), db=db, run_services=mock_rs)
    assert created["devices"][0].device_id == info.device_id

    active = _run(RunState.active)
    mock_rs.query.fetch_session_counts = AsyncMock(return_value={active.id: SessionCounts(total=1)})
    monkeypatch.setattr(runs.run_service, "build_run_read", _run_read)

    mock_rs.lifecycle.signal_ready = AsyncMock(return_value=active)
    assert (await runs.signal_ready(active.id, db=db, run_services=mock_rs)).session_counts.total == 1

    mock_rs.lifecycle.signal_active = AsyncMock(return_value=active)
    assert (await runs.signal_active(active.id, db=db, run_services=mock_rs)).state == RunState.active

    mock_rs.failure.report_preparation_failure = AsyncMock(return_value=active)
    assert (
        await runs.report_preparation_failed(
            active.id,
            uuid.uuid4(),
            runs.RunPreparationFailureReport(message="bad"),
            db=db,
            run_services=mock_rs,
        )
    ).state == RunState.active

    mock_rs.lifecycle.complete_run = AsyncMock(return_value=active)
    assert (await runs.complete_run(active.id, db=db, run_services=mock_rs)).state == RunState.active

    mock_rs.lifecycle.cancel_run = AsyncMock(return_value=active)
    assert (await runs.cancel_run(active.id, db=db, run_services=mock_rs)).state == RunState.active

    mock_rs.lifecycle.force_release = AsyncMock(return_value=active)
    assert (await runs.force_release(active.id, db=db, run_services=mock_rs)).state == RunState.active


async def test_device_groups_router_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    db = MagicMock()
    group_key = "group"
    device_id = uuid.uuid4()

    ds_empty = SimpleNamespace(groups=SimpleNamespace(get_group_type=AsyncMock(return_value=None)))
    with pytest.raises(HTTPException):
        await device_groups._group_device_ids_or_404(db, group_key, ds_empty)

    # The create route returns what the service serialized inside its own
    # transaction; there is no post-commit re-read to mock.
    created_payload = {"key": group_key, "device_count": 0}
    ds_create = SimpleNamespace(
        groups=SimpleNamespace(
            create_group=AsyncMock(return_value=created_payload),
            get_group=AsyncMock(return_value=None),
        )
    )
    assert (
        await device_groups.create_group(DeviceGroupCreate(key=group_key, name="g"), db=db, device_services=ds_create)
        == created_payload
    )
    ds_create.groups.get_group.assert_not_awaited()
    ds_create_with_presenter = SimpleNamespace(
        groups=SimpleNamespace(get_group=AsyncMock(return_value={"key": group_key, "devices": []})),
        presenter=SimpleNamespace(serialize_device=AsyncMock(return_value={})),
    )
    assert await device_groups.get_group(group_key, db=db, device_services=ds_create_with_presenter) == {
        "key": group_key,
        "devices": [],
    }

    ds_none = SimpleNamespace(groups=SimpleNamespace(get_group=AsyncMock(return_value=None)))
    with pytest.raises(HTTPException):
        await device_groups.get_group(group_key, db=db, device_services=ds_none)

    ds_update_none = SimpleNamespace(groups=SimpleNamespace(update_group=AsyncMock(return_value=None)))
    with pytest.raises(HTTPException):
        await device_groups.update_group(
            group_key, DeviceGroupUpdate(name="new"), db=db, device_services=ds_update_none
        )

    ds_delete_false = SimpleNamespace(groups=SimpleNamespace(delete_group=AsyncMock(return_value=False)))
    with pytest.raises(HTTPException):
        await device_groups.delete_group(group_key, db=db, device_services=ds_delete_false)

    ds_dynamic = SimpleNamespace(groups=SimpleNamespace(get_group_type=AsyncMock(return_value=GroupType.dynamic)))
    with pytest.raises(HTTPException):
        await device_groups.add_members(
            group_key, GroupMembershipUpdate(device_ids=[device_id]), db=db, device_services=ds_dynamic
        )
    with pytest.raises(HTTPException):
        await device_groups.remove_members(
            group_key, GroupMembershipUpdate(device_ids=[device_id]), db=db, device_services=ds_dynamic
        )

    ds_static = SimpleNamespace(
        groups=SimpleNamespace(
            get_group_type=AsyncMock(return_value=GroupType.static),
            add_members=AsyncMock(return_value=1),
            remove_members=AsyncMock(return_value=1),
        )
    )
    assert await device_groups.add_members(
        group_key, GroupMembershipUpdate(device_ids=[device_id]), db=db, device_services=ds_static
    ) == {"added": 1}
    assert await device_groups.remove_members(
        group_key, GroupMembershipUpdate(device_ids=[device_id]), db=db, device_services=ds_static
    ) == {"removed": 1}

    ds_bulk = SimpleNamespace(
        groups=SimpleNamespace(
            get_group_type=AsyncMock(return_value=GroupType.static),
            get_group_device_ids=AsyncMock(return_value=[device_id]),
        ),
        bulk=SimpleNamespace(
            bulk_start_nodes=AsyncMock(return_value={"ok": "start"}),
            bulk_stop_nodes=AsyncMock(return_value={"ok": "stop"}),
            bulk_restart_nodes=AsyncMock(return_value={"ok": "restart"}),
            bulk_enter_maintenance=AsyncMock(return_value={"ok": "enter"}),
        ),
    )
    assert await device_groups.group_bulk_start(group_key, db=db, device_services=ds_bulk) == {"ok": "start"}
    assert await device_groups.group_bulk_stop(group_key, db=db, device_services=ds_bulk) == {"ok": "stop"}
    assert await device_groups.group_bulk_restart(group_key, db=db, device_services=ds_bulk) == {"ok": "restart"}
    assert await device_groups.group_bulk_enter_maintenance(
        group_key,
        BulkDeviceIds(device_ids=[device_id]),
        db=db,
        device_services=ds_bulk,
    ) == {"ok": "enter"}


async def test_devices_core_router_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    db = MagicMock()
    device_id = uuid.uuid4()
    device = SimpleNamespace(id=device_id, pack_id="pack", platform_id="platform", lifecycle_policy_state=None)
    filters = devices_core.build_device_query_filters(
        pack_id=None,
        platform_id=None,
        status=None,
        reserved=None,
        host_id=None,
        identity_value=None,
        connection_target=None,
        device_type=None,
        connection_type=None,
        os_version=None,
        os_version_display=None,
        search=None,
        needs_attention=None,
        device_health=None,
        node_health=None,
        viability=None,
        sort_by="created_at",
        sort_dir="desc",
    )
    assert filters.pack_id is None

    _mock_crud = AsyncMock()
    _mock_crud.list_devices_paginated = AsyncMock(return_value=([device], 1))
    _mock_crud.list_devices_by_filters = AsyncMock(return_value=[device])
    _mock_capability = SimpleNamespace(get_device_capabilities=AsyncMock(return_value={"caps": True}))
    _mock_ds = SimpleNamespace(
        crud=_mock_crud,
        capability=_mock_capability,
        presenter=SimpleNamespace(
            serialize_device=AsyncMock(return_value={"id": str(device_id)}),
            serialize_device_detail=AsyncMock(return_value={"detail": str(device_id)}),
            build_serialization_contexts=AsyncMock(return_value={device_id: None}),
        ),
    )
    monkeypatch.setattr(devices_core.run_service, "get_device_reservation_map", AsyncMock(return_value={}))
    monkeypatch.setattr(
        devices_core.remediation_log,
        "load_ladders",
        AsyncMock(return_value={device_id: devices_core.remediation_log.EMPTY_LADDER}),
    )
    monkeypatch.setattr(
        devices_core.remediation_log,
        "load_ladder",
        AsyncMock(return_value=devices_core.remediation_log.EMPTY_LADDER),
    )
    monkeypatch.setattr(
        devices_core.device_health,
        "build_public_summary",
        lambda device, *, policy_view: {"healthy": True},
    )
    monkeypatch.setattr(
        devices_core.platform_label_service,
        "load_platform_label_map",
        AsyncMock(return_value={("pack", "platform"): "Android"}),
    )
    listed = await devices_core.list_devices(filters, limit=10, db=db, device_services=_mock_ds)
    assert listed["total"] == 1

    listed_plain = await devices_core.list_devices(filters, limit=None, offset=None, db=db, device_services=_mock_ds)
    assert listed_plain == [{"id": str(device_id)}]

    _mock_crud.update_device = AsyncMock(side_effect=DeviceIdentityConflictError("conflict"))
    with pytest.raises(HTTPException) as conflict:
        await devices_core.update_device(device_id, DevicePatch(name="new"), db=db, device_services=_mock_ds)
    assert conflict.value.status_code == 409
    _mock_crud.update_device = AsyncMock(side_effect=ValueError("bad"))
    with pytest.raises(HTTPException) as invalid:
        await devices_core.update_device(device_id, DevicePatch(name="new"), db=db, device_services=_mock_ds)
    assert invalid.value.status_code == 422
    _mock_crud.update_device = AsyncMock(return_value=None)
    with pytest.raises(HTTPException) as missing:
        await devices_core.update_device(device_id, DevicePatch(name="new"), db=db, device_services=_mock_ds)
    assert missing.value.status_code == 404

    _mock_crud.delete_device = AsyncMock(return_value=False)
    with pytest.raises(HTTPException):
        await devices_core.delete_device(device_id, db=db, device_services=_mock_ds)

    monkeypatch.setattr(devices_core, "get_device_or_404", AsyncMock(return_value=device))
    assert await devices_core.get_device(device_id, db=db, device_services=_mock_ds) == {"detail": str(device_id)}
    _mock_ds.capability.get_device_capabilities = AsyncMock(return_value={"caps": True})
    assert await devices_core.device_capabilities(device_id, db=db, device_services=_mock_ds) == {"caps": True}
    session_crud = SimpleNamespace(
        get_device_session_outcome_heatmap_rows=AsyncMock(return_value=[(datetime.now(UTC), "passed")])
    )
    heatmap = await devices_core.device_session_outcome_heatmap(
        device_id,
        days=1,
        db=db,
        device_services=_mock_ds,
        session_services=SimpleNamespace(crud=session_crud),  # type: ignore[arg-type]
    )
    assert heatmap[0].status == "passed"


async def test_device_verification_router_error_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    db = MagicMock(bind=object())
    device_id = uuid.uuid4()
    mock_vs_pack_error = SimpleNamespace(
        service=SimpleNamespace(start_verification_job=AsyncMock(side_effect=PackUnavailableError("pack")))
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
            verification_services=mock_vs_pack_error,
        )
    assert pack_error.value.status_code == 422

    mock_ds_none_crud = SimpleNamespace(
        crud=SimpleNamespace(get_device=AsyncMock(return_value=None)),
    )
    with pytest.raises(HTTPException):
        await devices_verification.create_existing_device_verification_job(
            device_id,
            devices_verification.DeviceVerificationUpdate(host_id=uuid.uuid4()),
            db=db,
            device_services=mock_ds_none_crud,
            verification_services=SimpleNamespace(service=SimpleNamespace()),
        )

    mock_vs_no_job = SimpleNamespace(service=SimpleNamespace(get_verification_job=AsyncMock(return_value=None)))
    with pytest.raises(HTTPException):
        await devices_verification.get_device_verification_job("job", db=db, verification_services=mock_vs_no_job)

    queue: asyncio.Queue[devices_verification.Event] = asyncio.Queue()
    task = asyncio.create_task(devices_verification.wait_for_queue_event(queue))
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=1.0)


async def test_device_verification_event_stream_initial_completed_job(monkeypatch: pytest.MonkeyPatch) -> None:
    db = MagicMock(bind=object())
    queue: asyncio.Queue[devices_verification.Event] = asyncio.Queue()
    unsubscribe = MagicMock()
    mock_event_services = SimpleNamespace(
        subscriber=SimpleNamespace(subscribe=MagicMock(return_value=queue), unsubscribe=unsubscribe)
    )
    mock_vs = SimpleNamespace(
        service=SimpleNamespace(get_verification_job=AsyncMock(return_value={"job_id": "job", "status": "completed"}))
    )
    response = await devices_verification.stream_device_verification_job_events(
        "job",
        SimpleNamespace(is_disconnected=AsyncMock(return_value=False)),
        db=db,
        event_services=mock_event_services,
        verification_services=mock_vs,
    )

    first = await response.body_iterator.__anext__()
    assert first["event"] == "device.verification.updated"
    with pytest.raises(StopAsyncIteration):
        await response.body_iterator.__anext__()
    unsubscribe.assert_called_once_with(queue)
