import asyncio
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import select

from app.devices import locking as device_locking
from app.devices.models import Device, DeviceOperationalState, DeviceReservation
from app.runs import service_allocator
from app.runs.schemas import RunCreate
from app.runs.service_allocator import RunAllocatorService
from tests.conftest import test_circuit_breaker
from tests.fakes import FakeSettingsReader
from tests.helpers import create_device
from tests.helpers import test_event_bus as event_bus

if TYPE_CHECKING:
    import uuid
    from collections.abc import Iterable

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.devices.services.readiness import DeviceReadiness
    from app.packs.models import DriverPack

_settings = FakeSettingsReader({})

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("seeded_driver_packs")]


async def test_create_run_rechecks_readiness_after_lock(
    db_session_maker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    default_host_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Readiness must be re-evaluated against the freshly locked device row, not
    trusted from the pre-lock batch assessment.

    The lever is a pack-manifest setup field (``roku_password``, declared
    ``required_for_session``) rather than ``verified_at``: ``verified_at`` is
    also part of ``is_available_sql``, so clearing it would be caught by the
    locked SELECT's own WHERE clause and the test would pass even with the
    post-lock readiness recheck removed. ``device_config`` is invisible to the
    SQL gates, so only the post-lock ``assess_device_with_pack`` call can
    reject this device.
    """
    device = await create_device(
        db_session,
        host_id=default_host_id,
        name="readiness-race",
        pack_id="appium-roku-dlenroc",
        platform_id="roku_network",
        identity_scheme="roku_serial",
        identity_scope="global",
        connection_target="192.0.2.10:8060",
        roku_password="dev-secret",
        operational_state=DeviceOperationalState.available,
        verified=True,
    )
    device_id = device.id
    pack_id = device.pack_id
    platform_id = device.platform_id
    await db_session.commit()

    pre_lock_assessed = asyncio.Event()
    setup_cleared = asyncio.Event()
    real_assess = service_allocator.assess_devices_async
    gate_fired = False

    async def gated_assess(
        session: AsyncSession,
        devices: Iterable[Device],
        *,
        packs: dict[str, DriverPack] | None = None,
    ) -> dict[uuid.UUID, DeviceReadiness]:
        nonlocal gate_fired
        device_list = list(devices)
        result = await real_assess(session, device_list, packs=packs)
        if not gate_fired and any(d.id == device_id for d in device_list):
            # The pre-lock batch has assessed the device as verified. Hold here
            # while a concurrent transaction strips its required setup field, so
            # the allocator reaches its locked recheck with a stale assessment.
            gate_fired = True
            pre_lock_assessed.set()
            await asyncio.wait_for(setup_cleared.wait(), timeout=5.0)
        return result

    monkeypatch.setattr(service_allocator, "assess_devices_async", gated_assess)
    monkeypatch.setattr(service_allocator, "_MATCH_RETRY_BACKOFF_SEC", 0.0)  # keep the test fast

    allocator_svc = RunAllocatorService(
        publisher=event_bus,
        settings=_settings,
        circuit_breaker=test_circuit_breaker,
        session_factory=db_session_maker,
    )

    async def create_run() -> None:
        with pytest.raises(ValueError, match="Not enough devices"):
            await allocator_svc.create_run(
                RunCreate(
                    name="readiness-race-run",
                    requirements=[{"pack_id": pack_id, "platform_id": platform_id, "count": 1}],
                ),
            )

    async def clear_setup_field_after_assessment() -> None:
        await asyncio.wait_for(pre_lock_assessed.wait(), timeout=5.0)
        async with db_session_maker() as session:
            locked = await device_locking.lock_device(session, device_id)
            locked.device_config = {}
            await session.commit()
        setup_cleared.set()

    await asyncio.gather(create_run(), clear_setup_field_after_assessment())

    async with db_session_maker() as verify:
        reservation = (
            await verify.execute(
                select(DeviceReservation).where(
                    DeviceReservation.device_id == device_id,
                    DeviceReservation.released_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        final_device = (await verify.execute(select(Device).where(Device.id == device_id))).scalar_one()

    assert reservation is None
    assert final_device.operational_state_last_emitted == DeviceOperationalState.available
