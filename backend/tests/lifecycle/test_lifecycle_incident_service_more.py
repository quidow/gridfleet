import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock

import pytest

from app.devices.models import Device, DeviceEvent, DeviceEventType
from app.devices.schemas.device import DeviceLifecyclePolicySummaryState
from app.lifecycle.services import incidents
from app.lifecycle.services.incidents import LifecycleIncidentService


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

    await LifecycleIncidentService().record_lifecycle_incident(
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


async def test_record_lifecycle_incident_publishes_sse_when_publisher_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F1: a recorded lifecycle incident is also published to the event bus so operators get
    a live signal (not just a row in the device_events audit table)."""
    device = Device(id=uuid.uuid4(), name="Incident Device", identity_value="incident-sse")
    run_id = uuid.uuid4()
    monkeypatch.setattr(incidents, "record_event", AsyncMock(return_value=object()))
    publisher = Mock()
    publisher.queue_for_session = Mock()
    db = object()

    await LifecycleIncidentService(publisher=publisher).record_lifecycle_incident(
        db,  # type: ignore[arg-type]
        device,
        DeviceEventType.lifecycle_recovery_failed,
        summary_state=DeviceLifecyclePolicySummaryState.backoff,
        reason="probe failed",
        detail="auto-stopped",
        source="session_viability",
        run_id=run_id,
        run_name="nightly",
    )

    publisher.queue_for_session.assert_called_once()
    call = publisher.queue_for_session.call_args
    assert call.args[0] is db
    assert call.args[1] == "device.lifecycle_incident"
    payload = call.args[2]
    assert payload["device_id"] == str(device.id)
    assert payload["event_type"] == DeviceEventType.lifecycle_recovery_failed.value
    assert payload["summary_state"] == DeviceLifecyclePolicySummaryState.backoff.value
    assert payload["reason"] == "probe failed"
    assert payload["run_id"] == str(run_id)
    # recovery_failed is operator-actionable -> critical severity
    assert call.kwargs["severity"] == "critical"


async def test_record_lifecycle_incident_no_publish_without_publisher(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Backward compatibility: the publisher is optional (88 no-arg construction sites). With
    no publisher the recorder still writes the audit row and never attempts to publish."""
    device = Device(id=uuid.uuid4(), name="Incident Device", identity_value="incident-nopub")
    record = AsyncMock(return_value=object())
    monkeypatch.setattr(incidents, "record_event", record)

    await LifecycleIncidentService().record_lifecycle_incident(
        object(),  # type: ignore[arg-type]
        device,
        DeviceEventType.lifecycle_recovered,
        summary_state=DeviceLifecyclePolicySummaryState.idle,
    )

    record.assert_awaited_once()
