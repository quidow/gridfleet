from datetime import UTC, datetime, timedelta
from typing import Any, cast
from unittest.mock import patch

import pytest
from httpx import AsyncClient
from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.events import event_bus
from app.services.session_viability import PROBE_TEST_NAME
from tests.helpers import create_device_record, create_reserved_run

DEVICE_PAYLOAD = {
    "identity_value": "sess-test-device",
    "name": "Session Test Phone",
    "pack_id": "appium-uiautomator2",
    "platform_id": "android_mobile",
    "identity_scheme": "android_serial",
    "identity_scope": "host",
    "os_version": "14",
}


async def _create_device(db_session: AsyncSession, host_id: str, **overrides: object) -> dict[str, Any]:
    payload = {**DEVICE_PAYLOAD, "connection_target": DEVICE_PAYLOAD["identity_value"], **overrides}
    device = await create_device_record(
        db_session,
        host_id=host_id,
        identity_value=payload["identity_value"],
        connection_target=payload["connection_target"],
        name=payload["name"],
        pack_id=payload["pack_id"],
        platform_id=payload["platform_id"],
        identity_scheme=payload["identity_scheme"],
        identity_scope=payload["identity_scope"],
        os_version=payload["os_version"],
    )
    return {"id": str(device.id), "name": device.name}


async def test_list_sessions_empty(client: AsyncClient) -> None:
    resp = await client.get("/api/sessions")
    assert resp.status_code == 200
    assert resp.json() == {
        "items": [],
        "total": 0,
        "limit": 50,
        "offset": 0,
        "next_cursor": None,
        "prev_cursor": None,
    }


