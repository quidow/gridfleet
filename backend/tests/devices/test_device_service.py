from typing import TYPE_CHECKING

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from app.devices.models import ConnectionType, Device, DeviceOperationalState, DeviceType
from app.devices.schemas.device import DeviceVerificationCreate
from app.devices.services import write as device_write
from app.devices.services.identity_conflicts import DeviceIdentityConflictError, DeviceIdentityConflictService
from app.devices.services.service import DeviceCrudService
from tests.helpers import create_device_record
from tests.helpers import test_event_bus as event_bus

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.hosts.models import Host

pytestmark = pytest.mark.usefixtures("seeded_driver_packs")


def _crud() -> DeviceCrudService:
    return DeviceCrudService(identity=DeviceIdentityConflictService(), publisher=event_bus)


@pytest.mark.db
@pytest.mark.asyncio
async def test_create_device_persists_initial_operational_state(db_session: AsyncSession, db_host: Host) -> None:
    data = DeviceVerificationCreate(
        identity_value="initial-state-verify",
        connection_target="initial-state-verify",
        name="Initial State Verify",
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        os_version="14",
        host_id=db_host.id,
    )

    device = await _crud().create_device_txn(
        db_session,
        data,
        initial_operational_state=DeviceOperationalState.verifying,
    )

    assert device.operational_state_last_emitted is DeviceOperationalState.verifying
    assert device.verified_at is None


@pytest.mark.db
@pytest.mark.asyncio
async def test_create_device_record_rolls_back_with_the_callers_transaction(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    db_host: Host,
) -> None:
    """The helper stages and flushes; the caller's transaction decides the outcome."""
    await db_session.commit()  # publish the host so the command session can see it
    payload = {
        "pack_id": "appium-uiautomator2",
        "platform_id": "android_mobile",
        "identity_scheme": "android_serial",
        "identity_scope": "host",
        "identity_value": "record-rollback-1",
        "connection_target": "record-rollback-1",
        "name": "Record Rollback",
        "os_version": "14",
        "host_id": db_host.id,
        "device_type": DeviceType.real_device,
        "connection_type": ConnectionType.usb,
        "operational_state_last_emitted": DeviceOperationalState.offline,
    }

    with pytest.raises(DBAPIError):
        async with db_session_maker() as command_db, command_db.begin():
            device = await device_write.create_device_record(command_db, payload)
            assert device.id is not None, "flush must apply the uuid PK default"
            # A real aborting statement, not a patched side effect: the caller's
            # transaction dies exactly the way production would kill it.
            await command_db.execute(text("SELECT 1 / 0"))

    surviving = await db_session.scalar(
        select(func.count()).select_from(Device).where(Device.identity_value == "record-rollback-1")
    )
    assert surviving == 0, "create_device_record committed behind its caller's back"


@pytest.mark.db
@pytest.mark.asyncio
async def test_create_device_txn_identity_conflict_leaves_the_transaction_to_its_caller(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    db_host: Host,
) -> None:
    await create_device_record(
        db_session,
        host_id=db_host.id,
        identity_value="identity-taken-1",
        name="Identity Taken",
    )
    data = DeviceVerificationCreate(
        identity_value="identity-taken-1",
        connection_target="identity-taken-1",
        name="Identity Duplicate",
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        os_version="14",
        host_id=db_host.id,
    )

    async with db_session_maker() as command_db, command_db.begin():
        with pytest.raises(DeviceIdentityConflictError):
            await _crud().create_device_txn(command_db, data)
        assert command_db.in_transaction(), "the identity gate must not end the caller's transaction"

    total = await db_session.scalar(
        select(func.count()).select_from(Device).where(Device.identity_value == "identity-taken-1")
    )
    assert total == 1
