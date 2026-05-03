import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.models.device import Device, DeviceAvailabilityStatus
from app.models.device_reservation import DeviceReservation
from app.models.driver_pack import DriverPack
from app.models.host import Host
from app.models.session import Session, SessionStatus
from app.schemas.run import DeviceRequirement, RunCreate, SessionCounts
from app.services import device_health_summary, run_service
from app.services.settings_service import settings_service
from tests.helpers import create_device_record
from tests.pack.factories import seed_test_packs


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
        availability_status="available",
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
    assert "grid_url" in data


async def test_find_matching_devices_filters_tags_before_readiness(
    db_session: AsyncSession,
    default_host_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    matching = await create_device_record(
        db_session,
        host_id=default_host_id,
        identity_value="tag-match",
        connection_target="tag-match",
        name="Tag Match",
        availability_status="available",
        tags={"pool": "smoke"},
    )
    nonmatching = await create_device_record(
        db_session,
        host_id=default_host_id,
        identity_value="tag-miss",
        connection_target="tag-miss",
        name="Tag Miss",
        availability_status="available",
        tags={"pool": "full"},
    )
    readiness_checked: list[uuid.UUID] = []

    async def fake_readiness(_db: AsyncSession, device: Device) -> bool:
        readiness_checked.append(device.id)
        return True

    monkeypatch.setattr(run_service, "_readiness_for_match", fake_readiness)

    devices = await run_service._find_matching_devices(
        db_session,
        DeviceRequirement(pack_id="appium-uiautomator2", platform_id="android_mobile", tags={"pool": "smoke"}),
    )

    assert [device.id for device in devices] == [matching.id]
    assert nonmatching.id not in readiness_checked


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
        availability_status="available",
    )
    await device_health_summary.update_device_checks(
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
    assert device.availability_status == DeviceAvailabilityStatus.offline


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


async def test_list_runs_paginates_and_sorts(
    client: AsyncClient, db_session: AsyncSession, default_host_id: str
) -> None:
    await _create_available_device(db_session, default_host_id, "run-sort-1", "D1")
    await _create_available_device(db_session, default_host_id, "run-sort-2", "D2")
    await _create_available_device(db_session, default_host_id, "run-sort-3", "D3")
    await _create_run(client, name="Zulu Run")
    await _create_run(client, name="Alpha Run")
    await _create_run(client, name="Middle Run")

    response = await client.get(
        "/api/runs",
        params={"limit": 2, "offset": 1, "sort_by": "name", "sort_dir": "asc"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 3
    assert body["limit"] == 2
    assert body["offset"] == 1
    assert [row["name"] for row in body["items"]] == ["Middle Run", "Zulu Run"]


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
    from app.models.test_run import RunState, TestRun

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

    # preparing -> ready
    resp = await client.post(f"/api/runs/{run_id}/ready")
    assert resp.status_code == 200
    assert resp.json()["state"] == "ready"

    # ready -> active
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


async def test_signal_active_wrong_state(client: AsyncClient, db_session: AsyncSession, default_host_id: str) -> None:
    await _create_available_device(db_session, default_host_id, "run-wrong-2", "D1")
    run = await _create_run(client)

    # Try active from preparing state (should be ready first)
    resp = await client.post(f"/api/runs/{run['id']}/active")
    assert resp.status_code == 409


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
    assert any(d["availability_status"] == "available" for d in devices)

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
) -> None:
    device = await _create_available_device(db_session, default_host_id, "run-fr-busy-1", "Busy Force Release")
    run = await _create_run(client)
    run_id = uuid.UUID(run["id"])
    device_id = uuid.UUID(device["id"])

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
    device_row.availability_status = DeviceAvailabilityStatus.busy
    await db_session.commit()

    resp = await client.post(f"/api/runs/{run['id']}/force-release")
    assert resp.status_code == 200

    await db_session.refresh(device_row)
    assert device_row.availability_status == DeviceAvailabilityStatus.available

    session_result = await db_session.execute(
        select(Session).where(Session.session_id == "force-release-running-session")
    )
    session = session_result.scalar_one()
    assert session.status == SessionStatus.error
    assert session.ended_at is not None
    assert session.error_message == "Force released by admin"


async def test_report_preparation_failure_excludes_device_and_marks_unhealthy(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
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
    data = resp.json()
    excluded = {entry["device_id"]: entry for entry in data["reserved_devices"]}
    assert excluded[device_a["id"]]["excluded"] is True
    assert excluded[device_a["id"]]["exclusion_reason"] == "ADB authorization failed on device during CI setup"
    assert excluded[device_b["id"]]["excluded"] is False

    device_resp = await client.get(f"/api/devices/{device_a['id']}")
    assert device_resp.status_code == 200
    device_data = device_resp.json()
    assert device_data["availability_status"] == DeviceAvailabilityStatus.maintenance.value
    assert device_data["reservation"]["excluded"] is True
    assert device_data["reservation"]["exclusion_reason"] == "ADB authorization failed on device during CI setup"
    assert device_data["health_summary"]["healthy"] is False
    assert device_data["health_summary"]["summary"] == "ADB authorization failed on device during CI setup"


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
    assert reserved_resp.json()["availability_status"] == DeviceAvailabilityStatus.reserved.value


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
    payload = RunCreate(
        name="Concurrent Run",
        requirements=[{"pack_id": "appium-uiautomator2", "platform_id": "android_mobile", "count": 1}],
    )

    async def _attempt(name: str) -> tuple[str, str]:
        async with session_factory() as session:
            run_payload = payload.model_copy(update={"name": name})
            try:
                run, _devices = await run_service.create_run(session, run_payload)
                return "success", str(run.id)
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
    default_host_id: str,
) -> None:
    device = await create_device_record(
        db_session,
        host_id=default_host_id,
        identity_value="fsc-001",
        name="Device FSC1",
        availability_status="available",
    )
    run = await run_service.create_run(
        db_session,
        RunCreate(
            name="counts-run",
            requirements=[{"pack_id": "appium-uiautomator2", "platform_id": "android_mobile", "count": 1}],
        ),
    )
    run_id = run[0].id

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

    counts_map = await run_service.fetch_session_counts(db_session, [run_id])
    assert counts_map == {run_id: SessionCounts(passed=2, failed=1, error=1, running=1, total=5)}


async def test_fetch_session_counts_handles_empty_input(db_session: AsyncSession) -> None:
    assert await run_service.fetch_session_counts(db_session, []) == {}


async def test_list_runs_returns_session_counts_per_run(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    device = await create_device_record(
        db_session,
        host_id=default_host_id,
        identity_value="lsc-001",
        name="Device LSC1",
        availability_status="available",
    )
    run = await run_service.create_run(
        db_session,
        RunCreate(
            name="list-counts",
            requirements=[{"pack_id": "appium-uiautomator2", "platform_id": "android_mobile", "count": 1}],
        ),
    )
    run_id = run[0].id

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
    default_host_id: str,
) -> None:
    device = await create_device_record(
        db_session,
        host_id=default_host_id,
        identity_value="dsc-001",
        name="Device DSC1",
        availability_status="available",
    )
    run = await run_service.create_run(
        db_session,
        RunCreate(
            name="detail-counts",
            requirements=[{"pack_id": "appium-uiautomator2", "platform_id": "android_mobile", "count": 1}],
        ),
    )
    run_id = run[0].id

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
        availability_status="available",
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
    from app.services import run_service
    from tests.helpers import create_device

    devices = [
        await create_device(
            db_session,
            host_id=db_host.id,
            name=f"d{i}",
            availability_status=DeviceAvailabilityStatus.available,
            verified=True,
        )
        for i in range(3)
    ]
    await db_session.commit()

    original_readiness = run_service._readiness_for_match

    async def flaky(db: AsyncSession, device: Device) -> bool:
        if device.id == devices[0].id:
            return False
        return await original_readiness(db, device)

    monkeypatch.setattr(run_service, "_readiness_for_match", flaky)

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


async def test_claim_skips_active_cooldown_and_reclaims_after_expiry(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    device_a = await _create_available_device(db_session, default_host_id, "run-cooldown-a", "Cooldown A")
    device_b = await _create_available_device(db_session, default_host_id, "run-cooldown-b", "Cooldown B")
    run = await _create_run(
        client,
        requirements=[{"pack_id": "appium-uiautomator2", "platform_id": "android_mobile", "count": 2}],
    )
    run_id = uuid.UUID(run["id"])
    now = datetime.now(UTC)

    reservation_a = (
        await db_session.execute(
            select(DeviceReservation).where(
                DeviceReservation.run_id == run_id,
                DeviceReservation.device_id == uuid.UUID(device_a["id"]),
            )
        )
    ).scalar_one()
    reservation_a.excluded = True
    reservation_a.exclusion_reason = "appium launch timeout"
    reservation_a.excluded_at = now
    reservation_a.excluded_until = now + timedelta(seconds=60)
    await db_session.commit()

    first_claim = await client.post(f"/api/runs/{run['id']}/claim", json={"worker_id": "gw0"})
    assert first_claim.status_code == 200
    assert first_claim.json()["device_id"] == device_b["id"]

    reservation_a.excluded_until = now - timedelta(seconds=1)
    await db_session.commit()

    second_claim = await client.post(f"/api/runs/{run['id']}/claim", json={"worker_id": "gw1"})
    assert second_claim.status_code == 200
    assert second_claim.json()["device_id"] == device_a["id"]

    await db_session.refresh(reservation_a)
    assert reservation_a.excluded is False
    assert reservation_a.exclusion_reason is None
    assert reservation_a.excluded_at is None
    assert reservation_a.excluded_until is None


async def test_claim_409_includes_retry_after_and_next_available_at_for_cooldown(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    device = await _create_available_device(db_session, default_host_id, "run-cooldown-wait", "Cooldown Wait")
    run = await _create_run(client)
    run_id = uuid.UUID(run["id"])
    expires_at = datetime.now(UTC) + timedelta(seconds=45)
    settings_service._cache["general.claim_default_retry_after_sec"] = 7

    reservation = (
        await db_session.execute(
            select(DeviceReservation).where(
                DeviceReservation.run_id == run_id,
                DeviceReservation.device_id == uuid.UUID(device["id"]),
            )
        )
    ).scalar_one()
    reservation.excluded = True
    reservation.exclusion_reason = "transient appium failure"
    reservation.excluded_at = datetime.now(UTC)
    reservation.excluded_until = expires_at
    await db_session.commit()

    resp = await client.post(f"/api/runs/{run['id']}/claim", json={"worker_id": "gw0"})

    assert resp.status_code == 409
    assert resp.headers["Retry-After"] == "7"
    body = resp.json()
    assert body["error"]["code"] == "CONFLICT"
    assert body["error"]["details"]["error"] == "no_claimable_devices"
    assert body["error"]["details"]["retry_after_sec"] == 7
    assert body["error"]["details"]["next_available_at"] is not None


async def test_release_with_cooldown_clears_worker_claim_and_keeps_active_reservation(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    device = await _create_available_device(db_session, default_host_id, "run-cooldown-release", "Cooldown Release")
    run = await _create_run(client)
    claim = await client.post(f"/api/runs/{run['id']}/claim", json={"worker_id": "gw0"})
    assert claim.status_code == 200

    resp = await client.post(
        f"/api/runs/{run['id']}/devices/{device['id']}/release-with-cooldown",
        json={"worker_id": "gw0", "reason": "appium launch timeout", "ttl_seconds": 60},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "cooldown_set"
    assert body["reservation"]["device_id"] == device["id"]
    assert body["reservation"]["excluded"] is True
    assert body["reservation"]["exclusion_reason"] == "appium launch timeout"
    assert body["reservation"]["excluded_until"] is not None
    assert 0 <= body["reservation"]["cooldown_remaining_sec"] <= 60

    reservation = (
        await db_session.execute(
            select(DeviceReservation).where(
                DeviceReservation.run_id == uuid.UUID(run["id"]),
                DeviceReservation.device_id == uuid.UUID(device["id"]),
            )
        )
    ).scalar_one()
    assert reservation.claimed_by is None
    assert reservation.claimed_at is None
    assert reservation.released_at is None
    assert reservation.excluded is True
    assert reservation.excluded_until is not None


async def test_restore_device_to_run_does_not_clear_active_cooldown(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    device = await _create_available_device(db_session, default_host_id, "run-cooldown-restore", "Cooldown Restore")
    run = await _create_run(client)
    claim = await client.post(f"/api/runs/{run['id']}/claim", json={"worker_id": "gw0"})
    assert claim.status_code == 200
    cooldown = await client.post(
        f"/api/runs/{run['id']}/devices/{device['id']}/release-with-cooldown",
        json={"worker_id": "gw0", "reason": "driver retry", "ttl_seconds": 60},
    )
    assert cooldown.status_code == 200

    restored = await run_service.restore_device_to_run(db_session, uuid.UUID(device["id"]))
    assert restored is not None

    reservation = (
        await db_session.execute(
            select(DeviceReservation).where(
                DeviceReservation.run_id == uuid.UUID(run["id"]),
                DeviceReservation.device_id == uuid.UUID(device["id"]),
            )
        )
    ).scalar_one()
    assert reservation.excluded is True
    assert reservation.exclusion_reason == "driver retry"
    assert reservation.excluded_until is not None
    assert reservation.excluded_until > datetime.now(UTC)


async def test_release_with_cooldown_records_lifecycle_event(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    from app.models.device_event import DeviceEvent, DeviceEventType

    device = await _create_available_device(db_session, default_host_id, "run-cooldown-event", "Cooldown Event")
    run = await _create_run(client)
    claim = await client.post(f"/api/runs/{run['id']}/claim", json={"worker_id": "gw0"})
    assert claim.status_code == 200

    resp = await client.post(
        f"/api/runs/{run['id']}/devices/{device['id']}/release-with-cooldown",
        json={"worker_id": "gw0", "reason": "driver bootstrap timeout", "ttl_seconds": 30},
    )
    assert resp.status_code == 200

    events = (
        (
            await db_session.execute(
                select(DeviceEvent).where(
                    DeviceEvent.device_id == uuid.UUID(device["id"]),
                    DeviceEvent.event_type == DeviceEventType.lifecycle_run_cooldown_set,
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(events) == 1
    assert events[0].details is not None
    assert events[0].details["reason"] == "driver bootstrap timeout"
    assert events[0].details["ttl_seconds"] == 30
    assert events[0].details["worker_id"] == "gw0"
    assert events[0].details["run_id"] == run["id"]


async def test_completed_run_does_not_globally_block_cooled_down_device(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    device = await _create_available_device(db_session, default_host_id, "run-cooldown-global", "Cooldown Global")
    run = await _create_run(client)
    claim = await client.post(f"/api/runs/{run['id']}/claim", json={"worker_id": "gw0"})
    assert claim.status_code == 200
    cooldown = await client.post(
        f"/api/runs/{run['id']}/devices/{device['id']}/release-with-cooldown",
        json={"worker_id": "gw0", "reason": "short quarantine", "ttl_seconds": 60},
    )
    assert cooldown.status_code == 200

    complete = await client.post(f"/api/runs/{run['id']}/complete")
    assert complete.status_code == 200

    new_run = await client.post(
        "/api/runs",
        json={
            "name": "New Run After Cooldown Owner Completes",
            "requirements": [{"pack_id": "appium-uiautomator2", "platform_id": "android_mobile", "count": 1}],
        },
    )
    assert new_run.status_code == 201
    assert new_run.json()["devices"][0]["device_id"] == device["id"]
