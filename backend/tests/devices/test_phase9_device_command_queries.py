"""Phase 9: per-device command transactions stay linear in the fleet size.

The bulk commands now open one transaction per device instead of one shared
transaction, so the interesting risk is no longer lock scope but query growth: a
preload that turned into an N+1, or a summary event emitted per device instead of
once. These tests measure the real statement count at 1, 10, and 50 devices and
pin the slope.
"""

from __future__ import annotations

import math
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.devices.models import DeviceOperationalState
from app.devices.services.bulk import BulkOperationsService
from app.devices.services.identity_conflicts import DeviceIdentityConflictService
from app.devices.services.maintenance import MaintenanceService
from app.devices.services.service import DeviceCrudService
from app.events.models import SystemEvent
from app.lifecycle.services.operator_node import OperatorNodeLifecycleService
from tests.concurrency.group_lock_helpers import capture_statements
from tests.fakes import FakeSettingsReader, build_review_service
from tests.helpers import create_device
from tests.helpers import test_event_bus as event_bus

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from app.devices.models import Device
    from app.hosts.models import Host

pytestmark = [pytest.mark.asyncio, pytest.mark.db, pytest.mark.usefixtures("seeded_driver_packs")]

FLEET_SIZES = (1, 10, 50)


class StatementRecordingFactory:
    """A ``SessionFactory`` that records every statement its sessions issue.

    ``capture_statements`` pins its listener to one session's connection, which
    is exactly right for a single-session command and useless for a command that
    opens one session per device. This wraps the real factory so each handed-out
    session is recorded through that same helper and the counts accumulate into
    one list.
    """

    def __init__(self, factory: async_sessionmaker[AsyncSession], statements: list[str]) -> None:
        self._factory = factory
        self._statements = statements

    @asynccontextmanager
    async def __call__(self) -> AsyncIterator[AsyncSession]:
        async with self._factory() as session, capture_statements(session) as recorded:
            try:
                yield session
            finally:
                self._statements.extend(recorded)

    @asynccontextmanager
    async def begin(self) -> AsyncIterator[AsyncSession]:
        async with self._factory() as session, capture_statements(session) as recorded:
            try:
                async with session.begin():
                    yield session
            finally:
                self._statements.extend(recorded)


@pytest_asyncio.fixture
async def command_session_factory(db_session: AsyncSession) -> async_sessionmaker[AsyncSession]:
    """A second factory on the test schema, standing in for the container's own."""
    assert db_session.bind is not None
    return async_sessionmaker(db_session.bind, class_=AsyncSession, expire_on_commit=False)


def _bulk_service(session_factory: object) -> BulkOperationsService:
    settings = FakeSettingsReader()
    return BulkOperationsService(
        publisher=event_bus,
        settings=settings,
        circuit_breaker=MagicMock(),
        maintenance=MaintenanceService(
            review=build_review_service(),
            settings=settings,
            publisher=event_bus,
            session_factory=session_factory,  # type: ignore[arg-type]
        ),
        crud=DeviceCrudService(identity=DeviceIdentityConflictService(), publisher=event_bus),
        operator=OperatorNodeLifecycleService(review=build_review_service(), settings=settings, publisher=event_bus),
        session_factory=session_factory,  # type: ignore[arg-type]
    )


async def _seed_fleet(db_session: AsyncSession, host: Host, *, count: int, prefix: str) -> list[Device]:
    devices = [
        await create_device(
            db_session,
            host_id=host.id,
            name=f"{prefix}-{index}",
            operational_state=DeviceOperationalState.available,
            verified=True,
        )
        for index in range(count)
    ]
    await db_session.commit()
    return devices


async def _summary_event_count(db_session: AsyncSession) -> int:
    total = await db_session.scalar(
        select(func.count()).select_from(SystemEvent).where(SystemEvent.type == "bulk.operation_completed")
    )
    return total or 0


def _assert_linear(counts: dict[int, int], *, label: str, baseline: tuple[int, int, int]) -> float:
    """Each extra device may add at most what the single-device command costs.

    *baseline* is the tuple measured when this test was written, carried in the
    failure message so a regression reads as a delta rather than a bare number.
    """
    q1, q10, q50 = counts[1], counts[10], counts[50]
    per_device = (q10 - q1) / 9
    budget = q1 + 49 * math.ceil(per_device)
    assert q1 > 0, f"{label}: nothing was recorded, the measurement is vacuous ({counts})"
    assert q50 <= budget, (
        f"{label}: statement count grew faster than one single-device transaction per device. "
        f"observed [q1, q10, q50] = [{q1}, {q10}, {q50}], per_device = {per_device:.2f}, budget = {budget}; "
        f"baseline when written = {list(baseline)}"
    )
    return per_device


async def test_bulk_enter_maintenance_statement_count_stays_linear(
    db_session: AsyncSession,
    db_host: Host,
    command_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    counts: dict[int, int] = {}
    for size in FLEET_SIZES:
        devices = await _seed_fleet(db_session, db_host, count=size, prefix=f"q-maint-{size}")
        statements: list[str] = []
        service = _bulk_service(StatementRecordingFactory(command_session_factory, statements))
        result = await service.bulk_enter_maintenance([device.id for device in devices])
        assert result["succeeded"] == size, result
        counts[size] = len(statements)

    _assert_linear(counts, label="bulk_enter_maintenance", baseline=(12, 111, 551))


async def test_bulk_start_nodes_statement_count_stays_linear(
    db_session: AsyncSession,
    db_host: Host,
    command_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    counts: dict[int, int] = {}
    for size in FLEET_SIZES:
        devices = await _seed_fleet(db_session, db_host, count=size, prefix=f"q-start-{size}")
        statements: list[str] = []
        service = _bulk_service(StatementRecordingFactory(command_session_factory, statements))
        result = await service.bulk_start_nodes([device.id for device in devices])
        assert result["succeeded"] == size, result
        counts[size] = len(statements)

    _assert_linear(counts, label="bulk_start_nodes", baseline=(31, 301, 1501))


async def test_bulk_enter_maintenance_emits_one_summary_event_for_any_fleet_size(
    db_session: AsyncSession,
    db_host: Host,
    command_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """One ``bulk.operation_completed`` row per command, not one per device."""
    devices = await _seed_fleet(db_session, db_host, count=10, prefix="q-summary")
    before = await _summary_event_count(db_session)

    await _bulk_service(command_session_factory).bulk_enter_maintenance([device.id for device in devices])

    after = await _summary_event_count(db_session)
    assert after - before == 1, f"expected exactly one summary insert for 10 devices, got {after - before}"
