from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from app.analytics import service as analytics_service
from app.analytics.schemas import GroupByOption
from app.devices.models import DeviceEvent, DeviceEventType
from app.sessions.models import Session, SessionStatus
from tests.helpers import create_device_record, seed_host_and_device

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


async def _seed_analytics_data(db_session: AsyncSession) -> tuple[datetime, datetime, object, object]:
    start = datetime(2026, 5, 1, 10, 0, tzinfo=UTC)
    date_to = start + timedelta(days=1)
    host, android = await seed_host_and_device(db_session, identity="analytics-android")
    ios = await create_device_record(
        db_session,
        host_id=host.id,
        identity_value="analytics-ios",
        name="Analytics iOS",
        pack_id="appium-xcuitest",
        platform_id="ios_real",
        identity_scheme="ios_udid",
        os_version="18",
    )
    db_session.add_all(
        [
            Session(
                session_id="s-passed",
                device_id=android.id,
                test_name="tests/test_login.py::test_login",
                started_at=start,
                ended_at=start + timedelta(minutes=10),
                status=SessionStatus.passed,
            ),
            Session(
                session_id="s-failed",
                device_id=android.id,
                test_name="tests/test_login.py::test_logout",
                started_at=start + timedelta(hours=2),
                ended_at=start + timedelta(hours=2, minutes=5),
                status=SessionStatus.failed,
            ),
            Session(
                session_id="s-error",
                device_id=ios.id,
                test_name="tests/test_ios.py::test_launch",
                started_at=start + timedelta(hours=3),
                ended_at=start + timedelta(hours=3, minutes=1),
                status=SessionStatus.error,
            ),
            Session(
                session_id="reserved",
                device_id=ios.id,
                test_name="tests/test_reserved.py::test_probe",
                started_at=start + timedelta(hours=4),
                ended_at=start + timedelta(hours=5),
                status=SessionStatus.passed,
            ),
            DeviceEvent(
                device_id=android.id,
                event_type=DeviceEventType.health_check_fail,
                created_at=start + timedelta(minutes=1),
                details={},
            ),
            DeviceEvent(
                device_id=android.id,
                event_type=DeviceEventType.connectivity_lost,
                created_at=start + timedelta(minutes=2),
                details={},
            ),
            DeviceEvent(
                device_id=ios.id,
                event_type=DeviceEventType.node_crash,
                created_at=start + timedelta(minutes=3),
                details={},
            ),
        ]
    )
    await db_session.commit()
    return start, date_to, android, ios


async def test_session_summary_groups_by_supported_dimensions(db_session: AsyncSession) -> None:
    date_from, date_to, android, _ios = await _seed_analytics_data(db_session)

    by_platform = await analytics_service.get_session_summary(db_session, date_from, date_to, GroupByOption.platform)
    assert [(row.group_key, row.total, row.passed, row.failed, row.error) for row in by_platform] == [
        ("android_mobile", 2, 1, 1, 0),
        ("ios_real", 1, 0, 0, 1),
    ]
    assert by_platform[0].avg_duration_sec == 450.0

    by_os = await analytics_service.get_session_summary(db_session, date_from, date_to, GroupByOption.os_version)
    assert [row.group_key for row in by_os] == ["14", "18"]

    by_device = await analytics_service.get_session_summary(db_session, date_from, date_to, GroupByOption.device_id)
    assert {row.group_key for row in by_device} >= {str(android.id)}

    by_day = await analytics_service.get_session_summary(db_session, date_from, date_to, GroupByOption.day)
    assert by_day[0].group_key.startswith("2026-05-01")


async def test_device_utilization_handles_invalid_range_and_overlaps(db_session: AsyncSession) -> None:
    date_from, date_to, android, _ios = await _seed_analytics_data(db_session)

    assert await analytics_service.get_device_utilization(db_session, date_to, date_from) == []

    rows = await analytics_service.get_device_utilization(db_session, date_from, date_to)
    android_row = next(row for row in rows if row.device_id == str(android.id))
    assert android_row.device_name == "Device analytics-android"
    assert android_row.total_session_time_sec == 900.0
    assert android_row.idle_time_sec == 85500.0
    assert android_row.session_count == 2


async def test_device_reliability_and_fleet_overview(db_session: AsyncSession) -> None:
    date_from, date_to, _android, _ios = await _seed_analytics_data(db_session)

    reliability = await analytics_service.get_device_reliability(db_session, date_from, date_to)
    assert [(row.platform_id, row.total_incidents) for row in reliability] == [
        ("android_mobile", 2),
        ("ios_real", 1),
    ]
    assert reliability[0].health_check_failures == 1
    assert reliability[0].connectivity_losses == 1
    assert reliability[1].node_crashes == 1

    overview = await analytics_service.get_fleet_overview(db_session, date_from, date_to)
    assert overview.devices_by_platform == {"android_mobile": 1, "ios_real": 1}
    assert overview.avg_utilization_pct > 0
    assert [device.platform_id for device in overview.most_used] == ["android_mobile", "ios_real"]
    assert [device.platform_id for device in overview.least_used] == ["ios_real", "android_mobile"]
    assert overview.pass_rate_pct == 33.33
    assert overview.devices_needing_attention == 0
