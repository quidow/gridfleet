from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock

import pytest

from app.devices.models import DeviceOperationalState
from app.devices.services.state import emit_operational_state_transition
from app.events.protocols import EventPublisher
from tests.helpers import create_device_record, create_host, settle_after_commit_tasks
from tests.helpers import test_event_bus as event_bus
from tests.packs.factories import seed_test_packs

if TYPE_CHECKING:
    from httpx2 import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession


@pytest.mark.db
async def test_emit_operational_state_transition_writes_and_emits(
    client: AsyncClient,
    db_session: AsyncSession,
    event_bus_capture: list[tuple[str, dict[str, Any]]],
) -> None:
    """The edge detector advances the ledger and queues an event when diverged."""
    await seed_test_packs(db_session)
    host = await create_host(client)
    # Device persisted as offline but facts derive to available (ready, verified, no session).
    device = await create_device_record(
        db_session,
        host_id=host["id"],
        identity_value="apply-mismatch-01",
        name="Apply Mismatch Device",
        operational_state=DeviceOperationalState.offline,
        verified=True,
    )
    event_bus_capture.clear()

    changed = await emit_operational_state_transition(db_session, device, now=datetime.now(UTC), publisher=event_bus)

    assert changed is True
    # Column was updated in-memory by the sanctioned writer.
    assert device.operational_state_last_emitted is DeviceOperationalState.available

    # Commit so the after_commit hook fires and publish is invoked.
    await db_session.commit()
    await settle_after_commit_tasks()

    names = [name for name, _ in event_bus_capture]
    assert "device.operational_state_changed" in names


@pytest.mark.db
async def test_emit_operational_state_transition_no_op_when_state_matches(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """The edge detector returns False when the ledger already matches."""
    await seed_test_packs(db_session)
    host = await create_host(client)
    device = await create_device_record(
        db_session,
        host_id=host["id"],
        identity_value="apply-match-01",
        name="Apply Match Device",
        operational_state=DeviceOperationalState.available,
        verified=True,
    )

    publisher = AsyncMock(spec=EventPublisher)
    changed = await emit_operational_state_transition(db_session, device, now=datetime.now(UTC), publisher=publisher)

    assert changed is False
    assert device.operational_state_last_emitted is DeviceOperationalState.available

    await db_session.commit()
    await settle_after_commit_tasks()

    publisher.publish.assert_not_called()
