import uuid
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.devices import locking as device_locking
from app.devices.models import ConnectionType, Device, DeviceOperationalState, DeviceType
from app.devices.schemas.device import DevicePatch, DeviceVerificationCreate, DeviceVerificationUpdate
from app.devices.services import readiness as device_readiness
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


async def test_create_device_txn_stamps_payload_and_leaves_integrity_error_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = MagicMock()
    db.rollback = AsyncMock()
    prepared = {"name": "Device"}
    monkeypatch.setattr(DeviceCrudService, "prepare_device_create_payload", AsyncMock(return_value=prepared))

    crud = DeviceCrudService(identity=DeviceIdentityConflictService(), publisher=event_bus)
    ensure = AsyncMock()
    monkeypatch.setattr(crud._identity, "ensure_device_payload_identity_available", ensure)
    monkeypatch.setattr(
        device_write,
        "create_device_record",
        AsyncMock(side_effect=IntegrityError("stmt", "params", Exception("dupe"))),
    )
    with pytest.raises(IntegrityError):
        await crud.create_device_txn(
            db,
            DeviceVerificationCreate(
                name="Device",
                pack_id="pack",
                platform_id="platform",
                host_id=uuid.uuid4(),
            ),
            mark_verified=True,
            initial_operational_state=DeviceOperationalState.available,
        )

    assert "verified_at" in prepared
    assert prepared["operational_state_last_emitted"] == DeviceOperationalState.available
    assert ensure.await_count == 1, "the command gates identity once and hands the race to its caller"
    db.rollback.assert_not_awaited()


async def test_update_device_txn_contract_missing_and_integrity_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    """update_device_txn rejects a non-``DevicePatch`` payload under the enforced
    contract, and — once past that gate — leaves a persist-time ``IntegrityError``
    to propagate untouched rather than defensively rolling back the caller's
    transaction. (The "device missing" branch of this same method is covered by
    ``test_update_device_txn_reports_false_when_device_missing`` in
    ``test_devices_api.py``.)
    """
    db = MagicMock()
    db.rollback = AsyncMock()
    device_id = uuid.uuid4()
    crud = DeviceCrudService(identity=DeviceIdentityConflictService(), publisher=event_bus)

    device = SimpleNamespace(id=device_id, verified_at="old")
    locked = SimpleNamespace(device=device, assert_active=lambda _db: None)
    monkeypatch.setattr(device_locking, "lock_device_handle", AsyncMock(return_value=locked))
    with pytest.raises(ValueError, match="generic device patch"):
        await crud.update_device_txn(
            db,
            device_id,
            DeviceVerificationUpdate(host_id=uuid.uuid4()),
            enforce_patch_contract=True,
        )

    monkeypatch.setattr(device_write, "validate_patch_contract", lambda *args: None)
    monkeypatch.setattr(DeviceCrudService, "prepare_device_update_payload", AsyncMock(return_value={"name": "new"}))
    monkeypatch.setattr(crud._identity, "ensure_device_payload_identity_available", AsyncMock())
    monkeypatch.setattr(device_readiness, "payload_requires_reverification", lambda *args: True)
    monkeypatch.setattr(device_write, "apply_device_payload", lambda *args: None)
    monkeypatch.setattr(
        device_write,
        "persist_device_record",
        AsyncMock(side_effect=IntegrityError("stmt", "params", Exception("dupe"))),
    )

    with pytest.raises(IntegrityError):
        await crud.update_device_txn(db, device_id, DevicePatch(name="new"))

    assert device.verified_at is None
    db.rollback.assert_not_awaited()
