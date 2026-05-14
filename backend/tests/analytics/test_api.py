import csv
import io
from datetime import UTC, datetime, timedelta
from typing import Any

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.devices.models import DeviceEvent, DeviceEventType
from app.sessions.models import Session, SessionStatus
from app.sessions.service_viability import PROBE_TEST_NAME
from tests.helpers import create_device_record

DEVICE_PAYLOAD = {
    "identity_value": "analytics-device-1",
    "name": "Analytics Test Phone",
    "pack_id": "appium-uiautomator2",
    "platform_id": "android_mobile",
    "identity_scheme": "android_serial",
    "identity_scope": "host",
    "os_version": "14",
}

DEVICE_PAYLOAD_2 = {
    "identity_value": "analytics-device-2",
    "name": "Analytics Test TV",
    "pack_id": "appium-uiautomator2",
    "platform_id": "firetv_real",
    "identity_scheme": "android_serial",
    "identity_scope": "host",
    "os_version": "8",
}


async def _seed_data(
    db_session: AsyncSession,
    host_id: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Create devices, sessions, and events for analytics tests."""
    device_one = await create_device_record(
        db_session,
        host_id=host_id,
        identity_value=DEVICE_PAYLOAD["identity_value"],
        connection_target=DEVICE_PAYLOAD["identity_value"],
        name=DEVICE_PAYLOAD["name"],
        pack_id=DEVICE_PAYLOAD["pack_id"],
        platform_id=DEVICE_PAYLOAD["platform_id"],
        identity_scheme=DEVICE_PAYLOAD["identity_scheme"],
        identity_scope=DEVICE_PAYLOAD["identity_scope"],
        os_version=DEVICE_PAYLOAD["os_version"],
    )
    device_two = await create_device_record(
        db_session,
        host_id=host_id,
        identity_value=DEVICE_PAYLOAD_2["identity_value"],
        connection_target=DEVICE_PAYLOAD_2["identity_value"],
        name=DEVICE_PAYLOAD_2["name"],
        pack_id=DEVICE_PAYLOAD_2["pack_id"],
        platform_id=DEVICE_PAYLOAD_2["platform_id"],
        identity_scheme=DEVICE_PAYLOAD_2["identity_scheme"],
        identity_scope=DEVICE_PAYLOAD_2["identity_scope"],
        os_version=DEVICE_PAYLOAD_2["os_version"],
    )
    d1 = {"id": str(device_one.id)}
    d2 = {"id": str(device_two.id)}

    now = datetime.now(UTC)

    # Sessions for device 1
    sessions = [
        Session(
            session_id="an-s1",
            device_id=d1["id"],
            test_name="test_checkout",
            status=SessionStatus.passed,
            started_at=now - timedelta(hours=5),
            ended_at=now - timedelta(hours=4),
        ),
        Session(
            session_id="an-s2",
            device_id=d1["id"],
            test_name="test_login",
            status=SessionStatus.failed,
            started_at=now - timedelta(hours=3),
            ended_at=now - timedelta(hours=2, minutes=30),
        ),
        Session(
            session_id="an-s3",
            device_id=d2["id"],
            test_name="test_playback",
            status=SessionStatus.passed,
            started_at=now - timedelta(hours=2),
            ended_at=now - timedelta(hours=1),
        ),
        Session(
            session_id="an-s4",
            device_id=d1["id"],
            test_name="test_settings",
            status=SessionStatus.error,
            started_at=now - timedelta(hours=1),
            ended_at=now - timedelta(minutes=45),
        ),
    ]
    db_session.add_all(sessions)

    # Device events
    events = [
        DeviceEvent(
            device_id=d1["id"],
            event_type=DeviceEventType.health_check_fail,
            details={"consecutive_failures": 1},
            created_at=now - timedelta(hours=4),
        ),
        DeviceEvent(
            device_id=d1["id"],
            event_type=DeviceEventType.node_crash,
            details={"error": "Max failures"},
            created_at=now - timedelta(hours=3),
        ),
        DeviceEvent(
            device_id=d2["id"],
            event_type=DeviceEventType.connectivity_lost,
            details={"reason": "Disconnected"},
            created_at=now - timedelta(hours=2),
        ),
        DeviceEvent(
            device_id=d2["id"],
            event_type=DeviceEventType.connectivity_restored,
            details={"reason": "Reconnected"},
            created_at=now - timedelta(hours=1),
        ),
    ]
    db_session.add_all(events)
    await db_session.commit()

    return dict(d1), dict(d2)


# --- Session Summary ---


async def test_session_summary_empty(client: AsyncClient) -> None:
    resp = await client.get("/api/analytics/sessions/summary")
    assert resp.status_code == 200
    assert resp.json() == []


async def test_session_summary_by_platform(client: AsyncClient, db_session: AsyncSession, default_host_id: str) -> None:
    await _seed_data(db_session, default_host_id)
    resp = await client.get(
        "/api/analytics/sessions/summary",
        params={"group_by": "platform"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 2  # android_mobile and firetv

    android = next(r for r in data if r["group_key"] == "android_mobile")
    assert android["total"] == 3
    assert android["passed"] == 1
    assert android["failed"] == 1
    assert android["error"] == 1

    firetv = next(r for r in data if r["group_key"] == "firetv_real")
    assert firetv["total"] == 1
    assert firetv["passed"] == 1


async def test_session_summary_by_day(client: AsyncClient, db_session: AsyncSession, default_host_id: str) -> None:
    await _seed_data(db_session, default_host_id)
    resp = await client.get(
        "/api/analytics/sessions/summary",
        params={"group_by": "day"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) >= 1


async def test_session_summary_by_os_version(
    client: AsyncClient, db_session: AsyncSession, default_host_id: str
) -> None:
    await _seed_data(db_session, default_host_id)
    resp = await client.get(
        "/api/analytics/sessions/summary",
        params={"group_by": "os_version"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 2  # os_version "14" and "8"


async def test_session_summary_date_filter(client: AsyncClient, db_session: AsyncSession, default_host_id: str) -> None:
    await _seed_data(db_session, default_host_id)
    # Use a range that covers nothing (far future)
    future = datetime.now(UTC) + timedelta(days=30)
    resp = await client.get(
        "/api/analytics/sessions/summary",
        params={
            "date_from": future.isoformat(),
            "date_to": (future + timedelta(days=1)).isoformat(),
        },
    )
    assert resp.status_code == 200
    assert resp.json() == []


async def test_session_summary_avg_duration(
    client: AsyncClient, db_session: AsyncSession, default_host_id: str
) -> None:
    await _seed_data(db_session, default_host_id)
    resp = await client.get(
        "/api/analytics/sessions/summary",
        params={"group_by": "platform"},
    )
    data = resp.json()
    android = next(r for r in data if r["group_key"] == "android_mobile")
    # All android sessions have ended_at, so avg_duration_sec should be non-null
    assert android["avg_duration_sec"] is not None
    assert android["avg_duration_sec"] > 0


# --- Device Utilization ---


async def test_device_utilization_empty(client: AsyncClient) -> None:
    resp = await client.get("/api/analytics/devices/utilization")
    assert resp.status_code == 200
    assert resp.json() == []


async def test_device_utilization(client: AsyncClient, db_session: AsyncSession, default_host_id: str) -> None:
    await _seed_data(db_session, default_host_id)
    resp = await client.get("/api/analytics/devices/utilization")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 2

    for row in data:
        assert row["busy_pct"] >= 0
        assert row["busy_pct"] <= 100
        assert row["session_count"] >= 1
        assert row["total_session_time_sec"] >= 0
        assert row["idle_time_sec"] >= 0


async def test_device_utilization_clamps_session_overlap_to_requested_window(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    device = await create_device_record(
        db_session,
        host_id=default_host_id,
        identity_value="analytics-window-1",
        connection_target="analytics-window-1",
        name="Overlap Device",
        os_version="14",
    )
    date_from = datetime(2026, 1, 10, 10, 0, tzinfo=UTC)
    date_to = datetime(2026, 1, 10, 11, 0, tzinfo=UTC)
    db_session.add_all(
        [
            Session(
                session_id="window-before",
                device_id=device.id,
                status=SessionStatus.passed,
                started_at=date_from - timedelta(minutes=30),
                ended_at=date_from + timedelta(minutes=15),
            ),
            Session(
                session_id="window-after",
                device_id=device.id,
                status=SessionStatus.passed,
                started_at=date_to - timedelta(minutes=10),
                ended_at=date_to + timedelta(minutes=20),
            ),
            Session(
                session_id="window-span",
                device_id=device.id,
                status=SessionStatus.passed,
                started_at=date_from - timedelta(minutes=10),
                ended_at=date_to + timedelta(minutes=10),
            ),
            Session(
                session_id="window-open",
                device_id=device.id,
                status=SessionStatus.running,
                started_at=date_to - timedelta(minutes=5),
                ended_at=None,
            ),
        ]
    )
    await db_session.commit()

    resp = await client.get(
        "/api/analytics/devices/utilization",
        params={"date_from": date_from.isoformat(), "date_to": date_to.isoformat()},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    row = data[0]

    expected_total = (15 + 10 + 60 + 5) * 60
    assert row["total_session_time_sec"] == expected_total
    assert row["idle_time_sec"] == 0
    assert row["busy_pct"] == 100.0


# --- Device Reliability ---


async def test_device_reliability_empty(client: AsyncClient) -> None:
    resp = await client.get("/api/analytics/devices/reliability")
    assert resp.status_code == 200
    assert resp.json() == []


async def test_device_reliability(client: AsyncClient, db_session: AsyncSession, default_host_id: str) -> None:
    d1, d2 = await _seed_data(db_session, default_host_id)
    db_session.add(
        DeviceEvent(
            device_id=d1["id"],
            event_type=DeviceEventType.lifecycle_recovery_failed,
            details={"reason": "Probe failed", "summary_state": "backoff"},
            created_at=datetime.now(UTC) - timedelta(minutes=30),
        )
    )
    await db_session.commit()
    resp = await client.get("/api/analytics/devices/reliability")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 2

    dev1 = next(r for r in data if r["device_id"] == d1["id"])
    assert dev1["health_check_failures"] == 1
    assert dev1["node_crashes"] == 1
    assert dev1["connectivity_losses"] == 0
    assert dev1["total_incidents"] == 2

    dev2 = next(r for r in data if r["device_id"] == d2["id"])
    assert dev2["connectivity_losses"] == 1
    assert dev2["total_incidents"] == 1


# --- Fleet Overview ---


async def test_fleet_overview_empty(client: AsyncClient) -> None:
    resp = await client.get("/api/analytics/fleet/overview")
    assert resp.status_code == 200
    data = resp.json()
    assert data["devices_by_platform"] == {}
    assert data["avg_utilization_pct"] == 0
    assert data["pass_rate_pct"] is None
    assert data["devices_needing_attention"] == 0


async def test_fleet_overview(client: AsyncClient, db_session: AsyncSession, default_host_id: str) -> None:
    await _seed_data(db_session, default_host_id)
    resp = await client.get("/api/analytics/fleet/overview")
    assert resp.status_code == 200
    data = resp.json()

    assert data["devices_by_platform"]["android_mobile"] == 1
    assert data["devices_by_platform"]["firetv_real"] == 1
    assert data["avg_utilization_pct"] >= 0
    assert data["pass_rate_pct"] is not None
    assert data["pass_rate_pct"] == 50.0  # 2 passed out of 4
    assert len(data["most_used"]) <= 5
    assert len(data["least_used"]) <= 5


async def test_fleet_overview_excludes_reserved_probe_and_manual_inspector_sessions(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    d1, _d2 = await _seed_data(db_session, default_host_id)
    now = datetime.now(UTC)
    db_session.add_all(
        [
            Session(
                session_id="reserved",
                device_id=d1["id"],
                status=SessionStatus.passed,
                started_at=now - timedelta(minutes=20),
                ended_at=now - timedelta(minutes=10),
            ),
            Session(
                session_id="probe-s1",
                device_id=d1["id"],
                test_name=PROBE_TEST_NAME,
                status=SessionStatus.passed,
                started_at=now - timedelta(minutes=15),
                ended_at=now - timedelta(minutes=14),
            ),
            Session(
                session_id="appium-inspector-manual",
                device_id=d1["id"],
                test_name=None,
                status=SessionStatus.failed,
                started_at=now - timedelta(minutes=12),
                ended_at=now - timedelta(minutes=11),
                requested_capabilities={
                    "platformName": "Android",
                    "appium:automationName": "UiAutomator2",
                },
            ),
        ]
    )
    await db_session.commit()

    resp = await client.get("/api/analytics/fleet/overview")
    assert resp.status_code == 200
    data = resp.json()

    assert data["pass_rate_pct"] == 50.0


async def test_fleet_overview_uses_clamped_utilization_window(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    device_one = await create_device_record(
        db_session,
        host_id=default_host_id,
        identity_value="analytics-fleet-window-1",
        connection_target="analytics-fleet-window-1",
        name="Fleet Window One",
        os_version="14",
    )
    device_two = await create_device_record(
        db_session,
        host_id=default_host_id,
        identity_value="analytics-fleet-window-2",
        connection_target="analytics-fleet-window-2",
        name="Fleet Window Two",
        platform_id="firetv_real",
        os_version="8",
    )
    date_from = datetime(2026, 2, 1, 10, 0, tzinfo=UTC)
    date_to = datetime(2026, 2, 1, 11, 0, tzinfo=UTC)
    db_session.add_all(
        [
            Session(
                session_id="fleet-overlap-1",
                device_id=device_one.id,
                status=SessionStatus.passed,
                started_at=date_from - timedelta(minutes=20),
                ended_at=date_from + timedelta(minutes=10),
            ),
            Session(
                session_id="fleet-overlap-2",
                device_id=device_two.id,
                status=SessionStatus.passed,
                started_at=date_from + timedelta(minutes=20),
                ended_at=date_to + timedelta(minutes=30),
            ),
        ]
    )
    await db_session.commit()

    resp = await client.get(
        "/api/analytics/fleet/overview",
        params={"date_from": date_from.isoformat(), "date_to": date_to.isoformat()},
    )
    assert resp.status_code == 200
    data = resp.json()

    assert data["avg_utilization_pct"] == 41.67


# --- CSV Export ---


async def test_session_summary_csv(client: AsyncClient, db_session: AsyncSession, default_host_id: str) -> None:
    await _seed_data(db_session, default_host_id)
    resp = await client.get(
        "/api/analytics/sessions/summary",
        params={"group_by": "platform", "format": "csv"},
    )
    assert resp.status_code == 200
    assert "text/csv" in resp.headers["content-type"]
    assert "session_summary.csv" in resp.headers.get("content-disposition", "")

    reader = csv.DictReader(io.StringIO(resp.text))
    rows = list(reader)
    assert len(rows) == 2
    assert "total" in rows[0]
    assert "passed" in rows[0]


async def test_device_utilization_csv(client: AsyncClient, db_session: AsyncSession, default_host_id: str) -> None:
    await _seed_data(db_session, default_host_id)
    resp = await client.get(
        "/api/analytics/devices/utilization",
        params={"format": "csv"},
    )
    assert resp.status_code == 200
    assert "text/csv" in resp.headers["content-type"]


async def test_device_reliability_csv(client: AsyncClient, db_session: AsyncSession, default_host_id: str) -> None:
    await _seed_data(db_session, default_host_id)
    resp = await client.get(
        "/api/analytics/devices/reliability",
        params={"format": "csv"},
    )
    assert resp.status_code == 200
    assert "text/csv" in resp.headers["content-type"]


async def test_csv_empty(client: AsyncClient) -> None:
    resp = await client.get(
        "/api/analytics/sessions/summary",
        params={"format": "csv"},
    )
    assert resp.status_code == 200
    assert "text/csv" in resp.headers["content-type"]
