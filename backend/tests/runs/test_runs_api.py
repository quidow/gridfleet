import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, Mock

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.appium_nodes.models import AppiumDesiredState, AppiumNode
from app.devices.models import Device, DeviceOperationalState, DeviceReservation
from app.devices.services.health import DeviceHealthService
from app.packs.models import DriverPack
from app.runs import service as run_service
from app.runs.schemas import DeviceRequirement, RunCreate, SessionCounts
from app.runs.service_allocator import RunAllocatorService
from app.runs.service_allocator import assess_devices_async as run_allocator_assess_devices_async
from app.runs.service_query import RunQueryService
from app.sessions.models import Session, SessionStatus
from tests.conftest import settings_service, test_circuit_breaker
from tests.fakes import FakeSettingsReader
from tests.helpers import create_device_record, select_devices_for_requirement
from tests.helpers import test_event_bus as event_bus
from tests.packs.factories import seed_test_packs

if TYPE_CHECKING:
    from httpx2 import AsyncClient

    from app.hosts.models import Host

_settings = FakeSettingsReader({})
_query_svc = RunQueryService()


def _make_allocator_svc(session_factory: async_sessionmaker[AsyncSession]) -> RunAllocatorService:
    return RunAllocatorService(
        publisher=event_bus,
        settings=_settings,
        circuit_breaker=test_circuit_breaker,
        session_factory=session_factory,
    )


@pytest_asyncio.fixture(autouse=True)
async def seed_packs(db_session: AsyncSession) -> None:
    """Seed driver packs so the policy gate passes in all tests."""
    await seed_test_packs(db_session)
    await db_session.commit()


async def _create_available_device(
    db_session: AsyncSession,
    host_id: str,
    identity_value: str,
    name: str,
    pack_id: str = "appium-uiautomator2",
    platform_id: str = "android_mobile",
    identity_scheme: str = "android_serial",
    identity_scope: str = "host",
) -> dict[str, Any]:
    device = await create_device_record(
        db_session,
        host_id=host_id,
        identity_value=identity_value,
        connection_target=identity_value,
        name=name,
        pack_id=pack_id,
        platform_id=platform_id,
        identity_scheme=identity_scheme,
        identity_scope=identity_scope,
        os_version="14",
        operational_state="available",
    )
    return {"id": str(device.id), "name": device.name}


async def _create_run(client: AsyncClient, **overrides: object) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "name": "Test Run",
        "requirements": [{"pack_id": "appium-uiautomator2", "platform_id": "android_mobile", "count": 1}],
        **overrides,
    }
    resp = await client.post("/api/runs", json=payload)
    assert resp.status_code == 201
    return dict(resp.json())


async def _seed_running_node(db_session: AsyncSession, device_id: uuid.UUID) -> None:
    db_session.add(
        AppiumNode(
            device_id=device_id,
            port=4723,
            desired_state=AppiumDesiredState.running,
            desired_port=4723,
            pid=1,
            active_connection_target="http://10.0.0.1:4723",
            health_running=True,
            health_state="ready",
        )
    )


async def test_create_run(client: AsyncClient, db_session: AsyncSession, default_host_id: str) -> None:
    await _create_available_device(db_session, default_host_id, "run-001", "Device 1")
    data = await _create_run(client)
    assert data["name"] == "Test Run"
    assert data["state"] == "preparing"
    assert len(data["devices"]) == 1
    assert data["devices"][0]["excluded"] is False
    assert data["devices"][0]["pack_id"] == "appium-uiautomator2"
    assert data["devices"][0]["platform_id"] == "android_mobile"
    assert data["devices"][0]["platform_label"] == "Android"


