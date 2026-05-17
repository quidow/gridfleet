"""Backend tests for the device diagnostic export feature."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.devices.models import DeviceDiagnosticSnapshot, DeviceEvent, DeviceEventType
from app.devices.services.data_cleanup import _cleanup_old_data
from app.devices.services.diagnostics_export import assemble_bundle, capture_snapshot
from app.devices.services.review import mark_review_required
from app.hosts.models import Host
from app.sessions.models import Session, SessionStatus
from app.settings import settings_service
from tests.helpers import create_device

pytestmark = pytest.mark.usefixtures("seeded_driver_packs")


@pytest.mark.db
async def test_diagnostic_snapshot_persists_with_payload(
    db_session: AsyncSession,
    db_host: Host,
    seeded_driver_packs: None,
) -> None:
    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="snapshot-model-device",
        identity_value="model-test",
    )
    row = DeviceDiagnosticSnapshot(
        device_id=device.id,
        trigger="operator",
        reason="manual",
        payload={"schema_version": 1, "device": {"id": str(device.id)}},
    )
    db_session.add(row)
    await db_session.commit()

    result = await db_session.execute(
        select(DeviceDiagnosticSnapshot).where(DeviceDiagnosticSnapshot.device_id == device.id)
    )
    persisted = result.scalar_one()
    assert persisted.trigger == "operator"
    assert persisted.reason == "manual"
    assert persisted.payload["schema_version"] == 1
    assert persisted.captured_at is not None
    assert isinstance(persisted.id, uuid.UUID)
    assert isinstance(persisted.captured_at, datetime)
    assert persisted.captured_at.tzinfo is not None


@pytest.mark.db
async def test_diagnostic_snapshots_retention_setting_defaults_to_30(
    db_session: AsyncSession,
) -> None:
    del db_session
    value = settings_service.get("retention.diagnostic_snapshots_days")
    assert value == 30


@pytest.mark.db
async def test_assemble_bundle_minimal_device_emits_empty_arrays(
    db_session: AsyncSession,
    db_host: Host,
    seeded_driver_packs: None,
) -> None:
    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="minimal-device",
        identity_value="minimal",
    )
    bundle = await assemble_bundle(db_session, device, redact=False)
    assert bundle["schema_version"] == 1
    assert bundle["redacted"] is False
    assert bundle["device"]["id"] == str(device.id)
    assert bundle["appium_node"] is None
    assert bundle["reservations"] == []
    assert bundle["intents"] == []
    assert bundle["sessions"]["running"] == []
    assert bundle["sessions"]["recent_ended"] == []
    assert bundle["events"] == []
    assert bundle["related_runs"] == []
    assert bundle["agent_reconfigure_outbox"] == []
    assert "captured_at" in bundle


@pytest.mark.db
async def test_assemble_bundle_full_state_respects_caps_and_ordering(
    db_session: AsyncSession,
    db_host: Host,
    seeded_driver_packs: None,
) -> None:
    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="full-state-device",
        identity_value="full",
    )
    base = datetime.now(UTC) - timedelta(hours=1)
    for index in range(25):
        db_session.add(
            Session(
                device_id=device.id,
                session_id=f"sess-{index}",
                status=SessionStatus.failed,
                started_at=base + timedelta(minutes=index),
                ended_at=base + timedelta(minutes=index, seconds=30),
            )
        )
    for index in range(60):
        db_session.add(
            DeviceEvent(
                device_id=device.id,
                event_type=DeviceEventType.health_check_fail,
                details={"i": index},
                created_at=base + timedelta(seconds=index),
            )
        )
    await db_session.commit()
    bundle = await assemble_bundle(db_session, device, redact=False)
    assert len(bundle["sessions"]["recent_ended"]) == 20
    assert len(bundle["events"]) == 50
    ended_times = [session["ended_at"] for session in bundle["sessions"]["recent_ended"]]
    assert ended_times == sorted(ended_times, reverse=True)
    event_created = [event["created_at"] for event in bundle["events"]]
    assert event_created == sorted(event_created, reverse=True)


@pytest.mark.db
async def test_assemble_bundle_redaction_hashes_named_fields(
    db_session: AsyncSession,
    db_host: Host,
    seeded_driver_packs: None,
) -> None:
    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="redact-device",
        identity_value="emulator-5554",
        connection_target="emulator-5554",
        ip_address="10.0.0.42",
    )
    db_session.add(
        DeviceEvent(
            device_id=device.id,
            event_type=DeviceEventType.connectivity_lost,
            details={
                "identity_value": "emulator-5554",
                "nested": {"connection_target": "emulator-5554"},
                "device_id": str(device.id),
                "harmless": "keep-me",
            },
        )
    )
    await db_session.commit()

    unredacted = await assemble_bundle(db_session, device, redact=False)
    redacted = await assemble_bundle(db_session, device, redact=True)

    assert redacted["redacted"] is True
    assert redacted["device"]["identity_value"] != "emulator-5554"
    assert redacted["device"]["connection_target"] != "emulator-5554"
    assert redacted["device"]["ip_address"] != "10.0.0.42"
    assert redacted["device"]["name"] == "redact-device"
    event = redacted["events"][0]
    assert event["details"]["identity_value"] != "emulator-5554"
    assert event["details"]["nested"]["connection_target"] != "emulator-5554"
    assert event["details"]["device_id"] != str(device.id)
    assert event["details"]["harmless"] == "keep-me"
    redacted2 = await assemble_bundle(db_session, device, redact=True)
    assert redacted["device"]["identity_value"] == redacted2["device"]["identity_value"]
    assert unredacted["device"]["identity_value"] == "emulator-5554"


@pytest.mark.db
async def test_capture_snapshot_persists_row_with_trigger_and_reason(
    db_session: AsyncSession,
    db_host: Host,
    seeded_driver_packs: None,
) -> None:
    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="capture-device",
        identity_value="capture",
    )
    snapshot_id = await capture_snapshot(db_session, device, trigger="operator", reason="manual click")
    await db_session.commit()
    assert isinstance(snapshot_id, uuid.UUID)
    result = await db_session.execute(
        select(DeviceDiagnosticSnapshot).where(DeviceDiagnosticSnapshot.id == snapshot_id)
    )
    row = result.scalar_one()
    assert row.trigger == "operator"
    assert row.reason == "manual click"
    assert row.payload["schema_version"] == 1
    assert row.payload["redacted"] is False


@pytest.mark.db
async def test_mark_review_required_records_snapshot(
    db_session: AsyncSession,
    db_host: Host,
    seeded_driver_packs: None,
) -> None:
    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="auto-snapshot-device",
        identity_value="auto",
    )
    changed = await mark_review_required(
        db_session, device, reason="health_failure:threshold", source="recovery_loop"
    )
    await db_session.commit()
    assert changed is True
    result = await db_session.execute(
        select(DeviceDiagnosticSnapshot).where(DeviceDiagnosticSnapshot.device_id == device.id)
    )
    snapshots = result.scalars().all()
    assert len(snapshots) == 1
    snap = snapshots[0]
    assert snap.trigger == "review_required"
    assert snap.reason == "health_failure:threshold"
    assert snap.payload["device"]["review_required"] is True


@pytest.mark.db
async def test_mark_review_required_still_flips_flag_when_snapshot_fails(
    db_session: AsyncSession,
    db_host: Host,
    seeded_driver_packs: None,
) -> None:
    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="snapshot-fails-device",
        identity_value="fails",
    )
    with patch(
        "app.devices.services.review.diagnostics_export.capture_snapshot",
        side_effect=RuntimeError("forced failure"),
    ):
        changed = await mark_review_required(db_session, device, reason="forced", source="test")
        await db_session.commit()
    assert changed is True
    assert device.review_required is True
    result = await db_session.execute(
        select(DeviceDiagnosticSnapshot).where(DeviceDiagnosticSnapshot.device_id == device.id)
    )
    assert result.scalars().all() == []


@pytest.mark.db
async def test_data_cleanup_deletes_snapshots_past_retention(
    db_session: AsyncSession,
    db_host: Host,
    seeded_driver_packs: None,
) -> None:
    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="retention-device",
        identity_value="ret",
    )
    old = DeviceDiagnosticSnapshot(
        device_id=device.id,
        trigger="operator",
        reason=None,
        payload={"schema_version": 1},
        captured_at=datetime.now(UTC) - timedelta(days=60),
    )
    fresh = DeviceDiagnosticSnapshot(
        device_id=device.id,
        trigger="operator",
        reason=None,
        payload={"schema_version": 1},
        captured_at=datetime.now(UTC) - timedelta(days=1),
    )
    db_session.add_all([old, fresh])
    await db_session.commit()
    await _cleanup_old_data(db_session)
    result = await db_session.execute(
        select(DeviceDiagnosticSnapshot).where(DeviceDiagnosticSnapshot.device_id == device.id)
    )
    remaining = result.scalars().all()
    assert {row.id for row in remaining} == {fresh.id}


@pytest.mark.db
async def test_device_delete_cascades_diagnostic_snapshots(
    db_session: AsyncSession,
    db_host: Host,
    seeded_driver_packs: None,
) -> None:
    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="cascade-device",
        identity_value="cascade",
    )
    db_session.add(
        DeviceDiagnosticSnapshot(
            device_id=device.id,
            trigger="operator",
            reason=None,
            payload={"schema_version": 1},
        )
    )
    await db_session.commit()
    await db_session.delete(device)
    await db_session.commit()
    result = await db_session.execute(select(DeviceDiagnosticSnapshot))
    assert result.scalars().all() == []
