import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.exc import IntegrityError

from app.appium_nodes.exceptions import NodeManagerError
from app.core.errors import AgentCallError
from app.devices.models import ConnectionType, DeviceType
from app.devices.services import verification_execution as execution
from app.devices.services.identity_conflicts import DeviceIdentityConflictError


def _device(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "id": uuid.uuid4(),
        "host": SimpleNamespace(ip="10.0.0.1", agent_port=5100),
        "host_id": uuid.uuid4(),
        "pack_id": "pack",
        "platform_id": "platform",
        "device_type": DeviceType.real_device,
        "connection_type": ConnectionType.usb,
        "ip_address": None,
        "connection_target": "target",
        "identity_value": "target",
        "tags": {},
        "appium_node": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


async def test_run_device_health_success_failure_and_agent_error(monkeypatch: pytest.MonkeyPatch) -> None:
    job: dict[str, object] = {"stages": []}
    monkeypatch.setattr(execution, "set_stage", AsyncMock())
    monkeypatch.setattr(execution.settings_service, "get", lambda key: 30)
    fetch = AsyncMock(return_value={"healthy": True, "avd_launched": {"serial": "emulator-5554"}})
    monkeypatch.setattr(execution, "fetch_pack_device_health", fetch)
    device = _device(device_type=DeviceType.emulator, tags={"emulator_headless": "false"})

    assert await execution.run_device_health(job, device, http_client_factory=MagicMock()) is None
    assert device.connection_target == "emulator-5554"
    assert fetch.await_args.kwargs["headless"] is False

    fetch.side_effect = None
    fetch.return_value = {"healthy": False, "checks": [{"check_id": "boot_completed", "ok": False, "message": "no"}]}
    assert (
        await execution.run_device_health(job, _device(), http_client_factory=MagicMock())
        == "boot completed failed (no)"
    )

    fetch.side_effect = AgentCallError("10.0.0.1", "down")
    assert await execution.run_device_health(job, _device(), http_client_factory=MagicMock()) == (
        "Agent health check failed: down"
    )

    no_host = _device(host=None)
    assert await execution.run_device_health(job, no_host, http_client_factory=MagicMock()) is None


async def test_save_verified_device_failure_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    db = MagicMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    job: dict[str, object] = {"stages": []}
    monkeypatch.setattr(execution, "set_stage", AsyncMock())

    create_context = SimpleNamespace(
        mode="create",
        save_payload={"name": "Device", "pack_id": "pack", "platform_id": "platform", "host_id": uuid.uuid4()},
        save_device_id=None,
    )
    monkeypatch.setattr(
        execution.device_service, "create_device", AsyncMock(side_effect=DeviceIdentityConflictError("dupe"))
    )
    saved, detail = await execution.save_verified_context(job, db, create_context)
    assert saved is None
    assert detail == "dupe"

    update_context_missing_id = SimpleNamespace(mode="update", save_payload={}, save_device_id=None)
    saved, detail = await execution.save_verified_context(job, db, update_context_missing_id)
    assert saved is None
    assert detail == "Verification context is missing the persisted device id"

    update_context = SimpleNamespace(
        mode="update",
        save_payload={"host_id": uuid.uuid4()},
        save_device_id=uuid.uuid4(),
    )
    monkeypatch.setattr(execution.device_service, "update_device", AsyncMock(return_value=None))
    saved, detail = await execution.save_verified_context(job, db, update_context)
    assert saved is None
    assert detail == "Device was not found"

    monkeypatch.setattr(
        execution.device_service, "update_device", AsyncMock(side_effect=IntegrityError("s", "p", Exception()))
    )
    saved, detail = await execution.save_verified_context(job, db, update_context)
    assert saved is None
    assert detail == "Device identity conflict"

    monkeypatch.setattr(execution.device_service, "update_device", AsyncMock(side_effect=ValueError("invalid")))
    saved, detail = await execution.save_verified_context(job, db, update_context)
    assert saved is None
    assert detail == "invalid"


async def test_finalize_failure_create_and_update_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    db = MagicMock()
    db.commit = AsyncMock()
    db.no_autoflush = MagicMock()
    db.no_autoflush.__enter__ = MagicMock()
    db.no_autoflush.__exit__ = MagicMock()
    job: dict[str, object] = {"stages": []}
    transient = _device()
    node = SimpleNamespace(observed_running=True)
    monkeypatch.setattr(execution, "_stop_verification_node_if_running", AsyncMock(return_value="cleanup failed"))
    monkeypatch.setattr(execution.device_service, "delete_device", AsyncMock())

    create_context = SimpleNamespace(mode="create", save_device_id=uuid.uuid4(), transient_device=transient)
    outcome = await execution._finalize_failure(db, create_context, error="bad", job=job, node=node)
    assert outcome.error == "cleanup failed"
    assert outcome.device_id is None

    locked = _device(name="changed")
    monkeypatch.setattr(execution, "_stop_verification_node_if_running", AsyncMock(return_value=None))
    monkeypatch.setattr(execution.device_locking, "lock_device", AsyncMock(return_value=locked))
    machine = MagicMock()
    machine.transition = AsyncMock()
    monkeypatch.setattr(execution, "DeviceStateMachine", lambda: machine)
    update_context = SimpleNamespace(mode="update", save_device_id=locked.id, transient_device=transient)
    outcome = await execution._finalize_failure(
        db,
        update_context,
        error="bad",
        job=job,
        original_fields={"name": "original"},
    )
    assert outcome.device_id == str(locked.id)
    assert locked.name == "original"
    machine.transition.assert_awaited_once()


async def test_execute_verification_context_missing_id_and_crash_path(monkeypatch: pytest.MonkeyPatch) -> None:
    db = MagicMock()
    db.commit = AsyncMock()
    job: dict[str, object] = {"stages": []}
    context = SimpleNamespace(save_device_id=None, transient_device=_device(identity_value="missing"))
    with pytest.raises(NodeManagerError, match="no persisted device id"):
        await execution.execute_verification_context(
            job,
            db,
            context,
            http_client_factory=MagicMock(),
            probe_session_fn=AsyncMock(),
        )

    context = SimpleNamespace(
        mode="create",
        save_device_id=uuid.uuid4(),
        transient_device=_device(),
        save_payload={},
        keep_running_after_verify=False,
    )
    monkeypatch.setattr(execution, "run_device_health", AsyncMock(side_effect=RuntimeError("crash")))
    finalize = AsyncMock(return_value=execution.VerificationExecutionOutcome(status="failed"))
    monkeypatch.setattr(execution, "_finalize_failure", finalize)
    with pytest.raises(RuntimeError, match="crash"):
        await execution.execute_verification_context(
            job,
            db,
            context,
            http_client_factory=MagicMock(),
            probe_session_fn=AsyncMock(),
        )
    finalize.assert_awaited_once()


async def test_run_device_health_accepts_plain_str_enum_attributes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: device row with plain-string device_type / connection_type
    (e.g. after `setattr` from agent-normalized save_payload in the update
    path) must not crash run_device_health with AttributeError on `.value`.
    """
    job: dict[str, object] = {"stages": []}
    monkeypatch.setattr(execution, "set_stage", AsyncMock())
    monkeypatch.setattr(execution.settings_service, "get", lambda key: 30)
    fetch = AsyncMock(return_value={"healthy": True})
    monkeypatch.setattr(execution, "fetch_pack_device_health", fetch)

    device = _device(device_type="real_device", connection_type="usb")
    assert await execution.run_device_health(job, device, http_client_factory=MagicMock()) is None
    assert fetch.await_args.kwargs["device_type"] == "real_device"
    assert fetch.await_args.kwargs["connection_type"] == "usb"
