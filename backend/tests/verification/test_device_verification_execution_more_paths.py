import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

from app.appium_nodes.exceptions import NodeManagerError
from app.core.errors import AgentCallError
from app.devices.models import ConnectionType, DeviceType
from app.devices.services.capability import DeviceCapabilityService
from app.devices.services.identity_conflicts import DeviceIdentityConflictService
from app.devices.services.service import DeviceCrudService
from app.verification.services import execution
from app.verification.services.execution import AgentCallContext, VerificationExecutionService
from tests.fakes import FakeSettingsReader, build_review_service
from tests.helpers import test_event_bus as event_bus


def _make_svc(**overrides: object) -> VerificationExecutionService:
    """Minimal service instance for unit-testing _finalize_* methods."""
    defaults: dict[str, object] = {
        "publisher": event_bus,
        "agent": AgentCallContext(settings=FakeSettingsReader({}), circuit_breaker=Mock()),
        "crud": AsyncMock(),
        "viability": AsyncMock(),
        "capability": AsyncMock(),
        "reconciler": AsyncMock(),
        "node_manager": AsyncMock(),
        "review": build_review_service(),
    }
    defaults.update(overrides)
    return VerificationExecutionService(**defaults)  # type: ignore[arg-type]


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
    fetch = AsyncMock(return_value={"healthy": True})
    monkeypatch.setattr(execution, "fetch_pack_device_health", fetch)
    device = _device(device_type=DeviceType.emulator)

    assert (
        await VerificationExecutionService(
            review=build_review_service(),
            publisher=event_bus,
            agent=AgentCallContext(settings=settings, circuit_breaker=Mock()),
            crud=DeviceCrudService(settings=settings, identity=DeviceIdentityConflictService(), publisher=event_bus),
            viability=Mock(),
            capability=DeviceCapabilityService(),
            reconciler=AsyncMock(),
            node_manager=AsyncMock(),
        ).run_device_health(job, device, http_client_factory=MagicMock())
        is None
    )

    fetch.side_effect = None
    fetch.return_value = {"healthy": False, "checks": [{"check_id": "boot_completed", "ok": False, "message": "no"}]}
    _s2 = FakeSettingsReader({})
    assert (
        await VerificationExecutionService(
            review=build_review_service(),
            publisher=event_bus,
            agent=AgentCallContext(settings=_s2, circuit_breaker=Mock()),
            crud=DeviceCrudService(settings=_s2, identity=DeviceIdentityConflictService(), publisher=event_bus),
            viability=Mock(),
            capability=DeviceCapabilityService(),
            reconciler=AsyncMock(),
            node_manager=AsyncMock(),
        ).run_device_health(job, _device(), http_client_factory=MagicMock())
        == "boot completed failed (no)"
    )

    fetch.side_effect = AgentCallError("10.0.0.1", "down")
    _s3 = FakeSettingsReader({})
    assert await VerificationExecutionService(
        review=build_review_service(),
        publisher=event_bus,
        agent=AgentCallContext(settings=_s3, circuit_breaker=Mock()),
        crud=DeviceCrudService(settings=_s3, identity=DeviceIdentityConflictService(), publisher=event_bus),
        viability=Mock(),
        capability=DeviceCapabilityService(),
        reconciler=AsyncMock(),
        node_manager=AsyncMock(),
    ).run_device_health(job, _device(), http_client_factory=MagicMock()) == ("Agent health check failed: down")

    no_host = _device(host=None)
    _s4 = FakeSettingsReader({})
    assert (
        await VerificationExecutionService(
            review=build_review_service(),
            publisher=event_bus,
            agent=AgentCallContext(settings=_s4, circuit_breaker=Mock()),
            crud=DeviceCrudService(settings=_s4, identity=DeviceIdentityConflictService(), publisher=event_bus),
            viability=Mock(),
            capability=DeviceCapabilityService(),
            reconciler=AsyncMock(),
            node_manager=AsyncMock(),
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
    mock_crud = AsyncMock()
    mock_crud.delete_device = AsyncMock()
    monkeypatch.setattr(execution, "_stop_verification_node_if_running", AsyncMock(return_value="cleanup failed"))
    stamp_mock = AsyncMock()
    monkeypatch.setattr(execution, "_stamp_verification_outcome", stamp_mock)

    create_node_manager = AsyncMock()
    create_svc = _make_svc(publisher=event_bus, crud=mock_crud, node_manager=create_node_manager)
    create_context = SimpleNamespace(mode="create", save_device_id=uuid.uuid4(), transient_device=transient)
    outcome = await create_svc._finalize_failure(
        db,
        create_context,
        error="bad",
        job=job,
        node=node,
    )
    assert outcome.error == "cleanup failed"
    assert outcome.device_id is None

    locked = _device(name="changed")
    monkeypatch.setattr(execution, "_stop_verification_node_if_running", AsyncMock(return_value=None))
    monkeypatch.setattr(execution.device_locking, "lock_device", AsyncMock(return_value=locked))
    # The update-mode failure path also strips the operator:stop branding + stray
    # operator:start via a real IntentService; mock it for this db=MagicMock unit test.
    strip_revoke = AsyncMock()
    strip_intent_service = MagicMock(revoke_intents_and_reconcile=strip_revoke)
    monkeypatch.setattr(execution, "IntentService", MagicMock(return_value=strip_intent_service))
    mark_mock = AsyncMock(return_value=True)
    review_mock = MagicMock()
    review_mock.mark_review_required = mark_mock
    update_svc = _make_svc(publisher=event_bus, crud=mock_crud, node_manager=AsyncMock(), review=review_mock)
    update_context = SimpleNamespace(mode="update", save_device_id=locked.id, transient_device=transient)
    outcome = await update_svc._finalize_failure(
        db,
        update_context,
        error="bad",
        job=job,
        original_fields={"name": "original"},
    )
    assert outcome.device_id == str(locked.id)
    assert locked.name == "original"
    # Update-mode failure is reconciler-authoritative: the durable review_required
    # fact and outcome stamp precede the single merged revoke, which carries the
    # publisher for the derived emit.
    mark_mock.assert_awaited_once()
    stamp_mock.assert_awaited_once()
    strip_revoke.assert_awaited_once()
    strip_sources = strip_revoke.await_args.kwargs["sources"]
    assert f"verification:{locked.id}" in strip_sources
    assert f"operator:stop:node:{locked.id}" in strip_sources
    assert f"operator:start:{locked.id}" in strip_sources


async def test_execute_verification_context_missing_id_and_crash_path(monkeypatch: pytest.MonkeyPatch) -> None:
    db = MagicMock()
    db.commit = AsyncMock()
    job: dict[str, object] = {"stages": []}
    context = SimpleNamespace(save_device_id=None, transient_device=_device(identity_value="missing"))
    _s5 = FakeSettingsReader({})
    svc = VerificationExecutionService(
        review=build_review_service(),
        publisher=event_bus,
        agent=AgentCallContext(settings=_s5, circuit_breaker=Mock()),
        crud=DeviceCrudService(settings=_s5, identity=DeviceIdentityConflictService(), publisher=event_bus),
        viability=Mock(),
        capability=DeviceCapabilityService(),
        reconciler=AsyncMock(),
        node_manager=AsyncMock(),
    )
    with pytest.raises(NodeManagerError, match="no persisted device id"):
        await svc.execute_verification_context(
            job,
            db,
            context,
            http_client_factory=MagicMock(),
        )

    context = SimpleNamespace(
        mode="create",
        save_device_id=uuid.uuid4(),
        transient_device=_device(),
        save_payload={},
    )
    _s6 = FakeSettingsReader({})
    svc2 = VerificationExecutionService(
        review=build_review_service(),
        publisher=event_bus,
        agent=AgentCallContext(settings=_s6, circuit_breaker=Mock()),
        crud=DeviceCrudService(settings=_s6, identity=DeviceIdentityConflictService(), publisher=event_bus),
        viability=Mock(),
        capability=DeviceCapabilityService(),
        reconciler=AsyncMock(),
        node_manager=AsyncMock(),
    )
    finalize = AsyncMock(return_value=execution.VerificationExecutionOutcome(status="failed"))
    monkeypatch.setattr(svc2, "_finalize_failure", finalize)
    svc2.run_device_health = AsyncMock(side_effect=RuntimeError("crash"))  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="crash"):
        await svc2.execute_verification_context(
            job,
            db,
            context,
            http_client_factory=MagicMock(),
        )
    finalize.assert_awaited_once()


async def test_finalize_success_is_reconciler_authoritative_after_verified_at(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The PASS terminal transition is reconciler-authoritative: no direct
    ``set_operational_state`` push, and the outcome stamp precedes the hygiene
    revoke that carries the publisher for the derived ``available`` emit.
    """
    from app.devices.models import DeviceOperationalState
    from app.verification.services.preparation import PreparedVerificationContext

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
    )
    monkeypatch.setattr(execution.device_locking, "lock_device", AsyncMock(return_value=locked))
    monkeypatch.setattr(execution, "_restore_create_payload_fields", lambda *args: None)
    monkeypatch.setattr(execution, "set_stage", AsyncMock())
    monkeypatch.setattr(
        execution.remediation_log,
        "load_ladder",
        AsyncMock(return_value=execution.remediation_log.EMPTY_LADDER),
    )

    _mock_viability = AsyncMock()
    _mock_viability.record_session_viability_result = AsyncMock()

    publisher_when_revoked: list[object] = []

    async def revoke(_db: object, device: SimpleNamespace, *, publisher: object = None) -> None:
        publisher_when_revoked.append(publisher)

    monkeypatch.setattr(execution, "_revoke_verification_node_intent", revoke)
    stamp_mock = AsyncMock()
    monkeypatch.setattr(execution, "_stamp_verification_outcome", stamp_mock)

    svc = _make_svc(publisher=event_bus, crud=AsyncMock(), viability=_mock_viability)
    outcome = await svc._finalize_success(
        db,
        context,
        job={"stages": []},
        node=SimpleNamespace(port=4723, pid=22),
    )

    assert outcome.status == "completed"
    # PASS is reconciler-authoritative: the module no longer imports set_operational_state
    # (enforced by test_no_direct_device_state_writes).
    stamp_mock.assert_awaited_once()
    assert len(publisher_when_revoked) == 1
    assert publisher_when_revoked[0] is event_bus, "revoke must carry the publisher for the derived emit"


async def test_update_mode_verification_failure_shelves_device(monkeypatch: pytest.MonkeyPatch) -> None:
    """§14.4 — when an update-mode verification fails, the device must be
    shelved (review_required=True) before the transaction commits."""
    db = MagicMock()
    db.commit = AsyncMock()
    db.no_autoflush = MagicMock()
    db.no_autoflush.__enter__ = MagicMock()
    db.no_autoflush.__exit__ = MagicMock()
    job: dict[str, object] = {"stages": []}
    transient = _device()

    locked = _device(review_required=False, review_reason=None)
    monkeypatch.setattr(execution, "_stop_verification_node_if_running", AsyncMock(return_value=None))
    monkeypatch.setattr(execution.device_locking, "lock_device", AsyncMock(return_value=locked))
    stamp_mock = AsyncMock()
    monkeypatch.setattr(execution, "_stamp_verification_outcome", stamp_mock)
    # The update-mode failure path strips operator:stop/operator:start via a real
    # IntentService; mock it for this db=MagicMock unit test.
    strip_revoke = AsyncMock()
    monkeypatch.setattr(
        execution, "IntentService", MagicMock(return_value=MagicMock(revoke_intents_and_reconcile=strip_revoke))
    )

    mark_mock = AsyncMock(return_value=True)
    review_mock = MagicMock()
    review_mock.mark_review_required = mark_mock

    db.commit = AsyncMock()

    shelve_svc = _make_svc(publisher=event_bus, crud=AsyncMock(), node_manager=AsyncMock(), review=review_mock)
    update_context = SimpleNamespace(mode="update", save_device_id=locked.id, transient_device=transient)
    outcome = await shelve_svc._finalize_failure(
        db,
        update_context,
        error="adb probe timed out",
        job=job,
        original_fields={},
    )

    assert outcome.status == "failed"
    mark_mock.assert_awaited_once()
    call_args = mark_mock.call_args
    assert call_args.args[1] is locked  # mark_review_required(db, device, *, ...)
    assert "verification" in call_args.kwargs.get("reason", "")
    assert call_args.kwargs.get("source") == "verification"
    stamp_mock.assert_awaited_once()
    strip_revoke.assert_awaited_once()


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
            review=build_review_service(),
            publisher=event_bus,
            agent=AgentCallContext(settings=settings, circuit_breaker=Mock()),
            crud=DeviceCrudService(settings=settings, identity=DeviceIdentityConflictService(), publisher=event_bus),
            viability=Mock(),
            capability=DeviceCapabilityService(),
            reconciler=AsyncMock(),
            node_manager=AsyncMock(),
        ).run_device_health(job, device, http_client_factory=MagicMock())
        is None
    )
    assert fetch.await_args.kwargs["device_type"] == "real_device"
    assert fetch.await_args.kwargs["connection_type"] == "usb"
