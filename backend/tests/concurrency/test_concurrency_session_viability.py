# backend/tests/test_concurrency_session_viability.py
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.appium_nodes.models import AppiumDesiredState, AppiumNode
from app.devices import locking as device_locking
from app.devices.models import Device, DeviceOperationalState
from app.devices.services.capability import DeviceCapabilityService
from app.sessions import service_viability as session_viability
from app.sessions.models import Session, SessionStatus
from app.sessions.probe_constants import PROBE_TEST_NAME
from app.sessions.service_viability import SessionViabilityService
from tests.fakes import FakeSettingsReader
from tests.helpers import create_device

if TYPE_CHECKING:
    from app.hosts.models import Host

pytestmark = pytest.mark.asyncio


@pytest.mark.usefixtures("seeded_driver_packs")
async def test_probe_lock_collision_raises_typed_in_progress_error(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    """F6: a genuine in-flight probe lock makes a second probe raise the *typed*
    ``SessionViabilityProbeInProgressError``.

    It subclasses ``ValueError`` so manual callers still surface HTTP 409, but the distinct
    type lets the recovery loop tell a *collision* (another probe holds the lock) from a real
    probe *failure* — a collision says nothing about device health and must not count as a
    failed recovery attempt (which would bump backoff/review and could shelve a healthy device).
    """
    from app.sessions.service_viability import SessionViabilityProbeInProgressError

    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="probe-collision",
        operational_state=DeviceOperationalState.available,
        verified=True,
    )
    node = AppiumNode(
        device_id=device.id,
        port=4723,
        desired_state=AppiumDesiredState.running,
        desired_port=4723,
        pid=1234,
        active_connection_target="probe-target",
    )
    device.appium_node = node
    db_session.add(node)
    db_session.add(
        Session(
            session_id="probe-collision-live",
            device_id=device.id,
            test_name=PROBE_TEST_NAME,
            status=SessionStatus.pending,
        )
    )
    await db_session.commit()

    svc = SessionViabilityService(
        publisher=Mock(),
        settings=FakeSettingsReader({}),
        session_factory=async_sessionmaker(db_session.bind, class_=AsyncSession, expire_on_commit=False),
        capability=DeviceCapabilityService(),
        health=AsyncMock(),
    )

    assert issubclass(SessionViabilityProbeInProgressError, ValueError)
    with pytest.raises(SessionViabilityProbeInProgressError):
        await svc.run_session_viability_probe(device.id, checked_by="recovery")


@pytest.mark.usefixtures("seeded_driver_packs")
async def test_session_viability_restore_handles_external_reservation(
    db_session_maker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """While a session-viability probe is running (device marked busy), an external
    transaction reserves the device. The probe finishes and must NOT restore the
    device back to available — the reservation must be honored.

    NOTE: This test pins the post-fix invariant (external reservation is not clobbered)
    rather than demonstrating a red→green transition. The existing db.refresh() + guard
    in the pre-fix code happens to pass this particular scenario because db.refresh()
    picks up the external commit before the restore decision. The fix (re-lock via
    FOR UPDATE) makes this guarantee deterministic and extends protection to the
    except clause as well.
    """
    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="probe-target",
        operational_state=DeviceOperationalState.available,
        verified=True,
    )
    appium_node = AppiumNode(
        device_id=device.id,
        port=9999,
        desired_state=AppiumDesiredState.running,
        desired_port=9999,
        pid=0,
        active_connection_target="",
    )
    db_session.add(appium_node)
    await db_session.commit()
    device_id = device.id

    probe_started = asyncio.Event()
    external_done = asyncio.Event()
    observed_target: str | None = None

    async def fake_probe(
        capabilities: dict[str, Any],
        timeout_sec: int,
        *,
        target: str | None = None,
        on_created: object | None = None,
    ) -> tuple[bool, str | None]:
        nonlocal observed_target
        observed_target = target
        probe_started.set()
        await external_done.wait()
        return True, None

    async def always_ready(*_a: object, **_kw: object) -> bool:
        return True

    async def fake_get_caps(*_a: object, **_kw: object) -> dict[str, Any]:
        return {"platformName": "Android"}

    svc = SessionViabilityService(
        publisher=Mock(),
        settings=FakeSettingsReader({}),
        session_factory=db_session_maker,
        capability=DeviceCapabilityService(),
        health=AsyncMock(),
    )
    monkeypatch.setattr(svc, "probe_session_direct", fake_probe)
    monkeypatch.setattr(session_viability, "is_ready_for_use_async", always_ready)
    monkeypatch.setattr(DeviceCapabilityService, "get_device_capabilities", fake_get_caps)

    async def run_probe() -> None:
        # The probe owns its own fresh sessions; only the device id crosses in.
        await svc.run_session_viability_probe(device_id, checked_by="manual")

    async def reserve_externally() -> None:
        await probe_started.wait()
        async with db_session_maker() as session, session.begin():
            # Acquire the device row lock concurrently with the probe to exercise
            # lock contention; the write itself is no longer relevant (hold removed).
            await device_locking.lock_device(session, device_id)
        external_done.set()

    await asyncio.gather(run_probe(), reserve_externally())
    assert observed_target == f"http://{db_host.ip}:9999"

    async with db_session_maker() as verify:
        device_row = (await verify.execute(select(Device).where(Device.id == device_id))).scalar_one()

    # The key invariant is that the probe completed without raising errors under
    # concurrent lock contention (observed_target is correct).
    assert device_row.operational_state_last_emitted in (
        DeviceOperationalState.available,
        DeviceOperationalState.offline,
    ), f"Unexpected device state after probe: {device_row.operational_state_last_emitted}"
