from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent_comm.probe_result import ProbeResult
from app.devices.models import ConnectionType, Device, DeviceOperationalState, DeviceType
from app.hosts.models import Host
from app.sessions.models import Session, SessionStatus
from app.sessions.probe_constants import PROBE_TEST_NAME
from app.sessions.service_probes import (
    PROBE_CHECKED_BY_CAP_KEY,
    ProbeSource,
    map_probe_result_to_status,
    record_probe_session,
)


async def _make_device(db_session: AsyncSession, db_host: Host, suffix: str) -> Device:
    device = Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value=f"probe-{suffix}",
        connection_target=f"probe-{suffix}",
        name=f"Probe Device {suffix}",
        os_version="14",
        host_id=db_host.id,
        operational_state=DeviceOperationalState.available,
        verified_at=datetime.now(UTC),
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )
    db_session.add(device)
    await db_session.flush()
    return device


def test_probe_source_values() -> None:
    assert ProbeSource.scheduled.value == "scheduled"
    assert ProbeSource.manual.value == "manual"
    assert ProbeSource.recovery.value == "recovery"
    assert ProbeSource.node_health.value == "node_health"
    assert ProbeSource.verification.value == "verification"


def test_map_probe_result_to_status_ack() -> None:
    status, error_type = map_probe_result_to_status(ProbeResult(status="ack"))
    assert status is SessionStatus.passed
    assert error_type is None


def test_map_probe_result_to_status_refused() -> None:
    status, error_type = map_probe_result_to_status(ProbeResult(status="refused", detail="x"))
    assert status is SessionStatus.failed
    assert error_type == "probe_refused"


def test_map_probe_result_to_status_indeterminate() -> None:
    status, error_type = map_probe_result_to_status(ProbeResult(status="indeterminate", detail="x"))
    assert status is SessionStatus.error
    assert error_type == "probe_indeterminate"


@pytest.mark.db
async def test_record_probe_session_writes_terminal_row(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    device = await _make_device(db_session, db_host, "ack")
    attempted_at = datetime(2026, 5, 15, 12, 0, 0, tzinfo=UTC)
    written = await record_probe_session(
        db_session,
        device=device,
        attempted_at=attempted_at,
        result=ProbeResult(status="ack"),
        source=ProbeSource.scheduled,
        capabilities={"appium:platformName": "Android"},
    )
    await db_session.commit()
    assert written is not None

    row = (await db_session.execute(select(Session).where(Session.id == written.id))).scalar_one()
    assert row.test_name == PROBE_TEST_NAME
    assert row.status is SessionStatus.passed
    assert row.error_type is None
    assert row.error_message is None
    assert row.session_id.startswith("probe-")
    assert row.requested_capabilities is not None
    assert row.requested_capabilities[PROBE_CHECKED_BY_CAP_KEY] == "scheduled"
    assert row.started_at == attempted_at
    assert row.ended_at is not None
    assert row.run_id is None


@pytest.mark.db
async def test_record_probe_session_refused_writes_failed_row(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    device = await _make_device(db_session, db_host, "refused")
    written = await record_probe_session(
        db_session,
        device=device,
        attempted_at=datetime.now(UTC),
        result=ProbeResult(status="refused", detail="no slots"),
        source=ProbeSource.node_health,
        capabilities={},
    )
    await db_session.commit()
    assert written is not None
    assert written.status is SessionStatus.failed
    assert written.error_type == "probe_refused"
    assert written.error_message == "no slots"
    assert written.requested_capabilities is not None
    assert written.requested_capabilities[PROBE_CHECKED_BY_CAP_KEY] == "node_health"


@pytest.mark.db
async def test_record_probe_session_indeterminate_writes_error_row(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    device = await _make_device(db_session, db_host, "indeterminate")
    written = await record_probe_session(
        db_session,
        device=device,
        attempted_at=datetime.now(UTC),
        result=ProbeResult(status="indeterminate", detail="transport error"),
        source=ProbeSource.verification,
        capabilities={},
    )
    await db_session.commit()
    assert written is not None
    assert written.status is SessionStatus.error
    assert written.error_type == "probe_indeterminate"
    assert written.error_message == "transport error"
