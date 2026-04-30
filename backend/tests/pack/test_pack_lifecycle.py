import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.device import ConnectionType, Device, DeviceType
from app.models.device_reservation import DeviceReservation
from app.models.driver_pack import DriverPack, DriverPackRelease, PackState
from app.models.session import Session, SessionStatus
from app.models.test_run import RunState, TestRun
from app.services.pack_lifecycle_service import (
    count_active_work_for_pack,
    transition_pack_state,
)


async def _seed_pack(db: AsyncSession, pack_id: str = "test-pack", state: PackState = PackState.enabled) -> DriverPack:
    pack = DriverPack(id=pack_id, origin="uploaded", display_name="Test", state=state.value)
    db.add(pack)
    release = DriverPackRelease(
        pack_id=pack_id,
        release="2026.04.0",
        manifest_json={"appium_server": {}, "appium_driver": {}, "platforms": []},
    )
    db.add(release)
    await db.flush()
    return pack


@pytest.mark.asyncio
async def test_enable_to_disabled_no_active_work(db_session: AsyncSession) -> None:
    await _seed_pack(db_session, state=PackState.enabled)
    pack = await transition_pack_state(db_session, "test-pack", PackState.disabled)
    assert pack.state == PackState.disabled


@pytest.mark.asyncio
async def test_draining_to_enabled(db_session: AsyncSession) -> None:
    await _seed_pack(db_session, state=PackState.draining)
    pack = await transition_pack_state(db_session, "test-pack", PackState.enabled)
    assert pack.state == PackState.enabled


@pytest.mark.asyncio
async def test_disabled_to_enabled(db_session: AsyncSession) -> None:
    await _seed_pack(db_session, state=PackState.disabled)
    pack = await transition_pack_state(db_session, "test-pack", PackState.enabled)
    assert pack.state == PackState.enabled


@pytest.mark.asyncio
async def test_draft_to_enabled(db_session: AsyncSession) -> None:
    await _seed_pack(db_session, state=PackState.draft)
    pack = await transition_pack_state(db_session, "test-pack", PackState.enabled)
    assert pack.state == PackState.enabled


@pytest.mark.asyncio
async def test_invalid_transition_raises(db_session: AsyncSession) -> None:
    await _seed_pack(db_session, state=PackState.disabled)
    with pytest.raises(ValueError, match="Cannot transition"):
        await transition_pack_state(db_session, "test-pack", PackState.draining)


@pytest.mark.asyncio
async def test_count_active_work_empty(db_session: AsyncSession) -> None:
    counts = await count_active_work_for_pack(db_session, "nonexistent-pack")
    assert counts["active_runs"] == 0
    assert counts["live_sessions"] == 0


@pytest.mark.asyncio
async def test_draining_stays_draining_with_active_run(db_session: AsyncSession, default_host_id: str) -> None:
    await _seed_pack(db_session, state=PackState.enabled)
    host_id = uuid.UUID(default_host_id)
    device = Device(
        name="test-dev",
        pack_id="test-pack",
        platform_id="test-plat",
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
        host_id=host_id,
        os_version="14",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value="SERIAL001",
    )
    db_session.add(device)
    await db_session.flush()

    run = TestRun(
        name="active-run",
        state=RunState.active,
        requirements=[{"pack_id": "test-pack", "platform_id": "test-plat", "count": 1}],
    )
    db_session.add(run)
    await db_session.flush()

    reservation = DeviceReservation(
        run_id=run.id,
        device_id=device.id,
        identity_value="serial-1",
        pack_id="test-pack",
        platform_id="test-plat",
        os_version="14",
    )
    db_session.add(reservation)
    await db_session.flush()

    pack = await transition_pack_state(db_session, "test-pack", PackState.disabled)
    assert pack.state == PackState.draining


@pytest.mark.asyncio
async def test_draining_stays_draining_with_live_session(db_session: AsyncSession, default_host_id: str) -> None:
    await _seed_pack(db_session, state=PackState.enabled)
    host_id = uuid.UUID(default_host_id)
    device = Device(
        name="test-dev",
        pack_id="test-pack",
        platform_id="test-plat",
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
        host_id=host_id,
        os_version="14",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value="SERIAL001",
    )
    db_session.add(device)
    await db_session.flush()

    session = Session(
        session_id="sess-1",
        device_id=device.id,
        status=SessionStatus.running,
        requested_pack_id="test-pack",
    )
    db_session.add(session)
    await db_session.flush()

    pack = await transition_pack_state(db_session, "test-pack", PackState.disabled)
    assert pack.state == PackState.draining
