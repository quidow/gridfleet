from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock

import pytest

from app.core.pagination import CursorPaginationError, encode_cursor
from app.devices.models import DeviceEvent, DeviceEventType
from app.devices.schemas.device import DeviceLifecyclePolicySummaryState
from app.devices.services.identity_conflicts import DeviceIdentityConflictService
from app.devices.services.presenter import DevicePresenterService
from app.hosts.service import HostTarget
from app.lifecycle.services import incidents
from app.lifecycle.services.incidents import LifecycleIncidentDetails, LifecycleIncidentService
from app.packs.services.discovery import PackDiscoveryService
from tests.helpers import create_device_record

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.hosts.models import Host


async def test_lifecycle_incident_record_serialize_and_paginate(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    device = await create_device_record(
        db_session,
        host_id=db_host.id,
        identity_value="incident-device-001",
        connection_target="incident-device-001",
        name="Incident Device",
    )
    run_id = __import__("uuid").uuid4()
    event = await LifecycleIncidentService().record_lifecycle_incident(
        db_session,
        device,
        DeviceEventType.lifecycle_recovery_backoff,
        LifecycleIncidentDetails(
            summary_state=DeviceLifecyclePolicySummaryState.recoverable,
            reason="adb offline",
            detail="recovery delayed",
            source="test",
            run_id=run_id,
            run_name="run",
            backoff_until=datetime.now(UTC) + timedelta(minutes=5),
        ),
    )
    await db_session.commit()
    await db_session.refresh(event)

    serialized = incidents.serialize_lifecycle_incident(event, device)
    assert serialized.run_id == run_id
    assert serialized.summary_state == DeviceLifecyclePolicySummaryState.recoverable
    assert serialized.backoff_until is not None

    invalid = DeviceEvent(
        id=__import__("uuid").uuid4(),
        device_id=device.id,
        event_type=DeviceEventType.lifecycle_run_excluded,
        details={"summary_state": "bogus", "run_id": "not-a-uuid", "backoff_until": "not-a-date"},
        created_at=datetime.now(UTC),
    )
    invalid_serialized = incidents.serialize_lifecycle_incident(invalid, device)
    assert invalid_serialized.summary_state == DeviceLifecyclePolicySummaryState.idle
    assert invalid_serialized.run_id is None
    assert invalid_serialized.backoff_until is None

    page = await LifecycleIncidentService().list_lifecycle_incidents_paginated(db_session, limit=1)
    assert [item.id for item in page.items] == [event.id]
    assert page.next_cursor is None
    assert page.prev_cursor is None

    newer_page = await LifecycleIncidentService().list_lifecycle_incidents_paginated(
        db_session,
        limit=1,
        cursor=encode_cursor(event.created_at, event.id),
        direction="newer",
    )
    # Nothing is strictly newer than the only event, using its own exact cursor.
    assert newer_page.items == []
    assert newer_page.next_cursor is None
    assert newer_page.prev_cursor is None

    with pytest.raises(CursorPaginationError):
        await LifecycleIncidentService().list_lifecycle_incidents_paginated(
            db_session,
            limit=1,
            cursor="not-a-date",
        )


async def test_lifecycle_incident_pagination_newer_direction_is_contiguous(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    """Regression test for an off-by-one: truncating to `limit` rows must happen before
    reversing for direction="newer", not after. Truncating after reversal keeps the
    `limit` rows *farthest* from the cursor instead of the nearest, which skips the row
    immediately newer than the cursor.
    """
    device = await create_device_record(
        db_session,
        host_id=db_host.id,
        identity_value="pagination-device-001",
        connection_target="pagination-device-001",
        name="Pagination Device",
    )
    base = datetime.now(UTC) - timedelta(minutes=10)
    events = [
        DeviceEvent(
            device_id=device.id,
            event_type=DeviceEventType.lifecycle_recovery_backoff,
            details={"summary_state": "backoff"},
            created_at=base + timedelta(minutes=index),
        )
        for index in range(8)
    ]
    db_session.add_all(events)
    await db_session.commit()
    for event in events:
        await db_session.refresh(event)

    service = LifecycleIncidentService()

    # Cursor sits at events[3]; four rows are newer (events[4..7]), but only limit+1=3 are
    # ever fetched (events[4..6]). The page must be the two rows closest to the cursor,
    # newest-first: [events[5], events[4]]. The pre-fix order (reverse-then-truncate)
    # would instead return [events[6], events[5]], skipping events[4] entirely.
    newer_page = await service.list_lifecycle_incidents_paginated(
        db_session,
        limit=2,
        cursor=encode_cursor(events[3].created_at, events[3].id),
        direction="newer",
    )
    assert [item.id for item in newer_page.items] == [events[5].id, events[4].id]

    # "older" from the same cursor is unaffected: the two rows closest to the cursor on
    # the older side (events[2], events[1]), newest-first.
    older_page = await service.list_lifecycle_incidents_paginated(
        db_session,
        limit=2,
        cursor=encode_cursor(events[3].created_at, events[3].id),
        direction="older",
    )
    assert [item.id for item in older_page.items] == [events[2].id, events[1].id]


async def test_pack_discovery_candidate_refresh_and_confirm_paths(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await create_device_record(
        db_session,
        host_id=db_host.id,
        identity_value="discovery-existing",
        connection_target="discovery-existing",
        name="Existing",
        os_version="13",
        software_versions={"build": "old"},
    )
    removed = await create_device_record(
        db_session,
        host_id=db_host.id,
        identity_value="discovery-removed",
        connection_target="discovery-removed",
        name="Removed",
    )
    candidates = [
        {
            "pack_id": "appium-uiautomator2",
            "platform_id": "android_mobile",
            "identity_scheme": "android_serial",
            "identity_scope": "host",
            "identity_value": "discovery-existing",
            "suggested_name": "Existing Updated",
            "detected_properties": {
                "connection_target": "discovery-existing",
                "os_version": "14",
                "manufacturer": "Acme",
                "model": "Model",
                "software_versions": {"build": "new"},
                "device_type": "real_device",
                "connection_type": "usb",
            },
            "runnable": True,
        },
        {
            "pack_id": "appium-uiautomator2",
            "platform_id": "android_mobile",
            "identity_scheme": "android_serial",
            "identity_scope": "host",
            "identity_value": "discovery-new",
            "suggested_name": "New Device",
            "detected_properties": {
                "connection_target": "discovery-new",
                "os_version": "15",
                "device_type": "real_device",
                "connection_type": "usb",
            },
            "runnable": False,
            "missing_requirements": ["adb"],
        },
    ]
    monkeypatch.setattr(
        "app.packs.services.discovery.platform_label_service.load_platform_label_map",
        AsyncMock(return_value={("appium-uiautomator2", "android_mobile"): "Android"}),
    )

    svc = PackDiscoveryService(
        agent_get_pack_devices=AsyncMock(return_value={"candidates": candidates}),
        circuit_breaker=Mock(),
        serializer=DevicePresenterService(),
        identity_guard=DeviceIdentityConflictService(),
    )

    target = HostTarget(
        host_id=db_host.id,
        hostname=db_host.hostname,
        ip=db_host.ip,
        agent_port=db_host.agent_port,
        current_boot_id=db_host.current_boot_id,
    )
    fetched = await svc.fetch_pack_candidates(target)

    intake = await svc.build_intake_candidates(db_session, target.host_id, fetched)
    assert [item.already_registered for item in intake] == [True, False]
    assert intake[0].platform_label == "Android"

    result = await svc.classify_discovery(db_session, target.host_id, fetched)
    assert [device.identity_value for device in result.updated_devices] == ["discovery-existing"]
    assert [device.identity_value for device in result.new_devices] == ["discovery-new"]
    assert result.removed_identity_values == ["discovery-removed"]

    monkeypatch.setattr(svc._identity_guard, "ensure_device_payload_identity_available", AsyncMock())
    # Confirmation classifies for itself, under the Host lock, from the same raw
    # candidate list — no caller-supplied DiscoveryResult to drift from it.
    confirm_result = await svc.confirm_discovery(
        db_session,
        target,
        fetched,
        ["discovery-new"],
        [removed.identity_value],
    )
    assert confirm_result.added == ["discovery-new"]
    assert confirm_result.removed == ["discovery-removed"]
    assert confirm_result.updated == ["discovery-existing"]


async def test_lifecycle_incidents_cursor_pages_do_not_skip_ties(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    """Four events share one created_at; paging by 2 must yield all four exactly once."""
    device = await create_device_record(
        db_session,
        host_id=db_host.id,
        identity_value="lifecycle-tie-1",
        connection_target="lifecycle-tie-1",
        name="Lifecycle Tie",
    )
    shared = datetime.now(UTC) - timedelta(minutes=5)
    db_session.add_all(
        [
            DeviceEvent(
                device_id=device.id,
                event_type=DeviceEventType.lifecycle_recovered,
                details={"summary_state": "idle"},
                created_at=shared,
            )
            for _ in range(4)
        ]
    )
    await db_session.commit()

    service = LifecycleIncidentService()
    seen: list[str] = []
    cursor: str | None = None
    for _ in range(4):  # bounded so a broken cursor loops finitely
        page = await service.list_lifecycle_incidents_paginated(
            db_session, limit=2, device_id=device.id, cursor=cursor, direction="older"
        )
        seen.extend(str(item.id) for item in page.items)
        if page.next_cursor is None:
            break
        cursor = page.next_cursor

    assert len(seen) == 4, f"expected 4 distinct events across pages, got {seen}"
    assert len(set(seen)) == 4, f"an event was returned on two pages: {seen}"


async def test_lifecycle_incidents_newer_page_reports_both_directions(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    """A 'newer' page in the middle of the range must offer BOTH an older and a newer cursor."""
    device = await create_device_record(
        db_session,
        host_id=db_host.id,
        identity_value="lifecycle-both-1",
        connection_target="lifecycle-both-1",
        name="Lifecycle Both",
    )
    base = datetime.now(UTC) - timedelta(minutes=30)
    events = [
        DeviceEvent(
            device_id=device.id,
            event_type=DeviceEventType.lifecycle_recovered,
            details={"summary_state": "idle"},
            created_at=base + timedelta(minutes=index),
        )
        for index in range(10)
    ]
    db_session.add_all(events)
    await db_session.commit()
    for event in events:
        await db_session.refresh(event)

    service = LifecycleIncidentService()
    page = await service.list_lifecycle_incidents_paginated(
        db_session,
        limit=3,
        device_id=device.id,
        cursor=encode_cursor(events[3].created_at, events[3].id),
        direction="newer",
    )

    # Rows strictly newer than events[3], nearest first, presented newest-first.
    assert [item.id for item in page.items] == [events[6].id, events[5].id, events[4].id]
    # events[7..9] are newer than this page -> a "Newer" cursor must exist.
    assert page.prev_cursor is not None
    # events[0..3] are older than this page -> an "Older" cursor must exist.
    assert page.next_cursor is not None

    # The discriminating position: a "newer" page that reaches the newest end. This is
    # the case the asymmetric-gating bug actually gets wrong -- the old code measured
    # has_more on the *newer* side (query direction) but gated next_cursor (the *older*
    # button) with it, and set prev_cursor unconditionally whenever a cursor was passed.
    newest_page = await service.list_lifecycle_incidents_paginated(
        db_session,
        limit=3,
        device_id=device.id,
        cursor=encode_cursor(events[6].created_at, events[6].id),
        direction="newer",
    )
    assert [item.id for item in newest_page.items] == [events[9].id, events[8].id, events[7].id]
    # Nothing is newer than events[9] -> no "Newer" cursor.
    assert newest_page.prev_cursor is None
    # events[0..6] are older than this page -> an "Older" cursor must exist.
    assert newest_page.next_cursor is not None
