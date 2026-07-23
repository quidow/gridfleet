"""Regression: when the viability probe raises mid-run, the exception path must
mark the device dirty and reconcile, not use projection-based state writes.

After Task 10, the exception path calls IntentService.reconcile_now
which derives state from durable facts (no running session → available/offline).

After Task 3, the probe owns its own fresh sessions per phase (prepare/confirm/
finalize/escalate). An exception in ``_prepare_probe`` (readiness gate or
capability load) propagates out before the finalize/escalate phases run, so no
reconcile is issued and no probe row is claimed — the exception path leaves the
device state to the next reconciler scan.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.appium_nodes.models import AppiumDesiredState, AppiumNode
from app.devices.models import DeviceOperationalState
from app.devices.services.capability import DeviceCapabilityService
from app.sessions import service_viability
from app.sessions.service_viability import SessionViabilityService
from tests.fakes import FakeSettingsReader
from tests.helpers import create_device
from tests.helpers import test_event_bus as event_bus

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.devices.models import Device
    from app.hosts.models import Host

pytestmark = [pytest.mark.db, pytest.mark.usefixtures("seeded_driver_packs")]


def _running_node(device: Device) -> AppiumNode:
    return AppiumNode(
        device_id=device.id,
        port=4723,
        desired_state=AppiumDesiredState.running,
        desired_port=4723,
        pid=12345,
        active_connection_target=device.connection_target,
        health_running=True,
        health_state="up",
    )


async def test_exception_path_calls_reconcile_now(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After Task 10: exception path calls reconcile_now instead of
    set_operational_state directly. No projection-as-write antipattern.
    """
    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="exc-available",
        operational_state=DeviceOperationalState.available,
        verified=True,
    )
    db_session.add(_running_node(device))
    await db_session.commit()

    monkeypatch.setattr(service_viability, "is_ready_for_use_async", AsyncMock(return_value=True))

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
        session_factory=db_session_maker,
        capability=DeviceCapabilityService(),
        health=AsyncMock(),
    )
    with pytest.raises(RuntimeError, match="probe-exploded"):
        await svc.run_session_viability_probe(
            device.id,
            checked_by=service_viability.SessionViabilityCheckedBy.manual,
        )

    # Exception paths leave the projection to the reconciler scan.
    mark_dirty.assert_not_awaited()


async def test_exception_path_from_offline_calls_mark_dirty(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exception path from offline state also calls reconcile_now."""
    # An unverified device with a running node derives ``offline`` (not ready)
    # but still passes the node check, so a recovery probe reaches the
    # capability load before exploding.
    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="exc-offline",
        operational_state=DeviceOperationalState.offline,
        verified=False,
    )
    db_session.add(_running_node(device))
    await db_session.commit()

    monkeypatch.setattr(service_viability, "is_ready_for_use_async", AsyncMock(return_value=True))

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
        session_factory=db_session_maker,
        capability=DeviceCapabilityService(),
        health=AsyncMock(),
    )
    with pytest.raises(RuntimeError, match="probe-offline-exploded"):
        await svc.run_session_viability_probe(
            device.id,
            checked_by=service_viability.SessionViabilityCheckedBy.recovery,
        )

    mark_dirty.assert_not_awaited()


async def test_gating_failure_before_claim_leaves_no_probe_row(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The readiness gate runs before the birth-row claim.

    A transient failure there must leave no probe claim behind; the row lifecycle
    starts only after readiness and node checks pass. (After Task 3 the gate runs
    under the device row lock; ``lock_device`` is not used by the probe — it
    locks via ``lock_device_handle`` — so a ``lock_device`` mock is not awaited.)
    """
    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="exc-gating",
        operational_state=DeviceOperationalState.available,
        verified=True,
    )
    db_session.add(_running_node(device))
    await db_session.commit()

    lock_device = AsyncMock()
    monkeypatch.setattr(service_viability.device_locking, "lock_device", lock_device)
    # Blowing up the readiness gate models a disconnect/transient failure before
    # the probe row is claimed.
    monkeypatch.setattr(
        service_viability,
        "is_ready_for_use_async",
        AsyncMock(side_effect=RuntimeError("disconnect-in-gap")),
    )

    svc = SessionViabilityService(
        publisher=event_bus,
        settings=FakeSettingsReader({"general.session_viability_timeout_sec": 5}),
        session_factory=db_session_maker,
        capability=DeviceCapabilityService(),
        health=AsyncMock(),
    )
    with pytest.raises(RuntimeError, match="disconnect-in-gap"):
        await svc.run_session_viability_probe(
            device.id,
            checked_by=service_viability.SessionViabilityCheckedBy.manual,
        )

    lock_device.assert_not_awaited()
