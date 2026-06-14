"""Regression: when the viability probe raises mid-run, the exception path must
mark the device dirty and reconcile, not use projection-based state writes.

After Task 10, the exception path calls IntentService.mark_dirty_and_reconcile
which derives state from durable facts (no running session → available/offline).
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.devices.models import DeviceOperationalState
from app.devices.services.capability import DeviceCapabilityService
from app.sessions import service_viability
from app.sessions.service_viability import SessionViabilityService
from tests.fakes import FakeSettingsReader
from tests.helpers import test_event_bus as event_bus

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.hosts.models import Host

pytestmark = [pytest.mark.db, pytest.mark.usefixtures("seeded_driver_packs")]


async def test_exception_path_calls_mark_dirty_and_reconcile(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After Task 10: exception path calls mark_dirty_and_reconcile instead of
    set_operational_state directly. No projection-as-write antipattern.
    """
    device_id = uuid.uuid4()
    available_device = MagicMock(
        id=device_id,
        operational_state=DeviceOperationalState.available,
        hold=None,
    )
    available_device.appium_node = MagicMock(observed_running=True)

    locked = MagicMock(id=device_id, operational_state=DeviceOperationalState.available, hold=None)

    monkeypatch.setattr(service_viability.control_plane_state_store, "try_claim_value", AsyncMock(return_value=True))
    monkeypatch.setattr(service_viability.control_plane_state_store, "delete_value", AsyncMock())
    monkeypatch.setattr(service_viability, "is_ready_for_use_async", AsyncMock(return_value=True))
    monkeypatch.setattr(service_viability.device_locking, "lock_device", AsyncMock(return_value=locked))

    mark_dirty = AsyncMock()
    monkeypatch.setattr(
        "app.sessions.service_viability.IntentService",
        MagicMock(return_value=MagicMock(mark_dirty_and_reconcile=mark_dirty)),
    )

    monkeypatch.setattr(
        DeviceCapabilityService,
        "get_device_capabilities",
        AsyncMock(side_effect=RuntimeError("probe-exploded")),
    )

    svc = SessionViabilityService(
        publisher=event_bus,
        settings=FakeSettingsReader({"general.session_viability_timeout_sec": 5}),
        session_factory=AsyncMock(),
        capability=DeviceCapabilityService(),
        health=AsyncMock(),
    )
    with pytest.raises(RuntimeError, match="probe-exploded"):
        await svc.run_session_viability_probe(
            db_session,
            available_device,
            checked_by=service_viability.SessionViabilityCheckedBy.manual,
        )

    # Exception path must have called mark_dirty_and_reconcile (not set_operational_state directly)
    mark_dirty.assert_awaited()


async def test_exception_path_from_offline_calls_mark_dirty(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exception path from offline state also calls mark_dirty_and_reconcile."""
    device_id = uuid.uuid4()
    offline_device = MagicMock(
        id=device_id,
        operational_state=DeviceOperationalState.offline,
        hold=None,
    )
    offline_device.appium_node = MagicMock(observed_running=True)

    locked = MagicMock(id=device_id, operational_state=DeviceOperationalState.offline, hold=None)

    monkeypatch.setattr(service_viability.control_plane_state_store, "try_claim_value", AsyncMock(return_value=True))
    monkeypatch.setattr(service_viability.control_plane_state_store, "delete_value", AsyncMock())
    monkeypatch.setattr(service_viability, "is_ready_for_use_async", AsyncMock(return_value=True))
    monkeypatch.setattr(service_viability.device_locking, "lock_device", AsyncMock(return_value=locked))

    mark_dirty = AsyncMock()
    monkeypatch.setattr(
        "app.sessions.service_viability.IntentService",
        MagicMock(return_value=MagicMock(mark_dirty_and_reconcile=mark_dirty)),
    )

    monkeypatch.setattr(
        DeviceCapabilityService,
        "get_device_capabilities",
        AsyncMock(side_effect=RuntimeError("probe-offline-exploded")),
    )
    svc = SessionViabilityService(
        publisher=event_bus,
        settings=FakeSettingsReader({"general.session_viability_timeout_sec": 5}),
        session_factory=AsyncMock(),
        capability=DeviceCapabilityService(),
        health=AsyncMock(),
    )
    with pytest.raises(RuntimeError, match="probe-offline-exploded"):
        await svc.run_session_viability_probe(
            db_session,
            offline_device,
            checked_by=service_viability.SessionViabilityCheckedBy.recovery,
        )

    mark_dirty.assert_awaited()
