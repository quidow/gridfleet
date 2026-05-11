import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.analytics_capacity_snapshot import AnalyticsCapacitySnapshot
from app.models.appium_node import AppiumNode, NodeState
from app.models.device import DeviceHold, DeviceOperationalState
from app.models.host import Host, HostStatus, OSType
from app.models.session import Session, SessionStatus
from app.services.fleet_capacity import (
    _count_schedulable_capacity,
    collect_capacity_snapshot_once,
    get_fleet_capacity_timeline,
    is_unmet_demand_session,
)
from tests.helpers import create_device_record


def _grid_status(*, active_sessions: int, queued_requests: int) -> dict[str, object]:
    slots = [{"session": {"sessionId": f"sess-{idx}"}} for idx in range(active_sessions)]
    return {
        "value": {
            "ready": True,
            "nodes": [{"slots": slots}],
            "sessionQueueRequests": [{} for _ in range(queued_requests)],
        }
    }


async def test_unmet_demand_classifier_counts_only_capacity_failures() -> None:
    started_at = datetime(2026, 4, 18, 10, 1, tzinfo=UTC)

    capacity_failure = Session(
        session_id="no-match",
        status=SessionStatus.error,
        started_at=started_at,
        ended_at=started_at + timedelta(seconds=1),
        error_type="SessionNotCreatedException",
        error_message="No matching capabilities found for requested platform",
    )
    generic_failure = Session(
        session_id="test-failed",
        status=SessionStatus.error,
        started_at=started_at,
        ended_at=started_at + timedelta(minutes=5),
        error_type="AssertionError",
        error_message="Expected title to be Home",
    )
    running_session = Session(
        session_id="running",
        status=SessionStatus.running,
        started_at=started_at,
        error_message="No matching capabilities found for requested platform",
    )
    post_launch_capacity_error = Session(
        session_id="post-launch-capacity",
        status=SessionStatus.error,
        device_id=uuid.uuid4(),
        started_at=started_at,
        ended_at=started_at + timedelta(seconds=1),
        error_type="WebDriverException",
        error_message="No matching capabilities found for requested platform",
    )
    unfinished_pre_execution_error = Session(
        session_id="unfinished-pre-execution",
        status=SessionStatus.error,
        device_id=uuid.uuid4(),
        started_at=started_at,
        ended_at=None,
        error_type="SessionNotCreatedException",
        error_message="No matching capabilities found for requested platform",
    )
    slow_pre_execution_error = Session(
        session_id="slow-pre-execution",
        status=SessionStatus.error,
        device_id=uuid.uuid4(),
        started_at=started_at,
        ended_at=started_at + timedelta(minutes=5),
        error_type="SessionNotCreatedException",
        error_message="No matching capabilities found for requested platform",
    )

    assert is_unmet_demand_session(capacity_failure) is True
    assert is_unmet_demand_session(generic_failure) is False
    assert is_unmet_demand_session(running_session) is False
    assert is_unmet_demand_session(post_launch_capacity_error) is False
    assert is_unmet_demand_session(unfinished_pre_execution_error) is False
    assert is_unmet_demand_session(slow_pre_execution_error) is False


