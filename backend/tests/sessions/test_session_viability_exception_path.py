"""Regression: when the viability probe raises mid-run, the exception path must
mark the device dirty and reconcile, not use projection-based state writes.

After Task 10, the exception path calls IntentService.reconcile_now
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


async def test_exception_path_calls_reconcile_now(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After Task 10: exception path calls reconcile_now instead of
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
    monkeypatch.setattr(
        service_viability,
        "derive_operational_state",
        AsyncMock(side_effect=lambda _db, device, *, now: device.operational_state),
    )
    monkeypatch.setattr(service_viability, "is_ready_for_use_async", AsyncMock(return_value=True))
    monkeypatch.setattr(service_viability.device_locking, "lock_device", AsyncMock(return_value=locked))

    mark_dirty = AsyncMock()
    monkeypatch.setattr(
        "app.sessions.service_viability.IntentService",
        MagicMock(return_value=MagicMock(reconcile_now=mark_dirty)),
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

    # Exception paths leave the projection to the reconciler scan.
    mark_dirty.assert_not_awaited()


async def test_exception_path_from_offline_calls_mark_dirty(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exception path from offline state also calls reconcile_now."""
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
    monkeypatch.setattr(
        service_viability,
        "derive_operational_state",
        AsyncMock(side_effect=lambda _db, device, *, now: device.operational_state),
    )
    monkeypatch.setattr(service_viability, "is_ready_for_use_async", AsyncMock(return_value=True))
    monkeypatch.setattr(service_viability.device_locking, "lock_device", AsyncMock(return_value=locked))

    mark_dirty = AsyncMock()
    monkeypatch.setattr(
        "app.sessions.service_viability.IntentService",
        MagicMock(return_value=MagicMock(reconcile_now=mark_dirty)),
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

    mark_dirty.assert_not_awaited()


async def test_gating_failure_after_claim_still_releases_lock(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failure between claiming the probe lock and the probe body must still
    release the lock.

    The lock has no TTL, so a leak parks the device's viability checks until the
    5-minute stale-reclaim window and surfaces to operators as a spurious 409
    "probe already in progress" on a device running no probe. This models a
    transient failure / client-disconnect cancellation in the post-claim gating
    stage (readiness check), which previously ran outside the try/finally.
    """
    device_id = uuid.uuid4()
    available_device = MagicMock(
        id=device_id,
        operational_state=DeviceOperationalState.available,
        hold=None,
    )
    available_device.appium_node = MagicMock(observed_running=True)

    monkeypatch.setattr(service_viability.control_plane_state_store, "try_claim_value", AsyncMock(return_value=True))
    delete_value = AsyncMock()
    monkeypatch.setattr(service_viability.control_plane_state_store, "delete_value", delete_value)
    monkeypatch.setattr(
        service_viability,
        "derive_operational_state",
        AsyncMock(side_effect=lambda _db, device, *, now: device.operational_state),
    )
    # The readiness gate runs after the lock is claimed but before the probe body.
    # Blowing it up models a disconnect/transient failure in that window.
    monkeypatch.setattr(
        service_viability,
        "is_ready_for_use_async",
        AsyncMock(side_effect=RuntimeError("disconnect-in-gap")),
    )

    svc = SessionViabilityService(
        publisher=event_bus,
        settings=FakeSettingsReader({"general.session_viability_timeout_sec": 5}),
        session_factory=AsyncMock(),
        capability=DeviceCapabilityService(),
        health=AsyncMock(),
    )
    with pytest.raises(RuntimeError, match="disconnect-in-gap"):
        await svc.run_session_viability_probe(
            db_session,
            available_device,
            checked_by=service_viability.SessionViabilityCheckedBy.manual,
        )

    # The no-TTL lock MUST be released despite the gap failure.
    delete_value.assert_awaited()
