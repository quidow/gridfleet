"""Lock the gauge values reported by GET /metrics across phased refactors.

Regression guard introduced in Phase 0a of the backend domain-layout
refactor. Seeds four cross-domain gauges and asserts the HTTP route
reports the seeded values exactly. Every later phase (P5/P6/P13/P14)
that migrates a contributing domain into the registration-based
dispatcher must keep this test green.

Gauge names checked here are the Prometheus names (not the Python
constant names). `gridfleet_devices_in_cooldown` carries the project
prefix; the other three predate the prefix convention and stay
`pending_jobs`, `active_sessions`, `active_sse_connections`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from app.events import event_bus
from app.jobs.models import Job
from app.models.device_reservation import DeviceReservation
from app.models.session import Session, SessionStatus
from app.models.test_run import RunState, TestRun
from tests.helpers import create_device_record

if TYPE_CHECKING:
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession


@pytest.mark.db
async def test_metrics_route_reports_all_four_cross_domain_gauges(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    db_session.add(Job(kind="parity-test", payload={}, status="pending"))

    run = TestRun(
        name="Metrics Parity Run",
        state=RunState.active,
        requirements=[{"pack_id": "appium-uiautomator2", "platform_id": "android_mobile", "count": 1}],
        ttl_minutes=60,
        heartbeat_timeout_sec=120,
    )
    db_session.add(run)
    await db_session.flush()

    db_session.add(
        Session(
            session_id="parity-session-1",
            run_id=run.id,
            status=SessionStatus.running,
        )
    )

    cooldown_device = await create_device_record(
        db_session,
        host_id=default_host_id,
        identity_value="metrics-parity-cooldown",
        connection_target="metrics-parity-cooldown",
        name="Metrics Parity Cooldown",
        operational_state="available",
    )
    now = datetime.now(UTC)
    db_session.add(
        DeviceReservation(
            run=run,
            device_id=cooldown_device.id,
            identity_value=cooldown_device.identity_value,
            connection_target=cooldown_device.connection_target,
            pack_id=cooldown_device.pack_id,
            platform_id=cooldown_device.platform_id,
            os_version=cooldown_device.os_version,
            excluded=True,
            excluded_at=now,
            excluded_until=now + timedelta(seconds=60),
        )
    )
    await db_session.commit()

    event_bus.reset()
    subscriber = event_bus.subscribe()
    try:
        response = await client.get("/metrics")
        assert response.status_code == 200
        body = response.text

        assert "\npending_jobs 1.0\n" in body
        assert "\nactive_sessions 1.0\n" in body
        assert "\nactive_sse_connections 1.0\n" in body
        assert "\ngridfleet_devices_in_cooldown 1.0\n" in body
    finally:
        event_bus.unsubscribe(subscriber)
