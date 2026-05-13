from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.device_event import DeviceEvent, DeviceEventType
from app.models.host import Host
from app.schemas.device import DeviceLifecyclePolicySummaryState
from app.schemas.host import DiscoveredDevice, DiscoveryResult
from app.services import lifecycle_incident_service as incidents
from app.services import pack_discovery_service as discovery
from tests.helpers import create_device_record


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
    event = await incidents.record_lifecycle_incident(
        db_session,
        device,
        DeviceEventType.lifecycle_recovery_backoff,
        summary_state=DeviceLifecyclePolicySummaryState.recoverable,
        reason="adb offline",
        detail="recovery delayed",
        source="test",
        run_id=run_id,
        run_name="run",
        backoff_until=datetime.now(UTC) + timedelta(minutes=5),
        ttl_seconds=30,
        worker_id="worker-1",
        expires_at=datetime.now(UTC) + timedelta(minutes=1),
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

    items, next_cursor, prev_cursor = await incidents.list_lifecycle_incidents_paginated(db_session, limit=1)
    assert [item.id for item in items] == [event.id]
    assert next_cursor is None
    assert prev_cursor is None

    newer, _next, prev = await incidents.list_lifecycle_incidents_paginated(
        db_session,
        limit=1,
        cursor=(event.created_at - timedelta(seconds=1)).isoformat(),
        direction="newer",
    )
    assert [item.id for item in newer] == [event.id]
    assert prev is not None

    invalid_cursor_items, _next, _prev = await incidents.list_lifecycle_incidents_paginated(
        db_session,
        cursor="not-a-date",
    )
    assert invalid_cursor_items

    listed = await incidents.list_lifecycle_incidents(db_session, device_id=device.id)
    assert [item.id for item in listed] == [event.id]


async def test_pack_discovery_candidate_refresh_and_confirm_paths(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    existing = await create_device_record(
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
        "app.services.pack_discovery_service.platform_label_service.load_platform_label_map",
        AsyncMock(return_value={("appium-uiautomator2", "android_mobile"): "Android"}),
    )

    agent = AsyncMock()
    agent.get_pack_devices = AsyncMock(return_value={"candidates": candidates})
    discovered = await discovery.discover_pack_candidates(agent, host=db_host.ip, port=db_host.agent_port)
    assert len(discovered.candidates) == 2
    assert discovered.candidates[1].missing_requirements == ["adb"]

    intake = await discovery.list_intake_candidates(
        db_session,
        db_host,
        agent_get_pack_devices=AsyncMock(return_value={"candidates": candidates}),
    )
    assert [item.already_registered for item in intake] == [True, False]
    assert intake[0].platform_label == "Android"

    result = await discovery.discover_devices(
        db_session,
        db_host,
        agent_get_pack_devices=AsyncMock(return_value={"candidates": candidates}),
    )
    assert [device.identity_value for device in result.updated_devices] == ["discovery-existing"]
    assert [device.identity_value for device in result.new_devices] == ["discovery-new"]
    assert result.removed_identity_values == ["discovery-removed"]

    await discovery.refresh_device_properties(
        db_session,
        existing,
        agent_get_pack_device_properties=AsyncMock(
            return_value={"detected_properties": {"os_version": "16", "software_versions": {"build": "fresh"}}}
        ),
    )
    assert existing.os_version == "16"
    assert existing.software_versions == {"build": "fresh"}
    await discovery.refresh_device_properties(
        db_session,
        existing,
        agent_get_pack_device_properties=AsyncMock(return_value=None),
    )

    monkeypatch.setattr("app.services.pack_discovery_service.ensure_device_payload_identity_available", AsyncMock())
    confirm_result = await discovery.confirm_discovery(
        db_session,
        db_host,
        add_identity_values=["discovery-new"],
        remove_identity_values=[removed.identity_value],
        discovery_result=DiscoveryResult(
            new_devices=result.new_devices,
            updated_devices=[
                DiscoveredDevice(
                    **{
                        **result.updated_devices[0].model_dump(),
                        "os_version": "17",
                        "software_versions": {"build": "confirm"},
                    }
                )
            ],
            removed_identity_values=result.removed_identity_values,
        ),
    )
    assert confirm_result.added == ["discovery-new"]
    assert confirm_result.removed == ["discovery-removed"]
    assert confirm_result.updated == ["discovery-existing"]
