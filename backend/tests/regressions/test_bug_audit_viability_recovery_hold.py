"""Recovery probe admits an offline device that is still reserved to an active run.

A device that goes ``offline`` mid-run keeps its (non-released) reservation row, so
``device_is_reserved`` stays True. The recovery-class probe is the only path that can
re-validate such a device (the scheduled loop skips non-``available`` devices). Gating the
recovery branch on ``not device_reserved`` therefore dead-locked recovery: the probe was
rejected with ``ValueError("...only run for available devices")``, the rejection was folded
into a *failed* recovery attempt, and the device escalated to ``review_required`` with
``exclusion_reason`` set to that gate string. The recovery branch must admit
``offline``/``verifying`` devices regardless of reservation — an ``offline`` device serves
no client session, so the probe cannot steal an in-use Grid slot.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.appium_nodes.models import AppiumDesiredState, AppiumNode
from app.devices.models import Device, DeviceOperationalState
from app.devices.services.capability import DeviceCapabilityService
from app.sessions import service_viability as session_viability
from app.sessions.service_viability import SessionViabilityProbeNotPermittedError, SessionViabilityService
from app.sessions.viability_types import SessionViabilityCheckedBy
from tests.fakes import FakeSettingsReader
from tests.helpers import create_device, create_host, create_reservation
from tests.helpers import test_event_bus as event_bus

if TYPE_CHECKING:
    from httpx2 import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@pytest.mark.db
@pytest.mark.asyncio
async def test_recovery_probe_admits_offline_reserved_device(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host = await create_host(client)
    device = await create_device(
        db_session,
        host_id=uuid.UUID(host["id"]),
        name="viability-recovery-reserved",
        operational_state=DeviceOperationalState.offline,
        verified=True,
    )
    # An observed-running node (Task 8 made a missing/unobserved node its own benign-skip
    # carve-out for recovery probes, so a node-down device can no longer prove reservation-gate
    # admission by falling through to a terminal verdict — see below). This is otherwise the same
    # fixture the reservation-deadlock bug this test guards actually manifested on.
    node = AppiumNode(
        device_id=device.id,
        port=4723,
        desired_state=AppiumDesiredState.running,
        desired_port=4723,
        pid=1234,
        active_connection_target="viability-recovery-reserved",
    )
    db_session.add(node)
    await create_reservation(db_session, device_id=device.id)
    await db_session.commit()

    # Isolate the reservation-gate behavior from readiness assessment.
    async def _always_ready(*_a: object, **_kw: object) -> bool:
        return True

    monkeypatch.setattr(session_viability, "is_ready_for_use_async", _always_ready)
    monkeypatch.setattr(
        DeviceCapabilityService, "get_device_capabilities", AsyncMock(return_value={"platformName": "Android"})
    )
    monkeypatch.setattr(SessionViabilityService, "probe_session_direct", AsyncMock(return_value=(True, None)))

    # Reload with the node relationship eagerly loaded; the probe reads device.appium_node.
    device = (
        await db_session.execute(select(Device).where(Device.id == device.id).options(selectinload(Device.appium_node)))
    ).scalar_one()

    svc = SessionViabilityService(
        publisher=event_bus,
        settings=FakeSettingsReader({}),
        session_factory=db_session_maker,
        capability=DeviceCapabilityService(),
        health=AsyncMock(),
    )

    # The recovery probe must NOT be rejected for being reserved: with an observed-running node it
    # runs all the way through to a real remote-probe verdict ("passed") instead of raising the
    # reservation-gate ValueError. Reaching a verdict at all is only possible once both the
    # reservation gate and the node precondition have been cleared.
    state = await svc.run_session_viability_probe(
        device.id,
        checked_by=SessionViabilityCheckedBy.recovery,
    )
    assert state["status"] == "passed"


@pytest.mark.db
@pytest.mark.asyncio
async def test_recovery_probe_on_non_recoverable_state_raises_not_permitted(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    client: AsyncClient,
) -> None:
    """A recovery probe against a device that is NOT ``offline``/``verifying`` (here
    ``maintenance``) is rejected with the typed ``SessionViabilityProbeNotPermittedError``
    (a ``ValueError`` subclass, so manual HTTP callers still surface 409). The distinct
    type lets the recovery loop treat the gate rejection as a skip, not a health failure."""
    host = await create_host(client)
    device = await create_device(
        db_session,
        host_id=uuid.UUID(host["id"]),
        name="viability-recovery-maintenance",
        # operational_state is a read-time projection: a maintenance_reason fact
        # makes the device derive ``maintenance`` — a non-recoverable state that
        # is not in the recovery-probe-admissible set (offline/verifying).
        lifecycle_policy_state={"maintenance_reason": "operator maintenance"},
        verified=True,
    )
    await db_session.commit()

    svc = SessionViabilityService(
        publisher=event_bus,
        settings=FakeSettingsReader({}),
        session_factory=db_session_maker,
        capability=DeviceCapabilityService(),
        health=AsyncMock(),
    )
    with pytest.raises(SessionViabilityProbeNotPermittedError):
        await svc.run_session_viability_probe(
            device.id,
            checked_by=SessionViabilityCheckedBy.recovery,
        )
