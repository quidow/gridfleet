from datetime import UTC, datetime

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.devices.models import DeviceOperationalState
from app.devices.services.state_derivation import SHADOW_STATE_MISMATCH, compare_shadow_state
from tests.helpers import create_device_record, create_host
from tests.pack.factories import seed_test_packs


def test_shadow_metric_exists() -> None:
    # Counter with labels for which axis diverged.
    SHADOW_STATE_MISMATCH.labels(axis="operational").inc(0)
    SHADOW_STATE_MISMATCH.labels(axis="hold").inc(0)


@pytest.mark.db
async def test_compare_logs_mismatch_without_writing(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """compare_shadow_state detects divergence and returns True without mutating columns."""
    await seed_test_packs(db_session)
    host = await create_host(client)
    # Persist the device with operational_state=offline so that the DB row says offline,
    # but all facts (ready, verified, no sessions, no stop-in-flight) derive to available.
    # This is a genuine persisted divergence — no in-memory tricks needed.
    device = await create_device_record(
        db_session,
        host_id=host["id"],
        identity_value="shadow-mismatch-01",
        name="Shadow Mismatch Device",
        operational_state=DeviceOperationalState.offline,
        verified=True,
    )

    before_op = device.operational_state
    before_hold = device.hold

    mismatched = await compare_shadow_state(db_session, device, now=datetime.now(UTC))

    assert mismatched is True
    # Columns untouched — shadow mode writes nothing.
    assert device.operational_state is before_op
    assert device.hold is before_hold


@pytest.mark.db
async def test_compare_returns_false_when_state_matches(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """compare_shadow_state returns False when persisted state matches derivation."""
    await seed_test_packs(db_session)
    host = await create_host(client)
    # available + no hold — derivation produces the same values, so no mismatch.
    device = await create_device_record(
        db_session,
        host_id=host["id"],
        identity_value="shadow-match-01",
        name="Shadow Match Device",
        operational_state=DeviceOperationalState.available,
        verified=True,
    )

    result = await compare_shadow_state(db_session, device, now=datetime.now(UTC))

    assert result is False
