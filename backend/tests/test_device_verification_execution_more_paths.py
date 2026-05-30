import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

from app.appium_nodes.exceptions import NodeManagerError
from app.core.errors import AgentCallError
from app.devices.models import ConnectionType, DeviceType
from app.devices.services import verification_execution as execution
from app.devices.services.verification_execution import VerificationExecutionService
from tests.fakes import FakeSettingsReader
from tests.helpers import test_event_bus as event_bus


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
    settings = FakeSettingsReader({"appium.startup_timeout_sec": 30})
    fetch = AsyncMock(return_value={"healthy": True, "avd_launched": {"serial": "emulator-5554"}})
    monkeypatch.setattr(execution, "fetch_pack_device_health", fetch)
    device = _device(device_type=DeviceType.emulator, tags={"emulator_headless": "false"})

    assert (
        await VerificationExecutionService(
            publisher=event_bus, settings=settings, circuit_breaker=Mock()
        ).run_device_health(job, device, http_client_factory=MagicMock())
        is None
    )
    assert device.connection_target == "emulator-5554"
    assert fetch.await_args.kwargs["headless"] is False

    fetch.side_effect = None
    fetch.return_value = {"healthy": False, "checks": [{"check_id": "boot_completed", "ok": False, "message": "no"}]}
    assert (
        await VerificationExecutionService(
            publisher=event_bus, settings=FakeSettingsReader({}), circuit_breaker=Mock()
        ).run_device_health(job, _device(), http_client_factory=MagicMock())
        == "boot completed failed (no)"
    )

    fetch.side_effect = AgentCallError("10.0.0.1", "down")
    assert await VerificationExecutionService(
        publisher=event_bus, settings=FakeSettingsReader({}), circuit_breaker=Mock()
    ).run_device_health(job, _device(), http_client_factory=MagicMock()) == ("Agent health check failed: down")

    no_host = _device(host=None)
    assert (
        await VerificationExecutionService(
            publisher=event_bus, settings=FakeSettingsReader({}), circuit_breaker=Mock()
        ).run_device_health(job, no_host, http_client_factory=MagicMock())
        is None
    )


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
    outcome = await execution._finalize_failure(
        db, create_context, error="bad", job=job, node=node, publisher=event_bus
    )
    assert outcome.error == "cleanup failed"
    assert outcome.device_id is None

    locked = _device(name="changed")
    monkeypatch.setattr(execution, "_stop_verification_node_if_running", AsyncMock(return_value=None))
    monkeypatch.setattr(execution.device_locking, "lock_device", AsyncMock(return_value=locked))
    revoke_mock = AsyncMock()
    monkeypatch.setattr(execution, "_revoke_verification_node_intent", revoke_mock)
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
        publisher=event_bus,
    )
    assert outcome.device_id == str(locked.id)
    assert locked.name == "original"
    machine.transition.assert_awaited_once()
    revoke_mock.assert_awaited_once_with(db, locked)


async def test_execute_verification_context_missing_id_and_crash_path(monkeypatch: pytest.MonkeyPatch) -> None:
    db = MagicMock()
    db.commit = AsyncMock()
    job: dict[str, object] = {"stages": []}
    context = SimpleNamespace(save_device_id=None, transient_device=_device(identity_value="missing"))
    svc = VerificationExecutionService(publisher=event_bus, settings=FakeSettingsReader({}), circuit_breaker=Mock())
    with pytest.raises(NodeManagerError, match="no persisted device id"):
        await svc.execute_verification_context(
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
    finalize = AsyncMock(return_value=execution.VerificationExecutionOutcome(status="failed"))
    monkeypatch.setattr(execution, "_finalize_failure", finalize)
    svc2 = VerificationExecutionService(publisher=event_bus, settings=FakeSettingsReader({}), circuit_breaker=Mock())
    svc2.run_device_health = AsyncMock(side_effect=RuntimeError("crash"))  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="crash"):
        await svc2.execute_verification_context(
            job,
            db,
            context,
            http_client_factory=MagicMock(),
            probe_session_fn=AsyncMock(),
        )
    finalize.assert_awaited_once()


