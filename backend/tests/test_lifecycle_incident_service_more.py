from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from app.devices.models import Device, DeviceEvent, DeviceEventType
from app.devices.services import lifecycle_incidents as incidents


def test_lifecycle_incident_serialization_parse_fallbacks() -> None:
    device = Device(
        id=__import__("uuid").uuid4(),
        name="Incident Device",
        identity_value="incident-1",
        platform_id="android_mobile",
    )
    event = DeviceEvent(
        id=__import__("uuid").uuid4(),
        device_id=device.id,
        event_type=DeviceEventType.lifecycle_recovery_failed,
        created_at=datetime(2026, 5, 13, tzinfo=UTC),
        details={
            "summary_state": "not-a-state",
            "run_id": "not-a-uuid",
            "backoff_until": "not-a-date",
            "expires_at": datetime(2026, 5, 13, tzinfo=UTC),
        },
    )

    read = incidents.serialize_lifecycle_incident(event, device)

    assert read.summary_state == incidents.DeviceLifecyclePolicySummaryState.idle
    assert read.run_id is None
    assert read.backoff_until is None
    assert incidents._parse_summary_state(incidents.DeviceLifecyclePolicySummaryState.recoverable) == (
        incidents.DeviceLifecyclePolicySummaryState.recoverable
    )


async def test_record_lifecycle_incident_serializes_optional_detail_branches(monkeypatch: pytest.MonkeyPatch) -> None:
    device = Device(id=__import__("uuid").uuid4())
    record = AsyncMock(return_value=object())
    monkeypatch.setattr(incidents, "record_event", record)

    await incidents.record_lifecycle_incident(
        object(),  # type: ignore[arg-type]
        device,
        DeviceEventType.lifecycle_run_cooldown_set,
        summary_state=incidents.DeviceLifecyclePolicySummaryState.backoff,
        backoff_until=datetime(2026, 5, 13, tzinfo=UTC),
        ttl_seconds=60,
        worker_id="worker-1",
        expires_at="later",
    )

    details = record.await_args.args[3]
    assert details["backoff_until"].startswith("2026-05-13")
    assert details["expires_at"] == "later"