async def test_fleet_capacity_timeline_aggregates_snapshots_and_capacity_rejections(
    db_session: AsyncSession,
) -> None:
    date_from = datetime(2026, 4, 18, 10, 0, tzinfo=UTC)
    db_session.add_all(
        [
            AnalyticsCapacitySnapshot(
                captured_at=date_from,
                total_capacity_slots=4,
                active_sessions=1,
                queued_requests=0,
                available_capacity_slots=3,
                devices_total=4,
                devices_available=3,
                devices_offline=1,
                devices_maintenance=0,
            ),
            AnalyticsCapacitySnapshot(
                captured_at=date_from + timedelta(minutes=1),
                total_capacity_slots=4,
                active_sessions=4,
                queued_requests=2,
                available_capacity_slots=0,
                devices_total=5,
                devices_available=1,
                devices_offline=1,
                devices_maintenance=1,
            ),
            AnalyticsCapacitySnapshot(
                captured_at=date_from + timedelta(minutes=3),
                total_capacity_slots=5,
                active_sessions=2,
                queued_requests=0,
                available_capacity_slots=3,
                devices_total=6,
                devices_available=4,
                devices_offline=0,
                devices_maintenance=1,
            ),
        ]
    )
    db_session.add_all(
        [
            Session(
                session_id="capacity-fast-fail",
                status=SessionStatus.error,
                started_at=date_from + timedelta(minutes=1, seconds=10),
                ended_at=date_from + timedelta(minutes=1, seconds=11),
                error_type="SessionNotCreatedException",
                error_message="Unable to create session: no matching capability found",
            ),
            Session(
                session_id="generic-test-fail",
                status=SessionStatus.error,
                started_at=date_from + timedelta(minutes=1, seconds=20),
                ended_at=date_from + timedelta(minutes=1, seconds=25),
                error_type="RuntimeError",
                error_message="Application crashed after launch",
            ),
        ]
    )
    await db_session.commit()

    response = await get_fleet_capacity_timeline(
        db_session,
        date_from=date_from,
        date_to=date_from + timedelta(minutes=4),
        bucket_minutes=1,
    )

    assert response.bucket_minutes == 1
    assert [point.timestamp for point in response.series] == [
        date_from,
        date_from + timedelta(minutes=1),
        date_from + timedelta(minutes=3),
    ]
    pressure_bucket = response.series[1]
    assert pressure_bucket.total_capacity_slots == 4
    assert pressure_bucket.active_sessions == 4
    assert pressure_bucket.queued_requests == 2
    assert pressure_bucket.rejected_unfulfilled_sessions == 1
    assert pressure_bucket.available_capacity_slots == 0
    assert pressure_bucket.inferred_demand == 7
    assert pressure_bucket.devices_total == 5
    assert pressure_bucket.devices_available == 1
    assert pressure_bucket.devices_offline == 1
    assert pressure_bucket.devices_maintenance == 1


