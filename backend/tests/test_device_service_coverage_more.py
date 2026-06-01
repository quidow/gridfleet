import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.exc import IntegrityError, NoResultFound

from app.devices.models import DeviceOperationalState
from app.devices.schemas.device import DevicePatch, DeviceVerificationCreate, DeviceVerificationUpdate
from app.devices.services import service as device_service
from app.devices.services.identity_conflicts import DeviceIdentityConflictService
from app.devices.services.intent import IntentService
from app.devices.services.intent_types import GRID_ROUTING, NODE_PROCESS, RECOVERY
from app.devices.services.service import DeviceCrudService
from tests.fakes import FakeSettingsReader
from tests.helpers import test_event_bus as event_bus


async def test_create_device_integrity_retry_and_mark_verified(monkeypatch: pytest.MonkeyPatch) -> None:
    db = MagicMock()
    db.rollback = AsyncMock()
    prepared = {"name": "Device"}
    monkeypatch.setattr(DeviceCrudService, "prepare_device_create_payload", AsyncMock(return_value=prepared))

    crud = DeviceCrudService(
        settings=FakeSettingsReader(), identity=DeviceIdentityConflictService(), publisher=event_bus
    )
    ensure = AsyncMock()
    monkeypatch.setattr(crud._identity, "ensure_device_payload_identity_available", ensure)
    monkeypatch.setattr(
        device_service.device_write,
        "create_device_record",
        AsyncMock(side_effect=IntegrityError("stmt", "params", Exception("dupe"))),
    )
    with pytest.raises(IntegrityError):
        await crud.create_device(
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
    assert prepared["operational_state"] == DeviceOperationalState.available
    assert ensure.await_count == 2
    db.rollback.assert_awaited_once()


async def test_update_device_contract_missing_and_integrity_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    db = MagicMock()
    db.rollback = AsyncMock()
    device_id = uuid.uuid4()
    crud = DeviceCrudService(
        settings=FakeSettingsReader(), identity=DeviceIdentityConflictService(), publisher=event_bus
    )
    monkeypatch.setattr(device_service.device_locking, "lock_device", AsyncMock(side_effect=NoResultFound))
    assert await crud.update_device(db, device_id, DevicePatch(name="new")) is None

    device = SimpleNamespace(id=device_id, verified_at="old")
    monkeypatch.setattr(device_service.device_locking, "lock_device", AsyncMock(return_value=device))
    with pytest.raises(ValueError, match="generic device patch"):
        await crud.update_device(
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
        await crud.update_device(db, device_id, DevicePatch(name="new"))

    assert device.verified_at is None
    db.rollback.assert_awaited_once()


async def test_delete_helpers_stop_and_missing_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    db = MagicMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    device_id = uuid.uuid4()
    intents = device_service._device_delete_intents(device_id)
    assert {intent.axis for intent in intents} == {NODE_PROCESS, GRID_ROUTING, RECOVERY}

    monkeypatch.setattr(device_service.device_locking, "lock_device", AsyncMock(side_effect=NoResultFound))
    assert await device_service._lock_device_for_delete(db, device_id) is None

    node = SimpleNamespace(observed_running=False)
    with pytest.raises(device_service.NodeManagerError, match="No running node"):
        await device_service._stop_node(db, SimpleNamespace(id=device_id, appium_node=node), publisher=event_bus)

    running_node = SimpleNamespace(observed_running=True)
    register = AsyncMock()
    monkeypatch.setattr(IntentService, "register_intents_and_reconcile", register)
    stopped = await device_service._stop_node(
        db, SimpleNamespace(id=device_id, appium_node=running_node), publisher=event_bus
    )
    assert stopped is running_node
    register.assert_awaited_once()
