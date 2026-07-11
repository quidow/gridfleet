"""The exhausted-retries run-create 409 must explain WHICH gate excluded the
candidates, not just report "matched 0".

The allocator's eligibility predicate is stricter than the UI's "available"
state: active reservations and Appium-node viability are invisible on the
operational-state axis, so a shortfall caused by them looks like a phantom
("the dashboard says the devices are free"). The diagnostic breakdown names
the gate — and for reservations, the blocking run — so the failure is
actionable from the error message alone.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from app.devices.models import DeviceOperationalState
from app.runs import service_allocator
from app.runs.schemas import RunCreate
from app.runs.service_allocator import RunAllocatorService
from tests.conftest import test_circuit_breaker
from tests.fakes import FakeSettingsReader
from tests.helpers import create_device
from tests.helpers import test_event_bus as event_bus

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.usefixtures("seeded_driver_packs")

_allocator_svc = RunAllocatorService(
    publisher=event_bus,
    settings=FakeSettingsReader({}),
    circuit_breaker=test_circuit_breaker,
)


async def test_shortfall_409_names_blocking_reservation_and_run(
    db_session: AsyncSession,
    default_host_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(service_allocator, "_MATCH_RETRY_BACKOFF_SEC", 0.0)
    device = await create_device(
        db_session,
        host_id=default_host_id,
        name="shortfall-reserved",
        operational_state=DeviceOperationalState.available,
        verified=True,
    )
    await db_session.commit()
    requirement = {"pack_id": device.pack_id, "platform_id": device.platform_id, "count": 1}

    first_run, _ = await _allocator_svc.create_run(db_session, RunCreate(name="holder-run", requirements=[requirement]))
    # The starved create_run below rolls back between match retries, expiring
    # this instance — snapshot the id while it is still live.
    first_run_id = str(first_run.id)

    with pytest.raises(ValueError, match="Not enough devices") as excinfo:
        await _allocator_svc.create_run(db_session, RunCreate(name="starved-run", requirements=[requirement]))

    message = str(excinfo.value)
    assert "held by active reservation" in message
    assert first_run_id in message, "the blocking run id must be named so the failure is traceable"


async def test_shortfall_409_reports_operational_state_breakdown(
    db_session: AsyncSession,
    default_host_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(service_allocator, "_MATCH_RETRY_BACKOFF_SEC", 0.0)
    device = await create_device(
        db_session,
        host_id=default_host_id,
        name="shortfall-offline",
        # operational_state is a read-time projection: a failed viability fact
        # makes the verified device derive ``offline`` for the breakdown count.
        operational_state=DeviceOperationalState.offline,
        session_viability_status="failed",
        verified=True,
    )
    await db_session.commit()

    with pytest.raises(ValueError, match="Not enough devices") as excinfo:
        await _allocator_svc.create_run(
            db_session,
            RunCreate(
                name="starved-run",
                requirements=[{"pack_id": device.pack_id, "platform_id": device.platform_id, "count": 1}],
            ),
        )

    assert "1 in state offline" in str(excinfo.value)


async def test_shortfall_409_reports_no_candidates_at_all(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(service_allocator, "_MATCH_RETRY_BACKOFF_SEC", 0.0)
    # The seeded packs are only flushed by the fixture; the allocator's
    # retry rollback must not wipe them out from under assert_runnable.
    await db_session.commit()

    with pytest.raises(ValueError, match="Not enough devices") as excinfo:
        await _allocator_svc.create_run(
            db_session,
            RunCreate(
                name="starved-run",
                requirements=[{"pack_id": "appium-uiautomator2", "platform_id": "android_mobile", "count": 1}],
            ),
        )

    assert "no devices are configured" in str(excinfo.value)
