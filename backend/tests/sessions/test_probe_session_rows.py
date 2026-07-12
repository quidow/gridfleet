"""Probe birth-row lifecycle (WS-16.1): claim → confirm → finalize, guarded
against resurrection, and silent on the event stream."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from app.agent_comm.probe_result import ProbeResult
from app.sessions.models import Session, SessionStatus
from app.sessions.probe_constants import PROBE_TEST_NAME
from app.sessions.service import close_running_session
from app.sessions.service_probes import (
    PROBE_CHECKED_BY_CAP_KEY,
    ProbeSource,
    claim_probe_session,
    confirm_probe_session,
    finalize_probe_session,
)
from app.sessions.viability_types import SessionViabilityProbeInProgressError
from tests.helpers import create_device

if TYPE_CHECKING:
    import asyncio

    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy.orm import Session as OrmSession

    from app.events.catalog import EventSeverity
    from app.hosts.models import Host


class _RecordingPublisher:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def queue_for_session(
        self,
        _db: AsyncSession | OrmSession,
        event_type: str,
        data: dict[str, Any],
        *,
        severity: EventSeverity | None = None,
    ) -> None:
        self.events.append((event_type, data))

    async def publish(self, event_type: str, data: dict[str, Any], *, severity: EventSeverity | None = None) -> None:
        self.events.append((event_type, data))

    def track_task(self, _task: asyncio.Task[None]) -> None:
        pass


async def _claim(db: AsyncSession, host: Host, name: str) -> Session:
    device = await create_device(db, host_id=host.id, name=name, verified=True)
    row = await claim_probe_session(
        db,
        device=device,
        source=ProbeSource.scheduled,
        capabilities={"platformName": "Android"},
        router_target="http://probe-target:4723",
    )
    await db.commit()
    return row


async def _attach_device(db: AsyncSession, row: Session) -> None:
    from sqlalchemy import select

    from app.devices.models import Device

    row.device = (await db.execute(select(Device).where(Device.id == row.device_id))).scalar_one()


async def test_claim_confirm_finalize_lifecycle(db_session: AsyncSession, db_host: Host) -> None:
    row = await _claim(db_session, db_host, "probe-row-lifecycle")
    assert row.status == SessionStatus.pending
    assert row.test_name == PROBE_TEST_NAME
    assert row.session_id.startswith("probe-")
    assert row.requested_capabilities is not None
    assert row.requested_capabilities[PROBE_CHECKED_BY_CAP_KEY] == "scheduled"
    assert row.run_id is None
    assert row.ticket_id is None
    assert row.router_target == "http://probe-target:4723"

    assert await confirm_probe_session(db_session, row, appium_session_id="real-appium-id") is True
    await db_session.commit()
    assert row.status == SessionStatus.running
    assert row.session_id == "real-appium-id"

    assert await finalize_probe_session(db_session, row, result=ProbeResult(status="ack")) is True
    await db_session.commit()
    assert row.status == SessionStatus.passed
    assert row.ended_at is not None


async def test_claim_conflicts_with_live_probe_row(db_session: AsyncSession, db_host: Host) -> None:
    from sqlalchemy import select

    from app.devices.models import Device

    row = await _claim(db_session, db_host, "probe-row-conflict")
    assert row.device_id is not None
    device = (await db_session.execute(select(Device).where(Device.id == row.device_id))).scalar_one()
    with pytest.raises(SessionViabilityProbeInProgressError):
        await claim_probe_session(
            db_session,
            device=device,
            source=ProbeSource.manual,
            capabilities={},
            router_target=None,
        )


async def test_confirm_does_not_resurrect_a_lost_claim(db_session: AsyncSession, db_host: Host) -> None:
    """A pending claim terminalized out from under a slow create (the allocation
    reaper past grid.claim_window_sec) must not be revived by the late confirm;
    the probe's Appium session then converges through the orphan sweep."""
    row = await _claim(db_session, db_host, "probe-row-lost-claim")
    assert await finalize_probe_session(
        db_session, row, result=ProbeResult(status="indeterminate", detail="claim reaped")
    )
    await db_session.commit()
    assert await confirm_probe_session(db_session, row, appium_session_id="late-id") is False
    assert row.status == SessionStatus.error
    assert row.session_id != "late-id"


async def test_finalize_noop_after_external_close(db_session: AsyncSession, db_host: Host) -> None:
    row = await _claim(db_session, db_host, "probe-row-external-close")
    assert await confirm_probe_session(db_session, row, appium_session_id="closed-elsewhere")
    await db_session.commit()
    await _attach_device(db_session, row)
    publisher = _RecordingPublisher()
    await close_running_session(db_session, row, attached_run=None, publisher=publisher)
    await db_session.commit()
    assert row.ended_at is not None
    assert await finalize_probe_session(db_session, row, result=ProbeResult(status="ack")) is False


async def test_sweep_close_of_probe_row_emits_no_session_ended(db_session: AsyncSession, db_host: Host) -> None:
    """Probes never emit session.started, so the shared close path must not emit
    an unpaired session.ended for a crash-orphaned probe row (WS-16.1)."""
    row = await _claim(db_session, db_host, "probe-row-silent-close")
    assert await confirm_probe_session(db_session, row, appium_session_id="probe-crash-id")
    await db_session.commit()
    await _attach_device(db_session, row)
    publisher = _RecordingPublisher()
    await close_running_session(db_session, row, attached_run=None, publisher=publisher)
    await db_session.commit()
    assert row.ended_at is not None
    assert [event for event in publisher.events if event[0].startswith("session.")] == []
