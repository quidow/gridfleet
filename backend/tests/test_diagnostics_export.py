"""Backend tests for the device diagnostic export feature."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.devices.models import DeviceDiagnosticSnapshot, DeviceEvent, DeviceEventType
from app.devices.services.diagnostics_export import assemble_bundle
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