@pytest.mark.usefixtures("seeded_driver_packs")
async def test_list_sessions_with_data(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    from app.models.session import Session, SessionStatus

    device = await _create_device(db_session, default_host_id)

    session = Session(
        session_id="grid-sess-001",
        device_id=device["id"],
        test_name="test_login",
        status=SessionStatus.running,
    )
    db_session.add(session)
    await db_session.commit()

    db_session.expunge(session)  # remove from identity map so selectinload works

    resp = await client.get("/api/sessions")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    assert len(data["items"]) == 1
    assert data["items"][0]["session_id"] == "grid-sess-001"
    assert data["items"][0]["device_name"] == "Session Test Phone"
    assert data["items"][0]["device_platform_id"] == "android_mobile"
    assert data["items"][0]["device_platform_label"] == "Android"


async def test_list_sessions_filter_by_status(
    client: AsyncClient, db_session: AsyncSession, default_host_id: str
) -> None:
    from app.models.session import Session, SessionStatus

    device = await _create_device(db_session, default_host_id)

    s1 = Session(session_id="gs-run", device_id=device["id"], status=SessionStatus.running)
    s2 = Session(session_id="gs-pass", device_id=device["id"], status=SessionStatus.passed)
    db_session.add_all([s1, s2])
    await db_session.commit()

    resp = await client.get("/api/sessions", params={"status": "running"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    assert len(data["items"]) == 1
    assert data["items"][0]["session_id"] == "gs-run"


async def test_list_sessions_filter_by_device(
    client: AsyncClient, db_session: AsyncSession, default_host_id: str
) -> None:
    from app.models.session import Session, SessionStatus

    d1 = await _create_device(db_session, default_host_id)
    d2 = await _create_device(
        db_session,
        default_host_id,
        identity_value="other-device",
        connection_target="other-device",
        name="Other",
    )

    s1 = Session(session_id="gs-d1", device_id=d1["id"], status=SessionStatus.running)
    s2 = Session(session_id="gs-d2", device_id=d2["id"], status=SessionStatus.running)
    db_session.add_all([s1, s2])
    await db_session.commit()

    resp = await client.get("/api/sessions", params={"device_id": d1["id"]})
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    assert len(data["items"]) == 1
    assert data["items"][0]["session_id"] == "gs-d1"


async def test_list_sessions_excludes_reserved_but_includes_probe_rows(
    client: AsyncClient, db_session: AsyncSession, default_host_id: str
) -> None:
    from app.models.session import Session, SessionStatus

    device = await _create_device(db_session, default_host_id)
    now = datetime.now(UTC)

    db_session.add_all(
        [
            Session(
                session_id="grid-real",
                device_id=device["id"],
                status=SessionStatus.running,
                started_at=now - timedelta(minutes=2),
            ),
            Session(
                session_id="reserved",
                device_id=device["id"],
                status=SessionStatus.passed,
                started_at=now - timedelta(minutes=1),
            ),
            Session(
                session_id="probe-sess-1",
                device_id=device["id"],
                test_name=PROBE_TEST_NAME,
                status=SessionStatus.passed,
                started_at=now,
            ),
        ]
    )
    await db_session.commit()

    resp = await client.get("/api/sessions")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 2
    assert [row["session_id"] for row in data["items"]] == ["probe-sess-1", "grid-real"]


async def test_list_sessions_paginates_and_sorts(
    client: AsyncClient, db_session: AsyncSession, default_host_id: str
) -> None:
    from app.models.session import Session, SessionStatus

    device = await _create_device(db_session, default_host_id)

    db_session.add_all(
        [
            Session(
                session_id="grid-z",
                device_id=device["id"],
                test_name="zeta",
                status=SessionStatus.running,
            ),
            Session(
                session_id="grid-a",
                device_id=device["id"],
                test_name="alpha",
                status=SessionStatus.passed,
            ),
            Session(
                session_id="grid-m",
                device_id=device["id"],
                test_name="mu",
                status=SessionStatus.failed,
            ),
        ]
    )
    await db_session.commit()

    response = await client.get(
        "/api/sessions",
        params={"limit": 2, "offset": 1, "sort_by": "test_name", "sort_dir": "asc"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 3
    assert body["limit"] == 2
    assert body["offset"] == 1
    assert [row["test_name"] for row in body["items"]] == ["mu", "zeta"]


async def test_list_sessions_out_of_range_offset_returns_empty_items_with_total(
    client: AsyncClient, db_session: AsyncSession, default_host_id: str
) -> None:
    from app.models.session import Session, SessionStatus

    device = await _create_device(db_session, default_host_id)
    db_session.add(Session(session_id="grid-1", device_id=device["id"], status=SessionStatus.running))
    await db_session.commit()

    response = await client.get("/api/sessions", params={"offset": 10})

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["items"] == []


async def test_list_sessions_cursor_navigation(
    client: AsyncClient, db_session: AsyncSession, default_host_id: str
) -> None:
    from app.models.session import Session, SessionStatus

    device = await _create_device(db_session, default_host_id)
    start = datetime(2026, 4, 1, 12, 0, tzinfo=UTC)
    db_session.add_all(
        [
            Session(
                session_id=f"grid-{index}",
                device_id=device["id"],
                test_name=f"test-{index}",
                status=SessionStatus.passed,
                started_at=start - timedelta(minutes=index),
                ended_at=start - timedelta(minutes=index - 1),
            )
            for index in range(5)
        ]
    )
    await db_session.commit()

    newest = await client.get("/api/sessions", params={"limit": 2, "direction": "older"})
    assert newest.status_code == 200
    newest_body = newest.json()
    assert [row["session_id"] for row in newest_body["items"]] == ["grid-0", "grid-1"]
    assert newest_body["prev_cursor"] is None
    assert newest_body["next_cursor"] is not None
    assert newest_body["total"] is None
    assert newest_body["offset"] is None

    older = await client.get(
        "/api/sessions",
        params={"limit": 2, "direction": "older", "cursor": newest_body["next_cursor"]},
    )
    assert older.status_code == 200
    older_body = older.json()
    assert [row["session_id"] for row in older_body["items"]] == ["grid-2", "grid-3"]
    assert older_body["prev_cursor"] is not None
    assert older_body["next_cursor"] is not None

    newer = await client.get(
        "/api/sessions",
        params={"limit": 2, "direction": "newer", "cursor": older_body["prev_cursor"]},
    )
    assert newer.status_code == 200
    newer_body = newer.json()
    assert [row["session_id"] for row in newer_body["items"]] == ["grid-0", "grid-1"]
    assert newer_body["prev_cursor"] is None


async def test_list_sessions_cursor_rejects_invalid_cursor(client: AsyncClient) -> None:
    response = await client.get("/api/sessions", params={"direction": "older", "cursor": "not-a-valid-cursor"})

    assert response.status_code == 422


@pytest.mark.usefixtures("seeded_driver_packs")
async def test_get_session(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    from app.models.session import Session, SessionStatus

    device = await _create_device(db_session, default_host_id)

    session = Session(
        session_id="gs-detail", device_id=device["id"], test_name="test_checkout", status=SessionStatus.passed
    )
    db_session.add(session)
    await db_session.commit()
    db_session.expunge(session)

    resp = await client.get("/api/sessions/gs-detail")
    assert resp.status_code == 200
    data = resp.json()
    assert data["session_id"] == "gs-detail"
    assert data["test_name"] == "test_checkout"
    assert data["device_name"] == "Session Test Phone"
    assert data["device_platform_label"] == "Android"


async def test_get_session_not_found(client: AsyncClient) -> None:
    resp = await client.get("/api/sessions/nonexistent")
    assert resp.status_code == 404


async def test_register_session_by_device_id(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    device = await _create_device(db_session, default_host_id)

    resp = await client.post(
        "/api/sessions",
        json={
            "session_id": "registered-sess-1",
            "device_id": device["id"],
            "test_name": "test_registered",
        },
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["session_id"] == "registered-sess-1"
    assert data["status"] == "running"
    assert data["test_name"] == "test_registered"


async def test_register_session_by_active_connection_target(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    from app.models.appium_node import AppiumDesiredState, AppiumNode

    device = await _create_device(
        db_session,
        default_host_id,
        identity_value="avd:Pixel_6",
        connection_target="Pixel_6",
    )
    db_session.add(
        AppiumNode(
            device_id=device["id"],
            port=4723,
            grid_url="http://hub:4444",
            active_connection_target="emulator-5554",
            desired_state=AppiumDesiredState.running,
            desired_port=4723,
            pid=0,
        )
    )
    await db_session.commit()

    resp = await client.post(
        "/api/sessions",
        json={
            "session_id": "registered-sess-2",
            "connection_target": "emulator-5554",
            "test_name": "test_runtime_target",
        },
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["session_id"] == "registered-sess-2"
    assert data["status"] == "running"


async def test_register_session_without_device_creates_device_less_error_session(
    client: AsyncClient,
) -> None:
    """POST /api/sessions with no device_id/connection_target should succeed and
    create a device-less session (used to record setup-phase failures from the
    pytest plugin)."""
    resp = await client.post(
        "/api/sessions",
        json={
            "session_id": "error-aaaabbbbccccdddd",
            "test_name": "test_broken_setup",
        },
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["session_id"] == "error-aaaabbbbccccdddd"
    assert data["status"] == "running"
    assert data["test_name"] == "test_broken_setup"


async def test_register_running_session_with_unknown_connection_target_returns_404(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/sessions",
        json={
            "session_id": "missing-target-session",
            "test_name": "test_missing_target",
            "connection_target": "missing-target",
        },
    )

    assert resp.status_code == 404


async def test_register_terminal_error_session_persists_setup_failure_context(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/sessions",
        json={
            "session_id": "error-setup-context",
            "test_name": "test_broken_setup",
            "status": "error",
            "requested_pack_id": "appium-uiautomator2",
            "requested_platform_id": "android_mobile",
            "requested_device_type": "real_device",
            "requested_connection_type": "network",
            "requested_capabilities": {
                "platformName": "Android",
                "appium:automationName": "UiAutomator2",
                "appium:appPackage": "io.appium.android.apis",
            },
            "error_type": "RuntimeError",
            "error_message": "Session could not be created",
        },
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "error"
    assert data["ended_at"] is not None
    assert data["requested_pack_id"] == "appium-uiautomator2"
    assert data["requested_platform_id"] == "android_mobile"
    assert "requested_platform" not in data
    assert data["requested_device_type"] == "real_device"
    assert data["requested_connection_type"] == "network"
    assert data["requested_capabilities"]["appium:appPackage"] == "io.appium.android.apis"
    assert data["error_type"] == "RuntimeError"
    assert data["error_message"] == "Session could not be created"

    detail_resp = await client.get("/api/sessions/error-setup-context")
    assert detail_resp.status_code == 200
    detail = detail_resp.json()
    assert detail["device_id"] is None
    assert detail["device_name"] is None
    assert detail["requested_pack_id"] == "appium-uiautomator2"
    assert detail["requested_platform_id"] == "android_mobile"
    assert "requested_platform" not in detail
    assert detail["error_message"] == "Session could not be created"

    await event_bus.drain_handlers()
    recent_events = event_bus.get_recent_events(limit=2)
    assert [event["type"] for event in recent_events] == ["session.started", "session.ended"]
    assert recent_events[0]["data"]["requested_pack_id"] == "appium-uiautomator2"
    assert recent_events[0]["data"]["requested_platform_id"] == "android_mobile"
    assert recent_events[1]["data"]["status"] == "error"
    assert recent_events[1]["data"]["error_type"] == "RuntimeError"


async def test_register_session_rejects_invalid_requested_enum_value(client: AsyncClient) -> None:
    response = await client.post(
        "/api/sessions",
        json={
            "session_id": "invalid-enum",
            "status": "error",
            "requested_pack_id": "appium-uiautomator2",
            "requested_device_type": "handset",
        },
    )

    assert response.status_code == 422


async def test_register_session_rejects_oversized_requested_capabilities(client: AsyncClient) -> None:
    response = await client.post(
        "/api/sessions",
        json={
            "session_id": "too-large",
            "requested_capabilities": {"blob": "x" * (33 * 1024)},
        },
    )

    assert response.status_code == 422
    assert "requested_capabilities must serialize to 32 KB or less" in response.text


async def test_list_sessions_includes_device_less_sessions(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    """Sessions without a device (error sessions) must appear in the list endpoint."""
    from app.models.session import Session, SessionStatus

    device = await _create_device(db_session, default_host_id)

    device_session = Session(session_id="with-device", device_id=device["id"], status=SessionStatus.passed)
    orphan_session = Session(session_id="error-no-device", device_id=None, status=SessionStatus.error)
    db_session.add_all([device_session, orphan_session])
    await db_session.commit()
    db_session.expunge_all()

    resp = await client.get("/api/sessions")
    assert resp.status_code == 200
    data = resp.json()
    session_ids = {item["session_id"] for item in data["items"]}
    assert "with-device" in session_ids
    assert "error-no-device" in session_ids

    orphan = next(item for item in data["items"] if item["session_id"] == "error-no-device")
    assert orphan["device_id"] is None
    assert orphan["device_name"] is None
    assert orphan["status"] == "error"


async def test_list_sessions_filters_device_less_rows_by_requested_platform_id(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    from app.models.session import Session, SessionStatus

    device = await _create_device(
        db_session,
        default_host_id,
        identity_value="ios-device",
        connection_target="ios-device",
        pack_id="appium-xcuitest",
        platform_id="ios",
        identity_scheme="apple_udid",
        identity_scope="global",
        name="iPhone 15",
    )
    db_session.add_all(
        [
            Session(
                session_id="device-less-android-error",
                device_id=None,
                status=SessionStatus.error,
                requested_pack_id="appium-uiautomator2",
                requested_platform_id="android_mobile",
            ),
            Session(
                session_id="ios-device-session",
                device_id=device["id"],
                status=SessionStatus.passed,
            ),
        ]
    )
    await db_session.commit()
    db_session.expunge_all()

    response = await client.get("/api/sessions", params={"platform_id": "android_mobile"})

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert [row["session_id"] for row in body["items"]] == ["device-less-android-error"]


async def test_device_sessions_endpoint(client: AsyncClient, db_session: AsyncSession, default_host_id: str) -> None:
    from app.models.session import Session, SessionStatus

    device = await _create_device(db_session, default_host_id)

    s1 = Session(session_id="ds-1", device_id=device["id"], status=SessionStatus.running)
    s2 = Session(session_id="ds-2", device_id=device["id"], status=SessionStatus.passed)
    db_session.add_all([s1, s2])
    await db_session.commit()

    resp = await client.get(f"/api/devices/{device['id']}/sessions")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 2


async def test_device_sessions_endpoint_excludes_reserved_and_probe_rows(
    client: AsyncClient, db_session: AsyncSession, default_host_id: str
) -> None:
    from app.models.session import Session, SessionStatus

    device = await _create_device(db_session, default_host_id)

    db_session.add_all(
        [
            Session(session_id="ds-real", device_id=device["id"], status=SessionStatus.running),
            Session(session_id="reserved", device_id=device["id"], status=SessionStatus.passed),
            Session(
                session_id="probe-sess-2",
                device_id=device["id"],
                test_name=PROBE_TEST_NAME,
                status=SessionStatus.passed,
            ),
        ]
    )
    await db_session.commit()

    resp = await client.get(f"/api/devices/{device['id']}/sessions")
    assert resp.status_code == 200
    data = resp.json()
    assert [row["session_id"] for row in data] == ["ds-real"]


async def test_device_session_outcome_heatmap_404_unknown_device(client: AsyncClient) -> None:
    resp = await client.get("/api/devices/00000000-0000-0000-0000-000000000001/session-outcome-heatmap")

    assert resp.status_code == 404


async def test_device_session_outcome_heatmap_validates_days(
    client: AsyncClient, db_session: AsyncSession, default_host_id: str
) -> None:
    device = await _create_device(db_session, default_host_id)

    for days in (0, 91):
        resp = await client.get(f"/api/devices/{device['id']}/session-outcome-heatmap", params={"days": days})
        assert resp.status_code == 422


async def test_device_session_outcome_heatmap_filters_and_orders_rows(
    client: AsyncClient, db_session: AsyncSession, default_host_id: str
) -> None:
    from app.models.session import Session, SessionStatus

    device = await _create_device(db_session, default_host_id)
    other_device = await _create_device(
        db_session,
        default_host_id,
        identity_value="heatmap-other-device",
        connection_target="heatmap-other-device",
        name="Other Heatmap Device",
    )
    now = datetime.now(UTC)
    included_error = now - timedelta(days=5)
    included_failed = now - timedelta(days=2)
    old_session = now - timedelta(days=95)

    db_session.add_all(
        [
            Session(
                session_id="heatmap-error",
                device_id=device["id"],
                status=SessionStatus.error,
                started_at=included_error,
            ),
            Session(
                session_id="heatmap-failed",
                device_id=device["id"],
                status=SessionStatus.failed,
                started_at=included_failed,
            ),
            Session(
                session_id="heatmap-running",
                device_id=device["id"],
                status=SessionStatus.running,
                started_at=now - timedelta(days=1),
            ),
            Session(
                session_id="reserved",
                device_id=device["id"],
                status=SessionStatus.passed,
                started_at=now - timedelta(days=4),
            ),
            Session(
                session_id="heatmap-probe",
                device_id=device["id"],
                test_name=PROBE_TEST_NAME,
                status=SessionStatus.passed,
                started_at=now - timedelta(days=3),
            ),
            Session(
                session_id="heatmap-old",
                device_id=device["id"],
                status=SessionStatus.passed,
                started_at=old_session,
            ),
            Session(
                session_id="heatmap-other-device",
                device_id=other_device["id"],
                status=SessionStatus.passed,
                started_at=now - timedelta(days=1),
            ),
        ]
    )
    await db_session.commit()

    resp = await client.get(f"/api/devices/{device['id']}/session-outcome-heatmap", params={"days": 90})

    assert resp.status_code == 200
    assert resp.json() == [
        {"timestamp": included_error.isoformat().replace("+00:00", "Z"), "status": "error"},
        {"timestamp": included_failed.isoformat().replace("+00:00", "Z"), "status": "failed"},
    ]


async def test_sessions_table_has_device_started_at_index(setup_database: AsyncEngine) -> None:
    async with setup_database.begin() as conn:
        indexes = await conn.run_sync(lambda sync_conn: inspect(sync_conn).get_indexes("sessions"))

    assert any(
        index["name"] == "ix_sessions_device_id_started_at" and index["column_names"] == ["device_id", "started_at"]
        for index in indexes
    )


async def test_update_session_status(client: AsyncClient, db_session: AsyncSession, default_host_id: str) -> None:
    from app.models.session import Session, SessionStatus

    device = await _create_device(db_session, default_host_id)

    session = Session(session_id="gs-update", device_id=device["id"], status=SessionStatus.running)
    db_session.add(session)
    await db_session.commit()

    resp = await client.patch("/api/sessions/gs-update/status", json={"status": "failed"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "failed"
    assert data["ended_at"] is not None

    await event_bus.drain_handlers()
    recent_events = event_bus.get_recent_events(limit=1)
    assert len(recent_events) == 1
    assert recent_events[0]["type"] == "session.ended"
    assert recent_events[0]["data"]["session_id"] == "gs-update"
    assert recent_events[0]["data"]["status"] == "failed"


async def test_grid_queue(client: AsyncClient) -> None:
    mock_data = {
        "value": {
            "ready": True,
            "nodes": [],
            "sessionQueueRequests": [
                {"capabilities": {"platformName": "android"}},
            ],
        }
    }

    with patch("app.grid.service.get_grid_status", return_value=mock_data):
        resp = await client.get("/api/grid/queue")

    assert resp.status_code == 200
    data = resp.json()
    assert data["queue_size"] == 1


# ---------------------------------------------------------------------------
# Phase 121: run_id persistence and run-scoped filtering
# ---------------------------------------------------------------------------


async def test_session_without_reservation_has_null_run_id(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    """Session created on a device with no active reservation keeps run_id=null."""
    device = await _create_device(db_session, default_host_id)

    resp = await client.post(
        "/api/sessions",
        json={"session_id": "no-run-sess", "device_id": device["id"], "test_name": "test_orphan"},
    )
    assert resp.status_code == 200
    assert resp.json()["run_id"] is None

    detail = await client.get("/api/sessions/no-run-sess")
    assert detail.status_code == 200
    assert detail.json()["run_id"] is None


async def test_session_with_active_reservation_persists_run_id(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    """Session registered while a live reservation is active picks up that run_id."""
    device = await create_device_record(
        db_session,
        host_id=default_host_id,
        identity_value="run-device",
        connection_target="run-device",
        name="Run Device",
    )
    run = await create_reserved_run(db_session, name="my-run", devices=[device])

    resp = await client.post(
        "/api/sessions",
        json={"session_id": "run-sess-1", "device_id": str(device.id), "test_name": "test_run_flow"},
    )
    assert resp.status_code == 200
    assert resp.json()["run_id"] == str(run.id)


async def test_list_sessions_filter_by_run_id_offset_mode(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    """GET /api/sessions?run_id=<id> returns only sessions belonging to that run."""
    from app.models.session import Session, SessionStatus

    device = await create_device_record(
        db_session,
        host_id=default_host_id,
        identity_value="filter-run-device",
        connection_target="filter-run-device",
        name="Filter Run Device",
    )
    run = await create_reserved_run(db_session, name="filter-run", devices=[device])

    s_in = Session(session_id="in-run-sess", device_id=device.id, status=SessionStatus.passed, run_id=run.id)
    s_out = Session(session_id="out-of-run", device_id=device.id, status=SessionStatus.passed)
    db_session.add_all([s_in, s_out])
    await db_session.commit()
    db_session.expunge_all()

    resp = await client.get("/api/sessions", params={"run_id": str(run.id)})
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    assert len(data["items"]) == 1
    assert data["items"][0]["session_id"] == "in-run-sess"
    assert data["items"][0]["run_id"] == str(run.id)


async def test_list_sessions_filter_by_run_id_cursor_mode(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    """GET /api/sessions?run_id=<id>&direction=older respects run_id in cursor mode."""
    from app.models.session import Session, SessionStatus

    device = await create_device_record(
        db_session,
        host_id=default_host_id,
        identity_value="cursor-run-device",
        connection_target="cursor-run-device",
        name="Cursor Run Device",
    )
    run = await create_reserved_run(db_session, name="cursor-run", devices=[device])

    s_in = Session(session_id="cursor-in-run", device_id=device.id, status=SessionStatus.passed, run_id=run.id)
    s_out = Session(session_id="cursor-out-run", device_id=device.id, status=SessionStatus.passed)
    db_session.add_all([s_in, s_out])
    await db_session.commit()
    db_session.expunge_all()

    resp = await client.get("/api/sessions", params={"run_id": str(run.id), "direction": "older"})
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["items"]) == 1
    assert data["items"][0]["session_id"] == "cursor-in-run"


async def test_list_sessions_unknown_run_id_returns_empty(client: AsyncClient) -> None:
    """An unknown but well-formed run_id yields an empty list, not an error."""
    import uuid as _uuid

    resp = await client.get("/api/sessions", params={"run_id": str(_uuid.uuid4())})
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 0
    assert data["items"] == []


async def test_list_sessions_invalid_run_id_format_returns_422(client: AsyncClient) -> None:
    """An invalid UUID for run_id is caught by FastAPI's query-param validation."""
    resp = await client.get("/api/sessions", params={"run_id": "not-a-uuid"})
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# D2: POST /api/sessions/{id}/finished push endpoint
# ---------------------------------------------------------------------------


async def test_post_session_finished_marks_ended_at_and_does_not_touch_status(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    """POST /api/sessions/{session_id}/finished stamps ended_at on the row and returns 204.

    The URL token is the WebDriver session token (Session.session_id), NOT the
    row PK. This mirrors the real testkit call shape: driver.session_id is the
    WebDriver-issued token, which maps to the Session.session_id string column.

    CRITICAL: the endpoint must NOT clobber Session.status — terminal status is
    owned by update_session_status (testkit) or session_sync_loop (fallback).
    """
    from unittest.mock import AsyncMock, patch

    from app.models.session import Session, SessionStatus

    device = await _create_device(db_session, default_host_id)

    session = Session(
        session_id="push-end-sess-1",
        device_id=device["id"],
        status=SessionStatus.running,
        ended_at=None,
    )
    db_session.add(session)
    await db_session.commit()
    # Use session.session_id (the WebDriver token), not session.id (the PK).
    webdriver_token = session.session_id

    with patch(
        "app.services.lifecycle_policy.handle_session_finished",
        new=AsyncMock(return_value=None),
    ) as mock_lifecycle:
        resp = await client.post(f"/api/sessions/{webdriver_token}/finished")

    assert resp.status_code == 204
    mock_lifecycle.assert_awaited_once()

    await db_session.refresh(session)
    assert session.ended_at is not None, "ended_at must be stamped by the push endpoint"
    # Status must be preserved — the push endpoint does NOT own terminal status.
    assert session.status == SessionStatus.running, (
        "POST /finished must not mutate Session.status; "
        "terminal status belongs to update_session_status or session_sync_loop"
    )


async def test_post_session_finished_not_found_returns_404(client: AsyncClient) -> None:
    """POST /api/sessions/{unknown_id}/finished returns 404 when the row is absent."""
    resp = await client.post("/api/sessions/nonexistent-webdriver-token/finished")
    assert resp.status_code == 404


async def test_post_session_finished_is_idempotent_and_does_not_double_fire_lifecycle(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    """Calling the endpoint twice returns 204 both times.

    handle_session_finished must be awaited exactly once — the second call is a
    no-op because ended_at is already set.
    """
    from unittest.mock import AsyncMock, patch

    from app.models.session import Session, SessionStatus

    device = await _create_device(db_session, default_host_id)

    session = Session(
        session_id="push-end-sess-idempotent",
        device_id=device["id"],
        status=SessionStatus.running,
        ended_at=None,
    )
    db_session.add(session)
    await db_session.commit()
    # Use session.session_id (the WebDriver token), not session.id (the PK).
    webdriver_token = session.session_id

    with patch(
        "app.services.lifecycle_policy.handle_session_finished",
        new=AsyncMock(return_value=None),
    ) as mock_lifecycle:
        resp1 = await client.post(f"/api/sessions/{webdriver_token}/finished")
        resp2 = await client.post(f"/api/sessions/{webdriver_token}/finished")

    assert resp1.status_code == 204
    assert resp2.status_code == 204
    mock_lifecycle.assert_awaited_once()


async def test_post_session_finished_real_testkit_call_shape(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    """Mirror the real testkit call shape: register a session with a known string
    session_id, then POST to that string token in the URL.

    The testkit's _wrap_quit_for_notify calls notify_session_finished(driver.session_id)
    where driver.session_id is the WebDriver token — a string that maps to
    Session.session_id, NOT the row PK. This test confirms the endpoint resolves
    via the string column and that D2 push detection works for real testkit traffic.
    """
    from unittest.mock import AsyncMock, patch

    from app.models.session import Session, SessionStatus

    device = await _create_device(db_session, default_host_id)

    # Register a session with an explicit WebDriver-style token, as the testkit does.
    webdriver_token = "webdriver-abc-123"
    session = Session(
        session_id=webdriver_token,
        device_id=device["id"],
        status=SessionStatus.running,
        ended_at=None,
    )
    db_session.add(session)
    await db_session.commit()

    with patch(
        "app.services.lifecycle_policy.handle_session_finished",
        new=AsyncMock(return_value=None),
    ):
        # POST to the string token — exactly what the testkit sends.
        resp = await client.post(f"/api/sessions/{webdriver_token}/finished")

    assert resp.status_code == 204

    await db_session.refresh(session)
    assert session.ended_at is not None, (
        "ended_at must be set when posting the WebDriver token — "
        "PK lookup would have returned 404 (silent no-op in testkit)"
    )


# ---------------------------------------------------------------------------
# D2 regression: ended_at must be durable (actually committed, not just flushed)
# ---------------------------------------------------------------------------


async def test_post_session_finished_ended_at_is_durable_after_request_closes(
    client: AsyncClient,
    db_session: AsyncSession,
    db_session_maker: object,
    default_host_id: str,
) -> None:
    """Regression: mark_session_finished must commit ended_at, not just flush.

    The NO_PENDING branch of handle_session_finished returns without committing.
    Before the fix, mark_session_finished relied on the lifecycle helper to commit,
    so the flushed ended_at write was rolled back when the request-scoped get_db
    session closed — POST returned 204 but the row's ended_at stayed null.

    This test catches the bug by verifying durability via a *second, independent*
    session that cannot see uncommitted writes from db_session.
    """
    from unittest.mock import AsyncMock, patch

    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.models.session import Session, SessionStatus

    device = await _create_device(db_session, default_host_id)

    session_obj = Session(
        session_id="push-end-durable-regression",
        device_id=device["id"],
        status=SessionStatus.running,
        ended_at=None,
    )
    db_session.add(session_obj)
    await db_session.commit()
    row_id = session_obj.id
    webdriver_token = session_obj.session_id

    # Simulate the NO_PENDING path: handle_session_finished returns without
    # calling db.commit(). This is the exact path that exposed the bug.
    with patch(
        "app.services.lifecycle_policy.handle_session_finished",
        new=AsyncMock(return_value=None),
    ):
        resp = await client.post(f"/api/sessions/{webdriver_token}/finished")

    assert resp.status_code == 204

    # Verify durability using an independent session that cannot see
    # uncommitted writes. If mark_session_finished only flushed (did not
    # commit), this second session will see ended_at=None and the assertion
    # will fail — catching the regression.
    maker = cast("async_sessionmaker[AsyncSession]", db_session_maker)
    async with maker() as fresh_session:
        refreshed = await fresh_session.get(Session, row_id)
        assert refreshed is not None
        assert refreshed.ended_at is not None, (
            "ended_at must be committed (not just flushed) by mark_session_finished; "
            "the NO_PENDING lifecycle path returns without committing, so "
            "mark_session_finished must own the final db.commit()"
        )
