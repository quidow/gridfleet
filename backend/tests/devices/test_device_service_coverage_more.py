import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.exc import IntegrityError, NoResultFound

from app.devices.models import DeviceOperationalState
from app.devices.schemas.device import DevicePatch, DeviceVerificationCreate, DeviceVerificationUpdate
from app.devices.services import service as device_service
from app.devices.services.identity_conflicts import DeviceIdentityConflictService
from app.devices.services.service import DeviceCrudService
from tests.helpers import test_event_bus as event_bus


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
        device_service.device_write,
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
    db = MagicMock()
    db.rollback = AsyncMock()
    device_id = uuid.uuid4()
    crud = DeviceCrudService(identity=DeviceIdentityConflictService(), publisher=event_bus)
    monkeypatch.setattr(device_service.device_locking, "lock_device_handle", AsyncMock(side_effect=NoResultFound))
    assert await crud.update_device_txn(db, device_id, DevicePatch(name="new")) is False

    device = SimpleNamespace(id=device_id, verified_at="old")
    locked = SimpleNamespace(device=device, assert_active=lambda _db: None)
    monkeypatch.setattr(device_service.device_locking, "lock_device_handle", AsyncMock(return_value=locked))
    with pytest.raises(ValueError, match="generic device patch"):
        await crud.update_device_txn(
            db,
            device_id,
            DeviceVerificationUpdate(host_id=uuid.uuid4()),
            enforce_patch_contract=True,
        )

    monkeypatch.setattr(device_service.device_write, "validate_patch_contract", lambda *args: None)
    monkeypatch.setattr(DeviceCrudService, "prepare_device_update_payload", AsyncMock(return_value={"name": "new"}))
    monkeypatch.setattr(crud._identity, "ensure_device_payload_identity_available", AsyncMock())
    monkeypatch.setattr(device_service.device_readiness, "payload_requires_reverification", lambda *args: True)
    monkeypatch.setattr(device_service.device_write, "apply_device_payload", lambda *args: None)
    monkeypatch.setattr(
        device_service.device_write,
        "persist_device_record",
        AsyncMock(side_effect=IntegrityError("stmt", "params", Exception("dupe"))),
    )

    with pytest.raises(IntegrityError):
        await crud.update_device_txn(db, device_id, DevicePatch(name="new"))

    assert device.verified_at is None
    db.rollback.assert_not_awaited()


async def test_lock_device_for_delete_missing_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    db = MagicMock()
    device_id = uuid.uuid4()
    monkeypatch.setattr(device_service.device_locking, "lock_device_handle", AsyncMock(side_effect=NoResultFound))
    assert await device_service._lock_device_for_delete(db, device_id) is None