async def test_capacity_snapshot_collector_counts_verified_running_nodes(
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    schedulable = await create_device_record(
        db_session,
        host_id=default_host_id,
        identity_value="capacity-schedulable",
        name="Capacity Phone",
        operational_state=DeviceOperationalState.available,
    )
    busy = await create_device_record(
        db_session,
        host_id=default_host_id,
        identity_value="capacity-busy",
        name="Busy Phone",
        operational_state=DeviceOperationalState.busy,
    )
    unverified = await create_device_record(
        db_session,
        host_id=default_host_id,
        identity_value="capacity-unverified",
        name="Unverified Phone",
        operational_state=DeviceOperationalState.available,
        verified=False,
    )
    offline = await create_device_record(
        db_session,
        host_id=default_host_id,
        identity_value="capacity-offline",
        name="Offline Phone",
        operational_state=DeviceOperationalState.offline,
    )
    stopped = await create_device_record(
        db_session,
        host_id=default_host_id,
        identity_value="capacity-stopped",
        name="Stopped Phone",
        operational_state=DeviceOperationalState.available,
    )
    db_session.add_all(
        [
            AppiumNode(device_id=schedulable.id, port=4723, grid_url="http://grid", state=NodeState.running),
            AppiumNode(device_id=busy.id, port=4724, grid_url="http://grid", state=NodeState.running),
            AppiumNode(device_id=unverified.id, port=4725, grid_url="http://grid", state=NodeState.running),
            AppiumNode(device_id=offline.id, port=4726, grid_url="http://grid", state=NodeState.running),
            AppiumNode(device_id=stopped.id, port=4727, grid_url="http://grid", state=NodeState.stopped),
        ]
    )
    await db_session.commit()

    with patch(
        "app.services.fleet_capacity.grid_service.get_grid_status",
        new=AsyncMock(return_value=_grid_status(active_sessions=3, queued_requests=2)),
    ):
        snapshot = await collect_capacity_snapshot_once(db_session, captured_at=datetime(2026, 4, 18, 12, tzinfo=UTC))

    assert snapshot is not None
    assert snapshot.total_capacity_slots == 2
    assert snapshot.active_sessions == 3
    assert snapshot.queued_requests == 2
    assert snapshot.available_capacity_slots == 0

    stored = (await db_session.execute(select(AnalyticsCapacitySnapshot))).scalars().all()
    assert len(stored) == 1


async def test_count_schedulable_capacity_uses_pid_not_state(
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    device = await create_device_record(
        db_session,
        host_id=default_host_id,
        identity_value="capacity-no-pid",
        name="Capacity No PID",
        operational_state=DeviceOperationalState.available,
    )
    db_session.add(
        AppiumNode(
            device_id=device.id,
            port=4723,
            grid_url="http://grid",
            state=NodeState.running,
            desired_state=NodeState.running,
            pid=None,
            active_connection_target=None,
        )
    )
    await db_session.commit()

    assert await _count_schedulable_capacity(db_session) == 0


async def test_capacity_snapshot_collector_skips_unreachable_grid(db_session: AsyncSession) -> None:
    with patch(
        "app.services.fleet_capacity.grid_service.get_grid_status",
        new=AsyncMock(return_value={"ready": False, "error": "connect failed"}),
    ):
        snapshot = await collect_capacity_snapshot_once(db_session)

    assert snapshot is None
    assert (await db_session.execute(select(AnalyticsCapacitySnapshot))).scalars().all() == []


async def test_collect_capacity_snapshot_records_fleet_counts(
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    # Seed 2 additional hosts: 1 online, 1 offline (default_host_id host already exists)
    # We create 2 fresh hosts so counts are fully controlled
    online_host = Host(
        hostname="fleet-test-online",
        ip="10.0.1.1",
        os_type=OSType.linux,
        agent_port=5100,
        status=HostStatus.online,
    )
    offline_host = Host(
        hostname="fleet-test-offline",
        ip="10.0.1.2",
        os_type=OSType.linux,
        agent_port=5100,
        status=HostStatus.offline,
    )
    db_session.add_all([online_host, offline_host])
    await db_session.commit()
    await db_session.refresh(online_host)

    # Seed 3 devices: 1 available, 1 offline, 1 maintenance
    await create_device_record(
        db_session,
        host_id=online_host.id,
        identity_value="fleet-available",
        name="Fleet Available Phone",
        operational_state=DeviceOperationalState.available,
    )
    await create_device_record(
        db_session,
        host_id=online_host.id,
        identity_value="fleet-offline-1",
        name="Fleet Offline Phone",
        operational_state=DeviceOperationalState.offline,
    )
    await create_device_record(
        db_session,
        host_id=online_host.id,
        identity_value="fleet-maintenance",
        name="Fleet Maintenance Phone",
        hold=DeviceHold.maintenance,
    )

    # Count hosts after all seeds — default_host_id fixture adds one more online host,
    # so query actual DB counts to make the assertion robust.
    hosts_total = int((await db_session.execute(select(func.count()).select_from(Host))).scalar_one())
    hosts_online_count = int(
        (
            await db_session.execute(select(func.count()).select_from(Host).where(Host.status == HostStatus.online))
        ).scalar_one()
    )

    with patch(
        "app.services.fleet_capacity.grid_service.get_grid_status",
        new=AsyncMock(return_value=_grid_status(active_sessions=0, queued_requests=0)),
    ):
        snapshot = await collect_capacity_snapshot_once(db_session, captured_at=datetime(2026, 4, 18, 13, tzinfo=UTC))

    assert snapshot is not None
    assert snapshot.hosts_total == hosts_total
    assert snapshot.hosts_online == hosts_online_count
    assert snapshot.devices_total == 3
    assert snapshot.devices_available == 1
    assert snapshot.devices_offline == 1
    assert snapshot.devices_maintenance == 1


async def test_capacity_timeline_endpoint_validates_date_range(client: AsyncClient) -> None:
    date_from = datetime(2026, 4, 18, 12, tzinfo=UTC)
    response = await client.get(
        "/api/analytics/fleet/capacity-timeline",
        params={
            "date_from": date_from.isoformat(),
            "date_to": (date_from - timedelta(minutes=1)).isoformat(),
        },
    )

    assert response.status_code == 422