async def test_batch_select_devices_filters_os_version(
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    """The os_version gate excludes non-matching devices from the candidate set.

    The predecessor of this test also asserted that the os_version filter ran
    *before* the readiness check, to pin the absence of per-device readiness IO.
    ``_batch_select_devices`` assesses readiness once for the whole candidate
    batch, so that ordering is no longer a property of the code; the surviving
    invariant is the exclusion itself.
    """
    matching = await create_device_record(
        db_session,
        host_id=default_host_id,
        identity_value="os-match",
        connection_target="os-match",
        name="OS Match",
        operational_state="available",
        os_version="14",
    )
    nonmatching = await create_device_record(
        db_session,
        host_id=default_host_id,
        identity_value="os-miss",
        connection_target="os-miss",
        name="OS Miss",
        operational_state="available",
        os_version="13",
    )
    devices = await select_devices_for_requirement(
        db_session,
        DeviceRequirement(
            pack_id="appium-uiautomator2",
            platform_id="android_mobile",
            os_version="14",
            allocation="all_available",
        ),
    )

    assert [device.id for device in devices] == [matching.id]
    assert nonmatching.id not in {device.id for device in devices}


async def test_batch_select_devices_excludes_review_required(
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    eligible = await create_device_record(
        db_session,
        host_id=default_host_id,
        identity_value="eligible-device",
        connection_target="eligible-device",
        name="Eligible Device",
        operational_state="available",
        review_required=False,
    )
    shelved = await create_device_record(
        db_session,
        host_id=default_host_id,
        identity_value="review-required-device",
        connection_target="review-required-device",
        name="Review Required Device",
        operational_state="available",
        review_required=True,
    )

    devices = await select_devices_for_requirement(
        db_session,
        DeviceRequirement(pack_id="appium-uiautomator2", platform_id="android_mobile", allocation="all_available"),
    )

    ids = {d.id for d in devices}
    assert eligible.id in ids
    assert shelved.id not in ids


@pytest.mark.db
@pytest.mark.asyncio
async def test_batch_select_devices_excludes_reserved_device_with_null_hold(
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    """Allocator excludes devices with an active reservation row even when hold=NULL (post-collapse state)."""
    from app.runs.models import RunState, TestRun

    # A device with hold=NULL but an active reservation row — simulates post-collapse state.
    reserved = await create_device_record(
        db_session,
        host_id=default_host_id,
        identity_value="reserved-null-hold",
        connection_target="reserved-null-hold",
        name="Reserved Null Hold",
        operational_state="available",
    )
    # Add an active reservation row without setting hold.
    run = TestRun(
        name="existing-run",
        state=RunState.preparing,
        requirements=[{"platform_id": "android_mobile", "count": 1}],
        ttl_minutes=60,
        heartbeat_timeout_sec=120,
    )
    db_session.add(run)
    await db_session.flush()
    db_session.add(
        DeviceReservation(
            run=run,
            device_id=reserved.id,
            identity_value=reserved.identity_value,
            connection_target=reserved.connection_target,
            pack_id=reserved.pack_id,
            platform_id=reserved.platform_id,
            platform_label=None,
            os_version=reserved.os_version,
            host_ip=None,
            excluded=False,
            released_at=None,
        )
    )
    await db_session.flush()

    devices = await select_devices_for_requirement(
        db_session,
        DeviceRequirement(pack_id="appium-uiautomator2", platform_id="android_mobile", allocation="all_available"),
    )

    assert reserved.id not in {d.id for d in devices}, (
        "device with active reservation row must be excluded even when hold=NULL"
    )


async def test_create_run_insufficient_devices(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/runs",
        json={
            "name": "Failing Run",
            "requirements": [{"pack_id": "appium-uiautomator2", "platform_id": "android_mobile", "count": 5}],
        },
    )
    assert resp.status_code == 409
    assert "/api/availability" in resp.json()["error"]["message"]


async def test_create_run_all_available_reserves_every_eligible_device(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    await _create_available_device(db_session, default_host_id, "run-all-1", "Device All 1")
    await _create_available_device(db_session, default_host_id, "run-all-2", "Device All 2")
    await _create_available_device(db_session, default_host_id, "run-all-3", "Device All 3")

    resp = await client.post(
        "/api/runs",
        json={
            "name": "All Available Run",
            "requirements": [
                {
                    "pack_id": "appium-uiautomator2",
                    "platform_id": "android_mobile",
                    "allocation": "all_available",
                    "min_count": 1,
                }
            ],
        },
    )

    assert resp.status_code == 201
    body = resp.json()
    assert len(body["devices"]) == 3

    run = await run_service.get_run(db_session, uuid.UUID(body["id"]))
    assert run is not None
    assert run.requirements == [
        {
            "pack_id": "appium-uiautomator2",
            "platform_id": "android_mobile",
            "allocation": "all_available",
            "min_count": 1,
            "groups": [],
        }
    ]


async def test_create_run_all_available_honors_min_count(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    await _create_available_device(db_session, default_host_id, "run-all-min-1", "Device All Min 1")

    resp = await client.post(
        "/api/runs",
        json={
            "name": "All Available Needs Two",
            "requirements": [
                {
                    "pack_id": "appium-uiautomator2",
                    "platform_id": "android_mobile",
                    "allocation": "all_available",
                    "min_count": 2,
                }
            ],
        },
    )

    assert resp.status_code == 409
    assert "min_count=2" in resp.json()["error"]["message"]
    assert "matched 1 eligible devices right now" in resp.json()["error"]["message"]


async def test_create_run_rejects_count_with_all_available(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/runs",
        json={
            "name": "Ambiguous Run",
            "requirements": [
                {
                    "pack_id": "appium-uiautomator2",
                    "platform_id": "android_mobile",
                    "allocation": "all_available",
                    "count": 2,
                }
            ],
        },
    )

    assert resp.status_code == 422


async def test_create_run_does_not_reserve_unhealthy_available_device(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    device = await create_device_record(
        db_session,
        host_id=default_host_id,
        identity_value="run-unhealthy-1",
        connection_target="run-unhealthy-1",
        name="Unhealthy Candidate",
        os_version="14",
        operational_state="available",
    )
    await DeviceHealthService(publisher=Mock()).update_device_checks(
        db_session,
        device,
        healthy=False,
        summary="Node: error",
    )
    await db_session.commit()

    resp = await client.post(
        "/api/runs",
        json={
            "name": "Should Not Reserve",
            "requirements": [{"pack_id": "appium-uiautomator2", "platform_id": "android_mobile", "count": 1}],
        },
    )

    assert resp.status_code == 409
    await db_session.refresh(device)
    assert device.operational_state_last_emitted == DeviceOperationalState.offline


async def test_create_run_rejects_removed_wait_field(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/runs",
        json={
            "name": "Deprecated Wait Run",
            "requirements": [{"pack_id": "appium-uiautomator2", "platform_id": "android_mobile", "count": 1}],
            "wait_for_devices_sec": 0,
        },
    )

    assert resp.status_code == 422


async def test_list_runs(client: AsyncClient, db_session: AsyncSession, default_host_id: str) -> None:
    await _create_available_device(db_session, default_host_id, "run-list-1", "D1")
    await _create_available_device(db_session, default_host_id, "run-list-2", "D2")
    await _create_run(client, name="Run A")
    await _create_run(client, name="Run B")

    resp = await client.get("/api/runs")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 2
    assert len(body["items"]) == 2


async def test_list_runs_filter_state(client: AsyncClient, db_session: AsyncSession, default_host_id: str) -> None:
    await _create_available_device(db_session, default_host_id, "run-filter-1", "D1")
    await _create_run(client, name="Preparing Run")

    resp = await client.get("/api/runs?state=preparing")
    assert resp.status_code == 200
    assert resp.json()["total"] == 1

    resp = await client.get("/api/runs?state=active")
    assert resp.status_code == 200
    assert resp.json()["items"] == []


async def test_list_runs_filter_created_range(
    client: AsyncClient, db_session: AsyncSession, default_host_id: str
) -> None:
    await _create_available_device(db_session, default_host_id, "run-date-1", "D1")
    run = await _create_run(client, name="Dated Run")
    created_at = datetime.fromisoformat(run["created_at"].replace("Z", "+00:00")).astimezone(UTC)
    same_day = created_at.date().isoformat()
    next_day = (created_at + timedelta(days=1)).date().isoformat()

    resp = await client.get(f"/api/runs?created_from={same_day}&created_to={same_day}")
    assert resp.status_code == 200
    assert resp.json()["total"] == 1

    resp = await client.get(f"/api/runs?created_from={next_day}")
    assert resp.status_code == 200
    assert resp.json()["items"] == []


async def test_list_runs_paginates(client: AsyncClient, db_session: AsyncSession, default_host_id: str) -> None:
    await _create_available_device(db_session, default_host_id, "run-sort-1", "D1")
    await _create_available_device(db_session, default_host_id, "run-sort-2", "D2")
    await _create_available_device(db_session, default_host_id, "run-sort-3", "D3")
    await _create_run(client, name="Zulu Run")
    await _create_run(client, name="Alpha Run")
    await _create_run(client, name="Middle Run")

    response = await client.get(
        "/api/runs",
        params={"limit": 2, "offset": 1},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 3
    assert body["limit"] == 2
    assert body["offset"] == 1
    # default order is created_at DESC — offset=1 skips the most-recent (Middle Run)
    assert [row["name"] for row in body["items"]] == ["Alpha Run", "Zulu Run"]


async def test_list_runs_out_of_range_offset_returns_empty_items_with_total(
    client: AsyncClient, db_session: AsyncSession, default_host_id: str
) -> None:
    await _create_available_device(db_session, default_host_id, "run-offset-1", "D1")
    await _create_run(client, name="Only Run")

    response = await client.get("/api/runs", params={"offset": 10})

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["items"] == []


async def test_list_runs_cursor_navigation(client: AsyncClient, db_session: AsyncSession, default_host_id: str) -> None:
    from app.runs.models import RunState, TestRun

    start = datetime(2026, 4, 4, 10, 0, tzinfo=UTC)
    db_session.add_all(
        [
            TestRun(
                name=f"Run {index}",
                state=RunState.preparing,
                requirements=[{"pack_id": "appium-uiautomator2", "platform_id": "android_mobile", "count": 1}],
                ttl_minutes=60,
                heartbeat_timeout_sec=120,
                created_at=start - timedelta(minutes=index),
            )
            for index in range(5)
        ]
    )
    await db_session.commit()

    newest = await client.get("/api/runs", params={"limit": 2, "direction": "older"})
    assert newest.status_code == 200
    newest_body = newest.json()
    assert [row["name"] for row in newest_body["items"]] == ["Run 0", "Run 1"]
    assert newest_body["prev_cursor"] is None
    assert newest_body["next_cursor"] is not None
    assert newest_body["total"] is None
    assert newest_body["offset"] is None

    older = await client.get(
        "/api/runs",
        params={"limit": 2, "direction": "older", "cursor": newest_body["next_cursor"]},
    )
    assert older.status_code == 200
    older_body = older.json()
    assert [row["name"] for row in older_body["items"]] == ["Run 2", "Run 3"]
    assert older_body["prev_cursor"] is not None
    assert older_body["next_cursor"] is not None

    newer = await client.get(
        "/api/runs",
        params={"limit": 2, "direction": "newer", "cursor": older_body["prev_cursor"]},
    )
    assert newer.status_code == 200
    newer_body = newer.json()
    assert [row["name"] for row in newer_body["items"]] == ["Run 0", "Run 1"]
    assert newer_body["prev_cursor"] is None


async def test_list_runs_cursor_rejects_invalid_cursor(client: AsyncClient) -> None:
    response = await client.get("/api/runs", params={"direction": "older", "cursor": "not-a-valid-cursor"})

    assert response.status_code == 422


async def test_get_run(client: AsyncClient, db_session: AsyncSession, default_host_id: str) -> None:
    await _create_available_device(db_session, default_host_id, "run-get-1", "D1")
    run = await _create_run(client)
    resp = await client.get(f"/api/runs/{run['id']}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "Test Run"
    assert "devices" in data


async def test_device_payload_surfaces_reservation_owner(
    client: AsyncClient, db_session: AsyncSession, default_host_id: str
) -> None:
    device = await _create_available_device(db_session, default_host_id, "run-device-1", "Reserved Device")
    run = await _create_run(client, name="Owner Run")

    device_resp = await client.get(f"/api/devices/{device['id']}")
    assert device_resp.status_code == 200
    reservation = device_resp.json()["reservation"]
    assert reservation is not None
    assert reservation["run_id"] == run["id"]
    assert reservation["run_name"] == "Owner Run"
    assert reservation["excluded"] is False


async def test_get_run_not_found(client: AsyncClient) -> None:
    resp = await client.get("/api/runs/00000000-0000-0000-0000-000000000000")
    assert resp.status_code == 404


async def test_run_lifecycle(client: AsyncClient, db_session: AsyncSession, default_host_id: str) -> None:
    await _create_available_device(db_session, default_host_id, "run-life-1", "D1")
    run = await _create_run(client)
    run_id = run["id"]

    # preparing -> active
    resp = await client.post(f"/api/runs/{run_id}/ready")
    assert resp.status_code == 200
    assert resp.json()["state"] == "active"

    # active endpoint remains an idempotent alias
    resp = await client.post(f"/api/runs/{run_id}/active")
    assert resp.status_code == 200
    assert resp.json()["state"] == "active"

    # active -> completed
    resp = await client.post(f"/api/runs/{run_id}/complete")
    assert resp.status_code == 200
    assert resp.json()["state"] == "completed"


async def test_run_cancel(client: AsyncClient, db_session: AsyncSession, default_host_id: str) -> None:
    await _create_available_device(db_session, default_host_id, "run-cancel-1", "D1")
    run = await _create_run(client)

    resp = await client.post(f"/api/runs/{run['id']}/cancel")
    assert resp.status_code == 200
    assert resp.json()["state"] == "cancelled"


@pytest.mark.skip(reason="deferred to Phase 4 Task 5: durable force-release teardown")
async def test_cancel_run_deletes_active_grid_session_before_releasing_device(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.helpers import create_reserved_run

    device = await create_device_record(
        db_session,
        host_id=default_host_id,
        identity_value="cancel-live-session",
        connection_target="cancel-live-session",
        name="Cancel Live Session",
        operational_state="busy",
    )
    run_obj = await create_reserved_run(db_session, name="Cancel Live Session Run", devices=[device])
    await _seed_running_node(db_session, device.id)
    session = Session(
        session_id="grid-live-cancel",
        device_id=device.id,
        run_id=run_obj.id,
        test_name="test_cancel_cleanup",
        status=SessionStatus.running,
    )
    db_session.add(session)
    await db_session.commit()

    deleted: list[str] = []

    async def fake_terminate(target: str, session_id: str, *, timeout: float = 10.0) -> bool:
        deleted.append(session_id)
        return True

    monkeypatch.setattr("app.runs.service_lifecycle_release.appium_direct.terminate_session", fake_terminate)

    resp = await client.post(f"/api/runs/{run_obj.id}/cancel")

    assert resp.status_code == 200
    assert deleted == ["grid-live-cancel"]
    await db_session.refresh(session)
    await db_session.refresh(device)
    assert session.status == SessionStatus.error
    assert session.error_type == "run_released"
    assert session.ended_at is not None
    assert device.operational_state_last_emitted == DeviceOperationalState.available


@pytest.mark.skip(reason="deferred to Phase 4 Task 5: durable force-release teardown")
async def test_cancel_run_keeps_device_busy_when_grid_session_delete_fails(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.helpers import create_reserved_run

    device = await create_device_record(
        db_session,
        host_id=default_host_id,
        identity_value="cancel-delete-fails",
        connection_target="cancel-delete-fails",
        name="Cancel Delete Fails",
        operational_state="busy",
    )
    run_obj = await create_reserved_run(db_session, name="Cancel Delete Fails Run", devices=[device])
    await _seed_running_node(db_session, device.id)
    session = Session(
        session_id="grid-still-live",
        device_id=device.id,
        run_id=run_obj.id,
        test_name="test_delete_fails",
        status=SessionStatus.running,
    )
    db_session.add(session)
    await db_session.commit()

    async def fake_terminate(target: str, _session_id: str, *, timeout: float = 10.0) -> bool:
        return False

    monkeypatch.setattr("app.runs.service_lifecycle_release.appium_direct.terminate_session", fake_terminate)

    resp = await client.post(f"/api/runs/{run_obj.id}/cancel")

    assert resp.status_code == 200
    await db_session.refresh(session)
    await db_session.refresh(device)
    assert session.status == SessionStatus.running
    assert session.ended_at is None
    assert device.operational_state_last_emitted == DeviceOperationalState.busy

    reservation = (
        await db_session.execute(
            select(DeviceReservation).where(
                DeviceReservation.run_id == run_obj.id,
                DeviceReservation.device_id == device.id,
            )
        )
    ).scalar_one()
    assert reservation.released_at is not None


async def test_run_heartbeat(client: AsyncClient, db_session: AsyncSession, default_host_id: str) -> None:
    await _create_available_device(db_session, default_host_id, "run-hb-1", "D1")
    run = await _create_run(client)

    resp = await client.post(f"/api/runs/{run['id']}/heartbeat")
    assert resp.status_code == 200
    data = resp.json()
    assert data["state"] == "preparing"
    assert "last_heartbeat" in data


async def test_run_heartbeat_terminal_state(
    client: AsyncClient, db_session: AsyncSession, default_host_id: str
) -> None:
    await _create_available_device(db_session, default_host_id, "run-hbt-1", "D1")
    run = await _create_run(client)
    await client.post(f"/api/runs/{run['id']}/complete")

    resp = await client.post(f"/api/runs/{run['id']}/heartbeat")
    assert resp.status_code == 200
    assert resp.json()["state"] == "completed"


async def test_signal_ready_wrong_state(client: AsyncClient, db_session: AsyncSession, default_host_id: str) -> None:
    await _create_available_device(db_session, default_host_id, "run-wrong-1", "D1")
    run = await _create_run(client)
    await client.post(f"/api/runs/{run['id']}/ready")

    # Try ready again from ready state
    resp = await client.post(f"/api/runs/{run['id']}/ready")
    assert resp.status_code == 409


async def test_signal_active_from_preparing(
    client: AsyncClient, db_session: AsyncSession, default_host_id: str
) -> None:
    await _create_available_device(db_session, default_host_id, "run-wrong-2", "D1")
    run = await _create_run(client)

    resp = await client.post(f"/api/runs/{run['id']}/active")
    assert resp.status_code == 200
    assert resp.json()["state"] == "active"


async def test_force_release(client: AsyncClient, db_session: AsyncSession, default_host_id: str) -> None:
    await _create_available_device(db_session, default_host_id, "run-fr-1", "D1")
    run = await _create_run(client)

    resp = await client.post(f"/api/runs/{run['id']}/force-release")
    assert resp.status_code == 200
    data = resp.json()
    assert data["state"] == "cancelled"
    assert data["error"] == "Force released by admin"

    # Verify device is back to available
    device_resp = await client.get("/api/devices")
    devices = device_resp.json()
    assert any(d["operational_state"] == "available" for d in devices)

    reservation_result = await db_session.execute(
        select(DeviceReservation).where(DeviceReservation.run_id == uuid.UUID(run["id"]))
    )
    reservations = reservation_result.scalars().all()
    assert len(reservations) == 1
    assert reservations[0].released_at is not None


async def test_force_release_restores_busy_run_devices(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = await _create_available_device(db_session, default_host_id, "run-fr-busy-1", "Busy Force Release")
    run = await _create_run(client)
    run_id = uuid.UUID(run["id"])
    device_id = uuid.UUID(device["id"])

    await _seed_running_node(db_session, device_id)
    db_session.add(
        Session(
            session_id="force-release-running-session",
            device_id=device_id,
            run_id=run_id,
            status=SessionStatus.running,
        )
    )
    device_row = await db_session.get(Device, device_id)
    assert device_row is not None
    device_row.operational_state_last_emitted = DeviceOperationalState.busy
    await db_session.commit()

    async def fake_terminate(target: str, _session_id: str, *, timeout: float = 10.0) -> bool:
        return True

    monkeypatch.setattr("app.runs.service_lifecycle_release.appium_direct.terminate_session", fake_terminate)
    monkeypatch.setattr(
        "app.runs.service_lifecycle_release.appium_direct.session_alive",
        AsyncMock(return_value=True),
    )

    resp = await client.post(f"/api/runs/{run['id']}/force-release")
    assert resp.status_code == 200

    await db_session.refresh(device_row)
    # Force-release stops the device's Appium node; with the node row still observed
    # running (no agent in-test to confirm teardown) the device derives ``offline``
    # via the stop-in-flight gate until the agent reconciles. The reservation is
    # released and the session terminated regardless (asserted below).
    assert device_row.operational_state_last_emitted == DeviceOperationalState.offline

    session_result = await db_session.execute(
        select(Session).where(Session.session_id == "force-release-running-session")
    )
    session = session_result.scalar_one()
    assert session.status == SessionStatus.error
    assert session.ended_at is not None
    assert session.error_message == "Force released by admin"


async def test_report_preparation_failure_releases_device_and_enters_maintenance(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    # Exercises the escalate=True (default) path: device is released from the run and
    # placed into maintenance; general.run_failure_escalates_to_maintenance defaults to True.
    device_a = await _create_available_device(db_session, default_host_id, "run-prep-1", "Prep Device A")
    device_b = await _create_available_device(db_session, default_host_id, "run-prep-2", "Prep Device B")
    run = await _create_run(
        client,
        requirements=[{"pack_id": "appium-uiautomator2", "platform_id": "android_mobile", "count": 2}],
    )

    resp = await client.post(
        f"/api/runs/{run['id']}/devices/{device_a['id']}/preparation-failed",
        json={"message": "ADB authorization failed on device during CI setup"},
    )
    assert resp.status_code == 200

    # Device is released (not a sticky exclusion): released_at is set on the reservation row.
    reservation_result = await db_session.execute(
        select(DeviceReservation).where(DeviceReservation.device_id == uuid.UUID(device_a["id"]))
    )
    reservation = reservation_result.scalar_one()
    assert reservation.released_at is not None
    assert reservation.exclusion_reason == "ADB authorization failed on device during CI setup"

    # Device B is unaffected.
    device_b_resp = await db_session.execute(
        select(DeviceReservation).where(DeviceReservation.device_id == uuid.UUID(device_b["id"]))
    )
    device_b_reservation = device_b_resp.scalar_one()
    assert device_b_reservation.released_at is None

    device_resp = await client.get(f"/api/devices/{device_a['id']}")
    assert device_resp.status_code == 200
    device_data = device_resp.json()
    # Device has no active reservation after release (released_at is set).
    assert device_data["reservation"] is None
    # Device is in maintenance after escalation.
    assert device_data["operational_state"] == "maintenance"


async def test_report_preparation_failure_rejects_device_not_reserved_by_run(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    reserved = await _create_available_device(db_session, default_host_id, "run-prep-3", "Reserved Device")
    other = await _create_available_device(db_session, default_host_id, "run-prep-4", "Other Device")
    run = await _create_run(client)

    resp = await client.post(
        f"/api/runs/{run['id']}/devices/{other['id']}/preparation-failed",
        json={"message": "Driver bootstrap timed out"},
    )
    assert resp.status_code == 409
    assert "not actively reserved" in resp.json()["error"]["message"]

    reserved_resp = await client.get(f"/api/devices/{reserved['id']}")
    assert reserved_resp.status_code == 200
    # The device that was reserved for the OTHER run still carries an active
    # reservation row from its own allocation, surfaced via is_reserved.
    assert "hold" not in reserved_resp.json()
    assert reserved_resp.json()["is_reserved"] is True


async def test_complete_run_releases_reservation_rows(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    await _create_available_device(db_session, default_host_id, "run-release-1", "Device 1")
    run = await _create_run(client)

    resp = await client.post(f"/api/runs/{run['id']}/complete")
    assert resp.status_code == 200

    reservation_result = await db_session.execute(
        select(DeviceReservation).where(DeviceReservation.run_id == uuid.UUID(run["id"]))
    )
    reservations = reservation_result.scalars().all()
    assert len(reservations) == 1
    assert reservations[0].released_at is not None


async def test_concurrent_create_run_reserves_device_once(
    db_session: AsyncSession,
    setup_database: AsyncEngine,
    default_host_id: str,
) -> None:
    await _create_available_device(db_session, default_host_id, "run-concurrent-1", "Concurrent Device")

    session_factory = async_sessionmaker(setup_database, class_=AsyncSession, expire_on_commit=False)
    allocator_svc = _make_allocator_svc(session_factory)
    payload = RunCreate(
        name="Concurrent Run",
        requirements=[{"pack_id": "appium-uiautomator2", "platform_id": "android_mobile", "count": 1}],
    )

    async def _attempt(name: str) -> tuple[str, str]:
        run_payload = payload.model_copy(update={"name": name})
        try:
            result = await allocator_svc.create_run(run_payload)
            return "success", str(result.response.id)
        except ValueError as exc:
            return "error", str(exc)

    outcomes = await asyncio.gather(_attempt("Concurrent Run A"), _attempt("Concurrent Run B"))
    assert [status for status, _detail in outcomes].count("success") == 1
    assert [status for status, _detail in outcomes].count("error") == 1

    reservation_result = await db_session.execute(
        select(DeviceReservation).where(DeviceReservation.released_at.is_(None))
    )
    active_reservations = reservation_result.scalars().all()
    assert len(active_reservations) == 1


async def test_run_read_includes_session_counts_default_zero(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    await _create_available_device(db_session, default_host_id, "sc-001", "Device SC1")
    run = await _create_run(client)
    resp = await client.get(f"/api/runs/{run['id']}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["session_counts"] == {
        "passed": 0,
        "failed": 0,
        "error": 0,
        "running": 0,
        "total": 0,
    }


async def test_fetch_session_counts_groups_by_status(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    default_host_id: str,
) -> None:
    device = await create_device_record(
        db_session,
        host_id=default_host_id,
        identity_value="fsc-001",
        name="Device FSC1",
        operational_state="available",
    )
    result = await _make_allocator_svc(db_session_maker).create_run(
        RunCreate(
            name="counts-run",
            requirements=[{"pack_id": "appium-uiautomator2", "platform_id": "android_mobile", "count": 1}],
        ),
    )
    run_id = result.response.id

    db_session.add_all(
        [
            Session(session_id="s1", device_id=device.id, run_id=run_id, status=SessionStatus.passed),
            Session(session_id="s2", device_id=device.id, run_id=run_id, status=SessionStatus.passed),
            Session(session_id="s3", device_id=device.id, run_id=run_id, status=SessionStatus.failed),
            Session(session_id="s4", device_id=device.id, run_id=run_id, status=SessionStatus.error),
            Session(session_id="s5", device_id=device.id, run_id=run_id, status=SessionStatus.running),
        ]
    )
    await db_session.commit()

    counts_map = await _query_svc.fetch_session_counts(db_session, [run_id])
    assert counts_map == {run_id: SessionCounts(passed=2, failed=1, error=1, running=1, total=5)}


async def test_fetch_session_counts_handles_empty_input(db_session: AsyncSession) -> None:
    assert await _query_svc.fetch_session_counts(db_session, []) == {}


async def test_list_runs_returns_session_counts_per_run(
    client: AsyncClient,
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    default_host_id: str,
) -> None:
    device = await create_device_record(
        db_session,
        host_id=default_host_id,
        identity_value="lsc-001",
        name="Device LSC1",
        operational_state="available",
    )
    result = await _make_allocator_svc(db_session_maker).create_run(
        RunCreate(
            name="list-counts",
            requirements=[{"pack_id": "appium-uiautomator2", "platform_id": "android_mobile", "count": 1}],
        ),
    )
    run_id = result.response.id

    db_session.add_all(
        [
            Session(session_id="ls1", device_id=device.id, run_id=run_id, status=SessionStatus.passed),
            Session(session_id="ls2", device_id=device.id, run_id=run_id, status=SessionStatus.failed),
        ]
    )
    await db_session.commit()

    resp = await client.get("/api/runs", params={"limit": 50})
    assert resp.status_code == 200
    body = resp.json()
    target = next(item for item in body["items"] if item["id"] == str(run_id))
    assert target["session_counts"]["passed"] == 1
    assert target["session_counts"]["failed"] == 1
    assert target["session_counts"]["total"] == 2


async def test_get_run_detail_returns_session_counts(
    client: AsyncClient,
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    default_host_id: str,
) -> None:
    device = await create_device_record(
        db_session,
        host_id=default_host_id,
        identity_value="dsc-001",
        name="Device DSC1",
        operational_state="available",
    )
    result = await _make_allocator_svc(db_session_maker).create_run(
        RunCreate(
            name="detail-counts",
            requirements=[{"pack_id": "appium-uiautomator2", "platform_id": "android_mobile", "count": 1}],
        ),
    )
    run_id = result.response.id

    db_session.add_all(
        [
            Session(session_id="ds1", device_id=device.id, run_id=run_id, status=SessionStatus.running),
            Session(session_id="ds2", device_id=device.id, run_id=run_id, status=SessionStatus.error),
        ]
    )
    await db_session.commit()

    resp = await client.get(f"/api/runs/{run_id}")
    assert resp.status_code == 200
    counts = resp.json()["session_counts"]
    assert counts == {"passed": 0, "failed": 0, "error": 1, "running": 1, "total": 2}


async def test_cancel_run_response_includes_session_counts(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    device = await create_device_record(
        db_session,
        host_id=default_host_id,
        identity_value="csc-001",
        name="Device CSC1",
        operational_state="available",
    )
    created = await _create_run(client)
    run_id = uuid.UUID(created["id"])

    db_session.add_all(
        [
            Session(session_id="csc-s1", device_id=device.id, run_id=run_id, status=SessionStatus.passed),
            Session(session_id="csc-s2", device_id=device.id, run_id=run_id, status=SessionStatus.failed),
        ]
    )
    await db_session.commit()

    resp = await client.post(f"/api/runs/{run_id}/cancel")
    assert resp.status_code == 200
    counts = resp.json()["session_counts"]
    assert counts == {"passed": 1, "failed": 1, "error": 0, "running": 0, "total": 2}


@pytest.mark.asyncio
async def test_create_run_rejects_disabled_pack(client: AsyncClient, db_session: AsyncSession, db_host: Host) -> None:
    pack = await db_session.scalar(select(DriverPack).where(DriverPack.id == "appium-uiautomator2"))
    pack.state = "disabled"
    await db_session.commit()
    payload = {
        "name": "rejected",
        "requirements": [{"pack_id": "appium-uiautomator2", "platform_id": "android_mobile", "count": 1}],
    }
    resp = await client.post("/api/runs", json=payload)
    assert resp.status_code == 422
    assert resp.json()["error"]["details"]["code"] == "pack_disabled"


@pytest.mark.asyncio
async def test_create_run_rejects_unknown_pack(client: AsyncClient, db_session: AsyncSession, db_host: Host) -> None:
    payload = {
        "name": "rejected",
        "requirements": [{"pack_id": "appium-roku", "platform_id": "roku_network", "count": 1}],
    }
    resp = await client.post("/api/runs", json=payload)
    assert resp.status_code == 422
    assert resp.json()["error"]["details"]["code"] == "pack_unavailable"


@pytest.mark.asyncio
async def test_create_run_rejects_removed_platform(
    client: AsyncClient, db_session: AsyncSession, db_host: Host
) -> None:
    payload = {
        "name": "rejected",
        "requirements": [{"pack_id": "appium-uiautomator2", "platform_id": "never_existed", "count": 1}],
    }
    resp = await client.post("/api/runs", json=payload)
    assert resp.status_code == 422
    assert resp.json()["error"]["details"]["code"] == "platform_removed"


@pytest.mark.asyncio
@pytest.mark.usefixtures("seeded_driver_packs")
async def test_create_run_drops_devices_that_lost_availability_between_passes(
    client: AsyncClient,
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.devices.services.readiness import DeviceReadiness
    from tests.helpers import create_device

    devices = [
        await create_device(
            db_session,
            host_id=db_host.id,
            name=f"d{i}",
            operational_state=DeviceOperationalState.available,
            verified=True,
        )
        for i in range(3)
    ]
    await db_session.commit()

    real_assess = run_allocator_assess_devices_async

    async def flaky_assess(
        session: AsyncSession,
        device_iter: object,
        *,
        packs: dict[str, DriverPack] | None = None,
    ) -> dict[uuid.UUID, DeviceReadiness]:
        result = await real_assess(session, device_iter, packs=packs)
        result[devices[0].id] = DeviceReadiness(readiness_state="setup_required", missing_setup_fields=["x"])
        return result

    monkeypatch.setattr("app.runs.service_allocator.assess_devices_async", flaky_assess)

    resp = await client.post(
        "/api/runs",
        json={
            "name": "two-pass-test",
            "requirements": [
                {"pack_id": devices[0].pack_id, "platform_id": devices[0].platform_id, "count": 2},
            ],
        },
    )
    assert resp.status_code == 201
    body = resp.json()
    reserved_ids = {dev["device_id"] for dev in body["devices"]}
    assert str(devices[0].id) not in reserved_ids


async def test_create_run_excludes_device_mid_appium_restart(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    restarting = await create_device_record(
        db_session,
        host_id=default_host_id,
        identity_value="run-restarting-node",
        connection_target="run-restarting-node",
        name="Restarting Node",
        operational_state="available",
    )
    available = await create_device_record(
        db_session,
        host_id=default_host_id,
        identity_value="run-ready-node",
        connection_target="run-ready-node",
        name="Ready Node",
        operational_state="available",
    )
    db_session.add_all(
        [
            AppiumNode(
                device_id=restarting.id,
                port=4723,
                desired_port=4723,
                pid=0,
                active_connection_target="",
                desired_state=AppiumDesiredState.running,
                started_at=datetime.now(UTC) - timedelta(seconds=60),
                restart_requested_at=datetime.now(UTC),
            ),
            AppiumNode(
                device_id=available.id,
                port=4724,
                desired_port=4724,
                pid=0,
                active_connection_target="",
                desired_state=AppiumDesiredState.running,
            ),
        ]
    )
    await db_session.commit()

    data = await _create_run(client)

    assert [device["device_id"] for device in data["devices"]] == [str(available.id)]


async def test_allocator_does_not_write_hold(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    default_host_id: str,
) -> None:
    """Allocator never writes hold; reserved state lives on the DeviceReservation row.

    Hold derivation has been removed. Reserving a device for a run creates a reservation
    row and leaves the device's operational axis untouched — the device is reserved per
    ``device_is_reserved``, with no hold write.
    """
    from app.devices.services.claims import device_is_reserved

    device = await create_device_record(
        db_session,
        host_id=default_host_id,
        identity_value="alloc-hold-001",
        connection_target="alloc-hold-001",
        name="Allocator Hold Test",
        operational_state="available",
    )

    await _make_allocator_svc(db_session_maker).create_run(
        RunCreate(
            name="hold-derivation-run",
            requirements=[{"pack_id": "appium-uiautomator2", "platform_id": "android_mobile", "count": 1}],
        ),
    )

    await db_session.refresh(device)
    assert await device_is_reserved(db_session, device.id), "reservation row must drive reserved state"


async def test_cooldown_escalation_status_is_released_when_toggle_off(
    client: AsyncClient, db_session: AsyncSession, default_host_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the maintenance toggle is off, the cooldown escalation response must report
    status='released' rather than 'maintenance_escalated'."""
    monkeypatch.setitem(settings_service._cache, "general.device_cooldown_escalation_threshold", 1)
    monkeypatch.setitem(settings_service._cache, "general.run_failure_escalates_to_maintenance", False)

    device = await create_device_record(
        db_session,
        host_id=default_host_id,
        identity_value="cooldown-released-status-001",
        connection_target="cooldown-released-status-001",
        name="Cooldown Released Status",
        operational_state="available",
    )
    run_resp = await client.post(
        "/api/runs",
        json={
            "name": "Released Status Run",
            "requirements": [{"pack_id": "appium-uiautomator2", "platform_id": "android_mobile", "count": 1}],
        },
    )
    assert run_resp.status_code == 201
    run_id = run_resp.json()["id"]

    resp = await client.post(
        f"/api/runs/{run_id}/devices/{device.id}/cooldown",
        json={"reason": "flaky test", "ttl_seconds": 60},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "released"
    assert data["cooldown_count"] == 1
    assert data["threshold"] == 1

    active = (
        await db_session.execute(
            select(DeviceReservation).where(
                DeviceReservation.device_id == device.id,
                DeviceReservation.released_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    assert active is None
