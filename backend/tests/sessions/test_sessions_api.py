from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx2 import AsyncClient
from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.grid.models import GridQueueStatus, GridSessionQueueTicket
from app.sessions import service as session_module
from app.sessions.service_viability import PROBE_TEST_NAME
from tests.helpers import create_device_record, create_reserved_run, drain_handlers, recent_events
from tests.helpers import test_event_bus as event_bus


@pytest.fixture(autouse=True)
def _inject_publisher_into_session_events(monkeypatch: pytest.MonkeyPatch) -> None:
    """Inject publisher=event_bus into session event helpers."""
    _orig_started = session_module.queue_session_started_event
    _orig_ended = session_module.queue_session_ended_event

    def _wrapped_started(*args: object, **kwargs: object) -> None:
        kwargs.setdefault("publisher", event_bus)
        _orig_started(*args, **kwargs)  # type: ignore[arg-type]

    def _wrapped_ended(*args: object, **kwargs: object) -> None:
        kwargs.setdefault("publisher", event_bus)
        _orig_ended(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(session_module, "queue_session_started_event", _wrapped_started)
    monkeypatch.setattr(session_module, "queue_session_ended_event", _wrapped_ended)


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
    from app.sessions.models import Session, SessionStatus

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
    from app.sessions.models import Session, SessionStatus

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
    from app.sessions.models import Session, SessionStatus

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


async def test_list_sessions_excludes_reserved_and_probes_by_default(
    client: AsyncClient, db_session: AsyncSession, default_host_id: str
) -> None:
    from app.sessions.models import Session, SessionStatus

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
    assert data["total"] == 1
    assert [row["session_id"] for row in data["items"]] == ["grid-real"]

    resp_with_probes = await client.get("/api/sessions", params={"include_probes": "true"})
    assert resp_with_probes.status_code == 200
    data_with_probes = resp_with_probes.json()
    assert data_with_probes["total"] == 2
    assert [row["session_id"] for row in data_with_probes["items"]] == ["probe-sess-1", "grid-real"]
    probe_row = next(row for row in data_with_probes["items"] if row["session_id"] == "probe-sess-1")
    assert probe_row["is_probe"] is True
    real_row = next(row for row in data_with_probes["items"] if row["session_id"] == "grid-real")
    assert real_row["is_probe"] is False


async def test_list_sessions_paginates_and_sorts(
    client: AsyncClient, db_session: AsyncSession, default_host_id: str
) -> None:
    from app.sessions.models import Session, SessionStatus

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
    from app.sessions.models import Session, SessionStatus

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
    from app.sessions.models import Session, SessionStatus

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
    from app.sessions.models import Session, SessionStatus

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


async def test_list_sessions_includes_device_less_sessions(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    """Sessions without a device (error sessions) must appear in the list endpoint."""
    from app.sessions.models import Session, SessionStatus

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
    from app.sessions.models import Session, SessionStatus

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
    from app.sessions.models import Session, SessionStatus

    device = await _create_device(db_session, default_host_id)

    session = Session(session_id="gs-update", device_id=device["id"], status=SessionStatus.running)
    db_session.add(session)
    await db_session.commit()

    resp = await client.patch("/api/sessions/gs-update/status", json={"status": "failed"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "failed"
    assert data["ended_at"] is not None

    await drain_handlers(event_bus)
    events = recent_events(event_bus, limit=1)
    assert len(events) == 1
    assert events[0]["type"] == "session.ended"
    assert events[0]["data"]["session_id"] == "gs-update"
    assert events[0]["data"]["status"] == "failed"


async def test_grid_queue(client: AsyncClient, db_session: AsyncSession) -> None:
    db_session.add(
        GridSessionQueueTicket(
            requested_body={"capabilities": {"alwaysMatch": {"platformName": "android"}}},
            status=GridQueueStatus.waiting,
        )
    )
    await db_session.commit()

    resp = await client.get("/api/grid/queue")

    assert resp.status_code == 200
    data = resp.json()
    assert data["queue_size"] == 1
    assert data["requests"][0]["capabilities"]["platformName"] == "android"


# ---------------------------------------------------------------------------
# Phase 121: run_id persistence and run-scoped filtering
# ---------------------------------------------------------------------------


async def test_list_sessions_filter_by_run_id_offset_mode(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    """GET /api/sessions?run_id=<id> returns only sessions belonging to that run."""
    from app.sessions.models import Session, SessionStatus

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
    from app.sessions.models import Session, SessionStatus

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


async def test_session_detail_exposes_actual_capabilities(
    client: AsyncClient, db_session: AsyncSession, default_host_id: str
) -> None:
    from app.sessions.models import Session, SessionStatus

    device = await _create_device(db_session, default_host_id)
    session = Session(
        session_id="caps-sess-001",
        device_id=device["id"],
        status=SessionStatus.running,
        requested_capabilities={"platformName": "Android"},
        actual_capabilities={"platformName": "Android", "appium:systemPort": 8201},
    )
    db_session.add(session)
    await db_session.commit()
    db_session.expunge(session)

    resp = await client.get("/api/sessions/caps-sess-001")
    assert resp.status_code == 200
    data = resp.json()
    assert data["requested_capabilities"] == {"platformName": "Android"}
    assert data["actual_capabilities"] == {"platformName": "Android", "appium:systemPort": 8201}


def test_confirm_request_drops_oversized_capabilities() -> None:
    from app.grid.schemas_internal import ConfirmRequest

    ok = ConfirmRequest(appium_session_id="s1", appium_capabilities={"a": 1})
    assert ok.appium_capabilities == {"a": 1}

    # > 32 KB serialized -> dropped, NOT rejected: caps capture must never fail a confirm.
    oversized = ConfirmRequest(appium_session_id="s1", appium_capabilities={"blob": "x" * (33 * 1024)})
    assert oversized.appium_capabilities is None

    absent = ConfirmRequest(appium_session_id="s1")
    assert absent.appium_capabilities is None


async def test_register_and_finished_endpoints_are_removed(client: AsyncClient) -> None:
    """The Selenium Grid-era client session-bookkeeping endpoints are gone.

    Session rows are owned by the router/grid allocation flow; clients only
    PATCH the outcome. POST /api/sessions has no method (GET survives → 405);
    POST /api/sessions/{id}/finished has no route at all (404).
    """
    resp = await client.post("/api/sessions", json={"session_id": "x"})
    assert resp.status_code == 405
    resp = await client.post("/api/sessions/some-session/finished")
    assert resp.status_code == 404


async def test_list_sessions_active_filter(client: AsyncClient, db_session: AsyncSession, default_host_id: str) -> None:
    from app.sessions.models import Session, SessionStatus

    device = await _create_device(db_session, default_host_id)
    live = Session(session_id="active-1", device_id=device["id"], status=SessionStatus.running)
    pending = Session(session_id="active-2", device_id=device["id"], status=SessionStatus.pending)
    done = Session(
        session_id="ended-1",
        device_id=device["id"],
        status=SessionStatus.passed,
        ended_at=datetime.now(UTC),
    )
    db_session.add_all([live, pending, done])
    await db_session.commit()

    resp = await client.get("/api/sessions", params={"active": "true"})
    assert resp.status_code == 200
    ids = {item["session_id"] for item in resp.json()["items"]}
    assert ids == {"active-1", "active-2"}

    # Cursor mode honors the filter too.
    resp = await client.get("/api/sessions", params={"active": "true", "direction": "older"})
    assert resp.status_code == 200
    ids = {item["session_id"] for item in resp.json()["items"]}
    assert ids == {"active-1", "active-2"}
