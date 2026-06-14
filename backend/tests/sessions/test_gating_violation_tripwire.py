"""The GATING_VIOLATION tripwire: a running session registering on a device whose
operational state forbids new sessions must increment
``gridfleet_device_state_gating_violation_total{kind="session_on_non_available"}``.

Detection only — registration behavior is unchanged (the state derivation absorbs the
breach into ``busy``, which is exactly why the counter is the only witness)."""

from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.devices.models import DeviceOperationalState
from app.devices.services.state import GATING_VIOLATION
from app.sessions.models import SessionStatus
from app.sessions.service import SessionCrudService
from tests.helpers import seed_host_and_device
from tests.helpers import test_event_bus as event_bus

pytestmark = [pytest.mark.db, pytest.mark.usefixtures("seeded_driver_packs")]


def _tripwire_value() -> float:
    return GATING_VIOLATION.labels(kind="session_on_non_available")._value.get()


@pytest.mark.parametrize(
    "state",
    [
        DeviceOperationalState.maintenance,
        DeviceOperationalState.offline,
        DeviceOperationalState.verifying,
    ],
)
async def test_running_registration_on_forbidden_state_increments_tripwire(
    db_session: AsyncSession,
    state: DeviceOperationalState,
) -> None:
    _, device = await seed_host_and_device(db_session, identity=f"tripwire-{state.value}", operational_state=state)
    crud = SessionCrudService(publisher=event_bus, lifecycle=AsyncMock())
    before = _tripwire_value()
    session = await crud.register_session(
        db_session,
        session_id=f"ssn-tripwire-{state.value}",
        test_name="tripwire",
        device_id=device.id,
        status=SessionStatus.running,
    )
    assert _tripwire_value() == before + 1
    # Detection only: the registration itself proceeds unchanged.
    assert session.status is SessionStatus.running


async def test_running_registration_on_available_device_does_not_increment(
    db_session: AsyncSession,
) -> None:
    _, device = await seed_host_and_device(db_session, identity="tripwire-clean")
    crud = SessionCrudService(publisher=event_bus, lifecycle=AsyncMock())
    before = _tripwire_value()
    await crud.register_session(
        db_session,
        session_id="ssn-tripwire-clean",
        test_name="tripwire",
        device_id=device.id,
        status=SessionStatus.running,
    )
    assert _tripwire_value() == before


async def test_terminal_registration_on_non_available_does_not_increment(
    db_session: AsyncSession,
) -> None:
    # A late-registered TERMINAL session (status != running) is bookkeeping, not an
    # allocation landing — it must not trip the wire.
    _, device = await seed_host_and_device(
        db_session, identity="tripwire-terminal", operational_state=DeviceOperationalState.offline
    )
    crud = SessionCrudService(publisher=event_bus, lifecycle=AsyncMock())
    before = _tripwire_value()
    await crud.register_session(
        db_session,
        session_id="ssn-tripwire-terminal",
        test_name="tripwire",
        device_id=device.id,
        status=SessionStatus.passed,
    )
    assert _tripwire_value() == before