async def test_finalize_success_revokes_verification_intent_after_verified_at(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: ``_revoke_verification_node_intent`` must run only after
    ``locked.verified_at`` is set. Otherwise the reconcile triggered by the
    revoke sees ``verified_at IS NULL``, skips the ``baseline:idle`` injection,
    and computes ``desired_state=stopped`` once the precondition sweep has
    already retired the ``operator:start`` intent — causing a spurious
    ``available -> offline`` event right after registration.
    """
    from app.devices.models import DeviceOperationalState
    from app.devices.services.verification_preparation import PreparedVerificationContext

    db = AsyncMock()
    device_id = uuid.uuid4()
    locked = SimpleNamespace(
        id=device_id,
        operational_state=DeviceOperationalState.verifying,
        verified_at=None,
    )
    context = PreparedVerificationContext(
        mode="create",
        transient_device=locked,
        save_payload={"name": "created"},
        save_device_id=device_id,
        keep_running_after_verify=True,
    )
    monkeypatch.setattr(execution.device_locking, "lock_device", AsyncMock(return_value=locked))
    monkeypatch.setattr(execution, "_restore_create_payload_fields", lambda *args: None)
    monkeypatch.setattr(execution, "set_stage", AsyncMock())

    def transition(device: SimpleNamespace, _event: object, *, reason: str, **kwargs: object) -> None:
        del reason
        device.operational_state = DeviceOperationalState.available

    machine = SimpleNamespace(transition=AsyncMock(side_effect=transition))
    monkeypatch.setattr(execution, "DeviceStateMachine", lambda: machine)
    monkeypatch.setattr(
        execution,
        "ready_operational_state",
        AsyncMock(return_value=DeviceOperationalState.available),
    )
    monkeypatch.setattr(execution, "set_operational_state", AsyncMock())
    monkeypatch.setattr(execution.session_viability, "record_session_viability_result", AsyncMock())

    verified_at_when_revoked: list[object] = []
    op_state_when_revoked: list[object] = []

    async def revoke(_db: object, device: SimpleNamespace) -> None:
        verified_at_when_revoked.append(device.verified_at)
        op_state_when_revoked.append(device.operational_state)

    monkeypatch.setattr(execution, "_revoke_verification_node_intent", revoke)

    outcome = await execution._finalize_success(
        db,
        context,
        job={"stages": []},
        node=SimpleNamespace(port=4723, pid=22),
        publisher=event_bus,
    )

    assert outcome.status == "completed"
    assert len(verified_at_when_revoked) == 1
    assert verified_at_when_revoked[0] is not None, "verified_at must be set before revoke"
    assert op_state_when_revoked[0] == DeviceOperationalState.available


async def test_run_device_health_accepts_plain_str_enum_attributes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: device row with plain-string device_type / connection_type
    (e.g. after `setattr` from agent-normalized save_payload in the update
    path) must not crash run_device_health with AttributeError on `.value`.
    """
    job: dict[str, object] = {"stages": []}
    monkeypatch.setattr(execution, "set_stage", AsyncMock())
    settings = FakeSettingsReader({"appium.startup_timeout_sec": 30})
    fetch = AsyncMock(return_value={"healthy": True})
    monkeypatch.setattr(execution, "fetch_pack_device_health", fetch)

    device = _device(device_type="real_device", connection_type="usb")
    assert (
        await VerificationExecutionService(
            publisher=event_bus, settings=settings, circuit_breaker=Mock()
        ).run_device_health(job, device, http_client_factory=MagicMock())
        is None
    )
    assert fetch.await_args.kwargs["device_type"] == "real_device"
    assert fetch.await_args.kwargs["connection_type"] == "usb"
