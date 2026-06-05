from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.appium_nodes.exceptions import NodeManagerError, NodeStopNotAcknowledgedError
from app.appium_nodes.models import AppiumNode
from app.core.errors import AgentCallError, AgentUnreachableError
from app.devices.models import ConnectionType, Device, DeviceOperationalState, DeviceType
from app.devices.schemas.device import DeviceVerificationCreate, DeviceVerificationUpdate
from app.devices.services import state_write_guard
from app.devices.services.capability import DeviceCapabilityService
from app.devices.services.identity_conflicts import DeviceIdentityConflictService
from app.devices.services.service import DeviceCrudService
from app.hosts.models import Host
from app.verification.services import execution as execution
from app.verification.services import preparation as preparation
from app.verification.services.execution import VerificationExecutionService
from app.verification.services.job_state import new_job
from app.verification.services.preparation import PreparedVerificationContext, VerificationPreparationService
from app.verification.services.service import VerificationService
from tests.fakes import FakeSettingsReader, build_review_service
from tests.helpers import create_device_record
from tests.helpers import test_event_bus as event_bus

pytestmark = pytest.mark.usefixtures("seeded_driver_packs")


def _job() -> dict[str, object]:
    return new_job("unit-job")


def _device(host: Host | None = None) -> Device:
    with state_write_guard.bypass():
        device = Device(
            pack_id="appium-uiautomator2",
            platform_id="android_mobile",
            identity_scheme="android_serial",
            identity_scope="host",
            identity_value="verify-unit-001",
            connection_target="verify-unit-001",
            name="Verify Unit",
            os_version="14",
            host_id=host.id if host else None,
            operational_state=DeviceOperationalState.available,
            device_type=DeviceType.real_device,
            connection_type=ConnectionType.usb,
        )
    if host is not None:
        device.host = host
    return device


async def test_device_verification_job_lookup_guards() -> None:
    svc = VerificationService()
    assert await svc.get_verification_job("not-a-uuid", session_factory=AsyncMock()) is None

    class Session:
        async def __aenter__(self) -> "Session":
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def get(self, *_args: object) -> object:
            return SimpleNamespace(kind="other", snapshot={"status": "queued"})

    assert await svc.get_verification_job(str(__import__("uuid").uuid4()), session_factory=Session) is None


async def test_run_device_health_covers_skip_agent_success_and_failure(
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.verification.services.job_state.publish", AsyncMock())
    job = _job()
    svc = VerificationExecutionService(
        review=build_review_service(),
        publisher=event_bus,
        settings=FakeSettingsReader({}),
        circuit_breaker=Mock(),
        crud=DeviceCrudService(
            settings=FakeSettingsReader({}), identity=DeviceIdentityConflictService(), publisher=event_bus
        ),
        viability=Mock(),
        capability=DeviceCapabilityService(),
        reconciler=AsyncMock(),
        node_manager=AsyncMock(),
    )
    assert await svc.run_device_health(job, _device(None), http_client_factory=object) is None
    assert job["current_stage"] == "device_health"
    assert job["stages"][1]["status"] == "skipped"

    device = _device(db_host)
    monkeypatch.setattr(
        "app.verification.services.execution.fetch_pack_device_health",
        AsyncMock(side_effect=AgentCallError("10.0.0.1", "down")),
    )
    detail = await VerificationExecutionService(
        review=build_review_service(),
        publisher=event_bus,
        settings=FakeSettingsReader({}),
        circuit_breaker=Mock(),
        crud=DeviceCrudService(
            settings=FakeSettingsReader({}), identity=DeviceIdentityConflictService(), publisher=event_bus
        ),
        viability=Mock(),
        capability=DeviceCapabilityService(),
        reconciler=AsyncMock(),
        node_manager=AsyncMock(),
    ).run_device_health(_job(), device, http_client_factory=object)
    assert detail == "Agent health check failed: down"

    monkeypatch.setattr(
        "app.verification.services.execution.fetch_pack_device_health",
        AsyncMock(return_value={"healthy": True, "avd_launched": {"serial": "emulator-5554"}}),
    )
    assert (
        await VerificationExecutionService(
            review=build_review_service(),
            publisher=event_bus,
            settings=FakeSettingsReader({}),
            circuit_breaker=Mock(),
            crud=DeviceCrudService(
                settings=FakeSettingsReader({}), identity=DeviceIdentityConflictService(), publisher=event_bus
            ),
            viability=Mock(),
            capability=DeviceCapabilityService(),
            reconciler=AsyncMock(),
            node_manager=AsyncMock(),
        ).run_device_health(_job(), device, http_client_factory=object)
        is None
    )
    assert device.connection_target == "emulator-5554"

    monkeypatch.setattr(
        "app.verification.services.execution.fetch_pack_device_health",
        AsyncMock(return_value={"healthy": False, "checks": [{"check_id": "adb_ready", "ok": False, "message": "no"}]}),
    )
    assert (
        await VerificationExecutionService(
            review=build_review_service(),
            publisher=event_bus,
            settings=FakeSettingsReader({}),
            circuit_breaker=Mock(),
            crud=DeviceCrudService(
                settings=FakeSettingsReader({}), identity=DeviceIdentityConflictService(), publisher=event_bus
            ),
            viability=Mock(),
            capability=DeviceCapabilityService(),
            reconciler=AsyncMock(),
            node_manager=AsyncMock(),
        ).run_device_health(_job(), device, http_client_factory=object)
        == "adb ready failed (no)"
    )


async def test_stop_existing_node_and_run_probe_failure_paths(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.verification.services.job_state.publish", AsyncMock())
    existing = await create_device_record(
        db_session,
        host_id=db_host.id,
        identity_value="verify-existing-node",
        connection_target="verify-existing-node",
        name="Verify Existing",
        operational_state=DeviceOperationalState.available,
    )
    with state_write_guard.bypass():
        node = AppiumNode(
            device_id=existing.id, port=4723, grid_url="http://grid", pid=1, active_connection_target="live"
        )
    existing.appium_node = node
    context = PreparedVerificationContext(
        mode="update",
        transient_device=existing,
        save_payload={},
        existing_device=existing,
        save_device_id=existing.id,
    )

    monkeypatch.setattr(
        "app.verification.services.execution._stop_managed_node_for_verification",
        AsyncMock(side_effect=NodeManagerError("stop failed")),
    )
    detail = await VerificationExecutionService(
        review=build_review_service(),
        publisher=event_bus,
        settings=FakeSettingsReader({}),
        circuit_breaker=Mock(),
        crud=DeviceCrudService(
            settings=FakeSettingsReader({}), identity=DeviceIdentityConflictService(), publisher=event_bus
        ),
        viability=Mock(),
        capability=DeviceCapabilityService(),
        reconciler=AsyncMock(),
        node_manager=AsyncMock(),
    ).stop_existing_managed_node_for_update(_job(), db_session, context)
    assert detail is not None
    assert "stop failed" in detail

    nm_start_err = AsyncMock()
    nm_start_err.start_node = AsyncMock(side_effect=NodeManagerError("no node"))
    started, error = await VerificationExecutionService(
        review=build_review_service(),
        publisher=event_bus,
        settings=FakeSettingsReader({}),
        circuit_breaker=Mock(),
        crud=DeviceCrudService(
            settings=FakeSettingsReader({}), identity=DeviceIdentityConflictService(), publisher=event_bus
        ),
        viability=Mock(),
        capability=DeviceCapabilityService(),
        reconciler=AsyncMock(),
        node_manager=nm_start_err,
    ).run_probe(
        _job(),
        db_session,
        existing,
        probe_session_fn=AsyncMock(),
    )
    assert started is None
    assert error == "no node"

    with state_write_guard.bypass():
        fake_node = AppiumNode(id=__import__("uuid").uuid4(), device_id=existing.id, port=4723, grid_url="http://grid")
    nm_timeout = AsyncMock()
    nm_timeout.start_node = AsyncMock(return_value=fake_node)
    nm_timeout.wait_for_node_running = AsyncMock(return_value=None)
    started, error = await VerificationExecutionService(
        review=build_review_service(),
        publisher=event_bus,
        settings=FakeSettingsReader({}),
        circuit_breaker=Mock(),
        crud=DeviceCrudService(
            settings=FakeSettingsReader({}), identity=DeviceIdentityConflictService(), publisher=event_bus
        ),
        viability=Mock(),
        capability=DeviceCapabilityService(),
        reconciler=AsyncMock(),
        node_manager=nm_timeout,
    ).run_probe(
        _job(),
        db_session,
        existing,
        probe_session_fn=AsyncMock(),
    )
    assert started is fake_node
    assert error == "Verification node did not reach running state within timeout"

    with state_write_guard.bypass():
        running_node = AppiumNode(
            id=__import__("uuid").uuid4(),
            device_id=existing.id,
            port=4723,
            grid_url="http://grid",
            pid=1,
            active_connection_target="live",
        )
    nm_probe_fail = AsyncMock()
    nm_probe_fail.start_node = AsyncMock(return_value=running_node)
    nm_probe_fail.wait_for_node_running = AsyncMock(return_value=running_node)
    monkeypatch.setattr(
        "app.devices.services.capability.DeviceCapabilityService.get_device_capabilities",
        AsyncMock(return_value={"platformName": "Android"}),
    )
    probe_session = AsyncMock(return_value=(False, "probe failed"))
    started, error = await VerificationExecutionService(
        review=build_review_service(),
        publisher=event_bus,
        settings=FakeSettingsReader({}),
        circuit_breaker=Mock(),
        crud=DeviceCrudService(
            settings=FakeSettingsReader({}), identity=DeviceIdentityConflictService(), publisher=event_bus
        ),
        viability=Mock(),
        capability=DeviceCapabilityService(),
        reconciler=AsyncMock(),
        node_manager=nm_probe_fail,
    ).run_probe(
        _job(),
        db_session,
        existing,
        probe_session_fn=probe_session,
    )
    assert started is running_node
    assert error == "probe failed"
    probe_session.assert_awaited_once_with(
        {"platformName": "Android"},
        120,
        target=f"http://{db_host.ip}:4723",
    )

    from sqlalchemy import select

    from app.sessions.models import Session
    from app.sessions.probe_constants import PROBE_TEST_NAME
    from app.sessions.service_probes import PROBE_CHECKED_BY_CAP_KEY

    rows = (
        (
            await db_session.execute(
                select(Session).where(Session.device_id == existing.id, Session.test_name == PROBE_TEST_NAME)
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].requested_capabilities is not None
    assert rows[0].requested_capabilities[PROBE_CHECKED_BY_CAP_KEY] == "verification"


async def test_run_probe_drives_immediate_convergence_after_start_node(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """run_probe must kick converge_device_now after start_node so verification does
    not wait for the next reconciler tick. Without this, wait_for_node_running races
    appium_reconciler.interval_sec (default 30s) against appium.startup_timeout_sec
    (default 30s) — a common cause of `node_start failed` → cleanup skipped for new
    devices.
    """
    monkeypatch.setattr("app.verification.services.job_state.publish", AsyncMock())
    existing = await create_device_record(
        db_session,
        host_id=db_host.id,
        identity_value="verify-converge-immediate",
        connection_target="verify-converge-immediate",
        name="Verify Converge",
        operational_state=DeviceOperationalState.available,
    )
    with state_write_guard.bypass():
        fake_node = AppiumNode(id=__import__("uuid").uuid4(), device_id=existing.id, port=4723, grid_url="http://grid")
    nm_converge = AsyncMock()
    nm_converge.start_node = AsyncMock(return_value=fake_node)
    nm_converge.wait_for_node_running = AsyncMock(return_value=None)
    converge_mock = AsyncMock(return_value=None)

    await VerificationExecutionService(
        review=build_review_service(),
        publisher=Mock(),
        settings=FakeSettingsReader({}),
        circuit_breaker=Mock(),
        crud=DeviceCrudService(
            settings=FakeSettingsReader({}), identity=DeviceIdentityConflictService(), publisher=event_bus
        ),
        viability=Mock(),
        capability=DeviceCapabilityService(),
        reconciler=SimpleNamespace(converge_device_now=converge_mock),
        node_manager=nm_converge,
    ).run_probe(
        _job(),
        db_session,
        existing,
        probe_session_fn=AsyncMock(),
    )

    converge_mock.assert_awaited_once()
    call_args = converge_mock.await_args
    assert call_args is not None
    assert call_args.args[0] == existing.id


async def test_run_probe_swallows_transient_converge_kick_failure(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A self-healing transient from the immediate convergence kick (e.g. the
    agent hasn't acknowledged a stop yet) must not fail the probe — the
    reconciler tick is the durable fallback. The kick is best-effort."""
    monkeypatch.setattr("app.verification.services.job_state.publish", AsyncMock())
    existing = await create_device_record(
        db_session,
        host_id=db_host.id,
        identity_value="verify-converge-transient",
        connection_target="verify-converge-transient",
        name="Verify Converge Transient",
        operational_state=DeviceOperationalState.available,
    )
    with state_write_guard.bypass():
        fake_node = AppiumNode(id=__import__("uuid").uuid4(), device_id=existing.id, port=4723, grid_url="http://grid")
    nm = AsyncMock()
    nm.start_node = AsyncMock(return_value=fake_node)
    nm.wait_for_node_running = AsyncMock(return_value=None)
    converge_mock = AsyncMock(side_effect=NodeStopNotAcknowledgedError("agent did not acknowledge stop"))

    # Must not raise despite the transient kick failure.
    _node, error = await VerificationExecutionService(
        review=build_review_service(),
        publisher=Mock(),
        settings=FakeSettingsReader({}),
        circuit_breaker=Mock(),
        crud=DeviceCrudService(
            settings=FakeSettingsReader({}), identity=DeviceIdentityConflictService(), publisher=event_bus
        ),
        viability=Mock(),
        capability=DeviceCapabilityService(),
        reconciler=SimpleNamespace(converge_device_now=converge_mock),
        node_manager=nm,
    ).run_probe(
        _job(),
        db_session,
        existing,
        probe_session_fn=AsyncMock(),
    )

    converge_mock.assert_awaited_once()
    # wait_for_node_running returned None → node_start times out gracefully.
    assert error is not None


async def test_run_probe_marks_device_inflight_during_probe_session(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """run_probe must register the device in ``probe_inflight`` while the Grid
    probe session is open, so session_sync ignores the slot. The Appium driver
    strips ``gridfleet:*`` markers from matched caps, so the registry is the
    only mechanism that can identify a verification probe's slot."""
    from app.sessions import probe_inflight

    monkeypatch.setattr("app.verification.services.job_state.publish", AsyncMock())
    existing = await create_device_record(
        db_session,
        host_id=db_host.id,
        identity_value="verify-inflight",
        connection_target="verify-inflight",
        name="Verify Inflight",
        operational_state=DeviceOperationalState.available,
    )
    with state_write_guard.bypass():
        running_node = AppiumNode(
            id=__import__("uuid").uuid4(),
            device_id=existing.id,
            port=4723,
            grid_url="http://grid",
            pid=1,
            active_connection_target="live",
        )
    nm_inflight = AsyncMock()
    nm_inflight.start_node = AsyncMock(return_value=running_node)
    nm_inflight.wait_for_node_running = AsyncMock(return_value=running_node)
    monkeypatch.setattr(
        "app.devices.services.capability.DeviceCapabilityService.get_device_capabilities",
        AsyncMock(return_value={"platformName": "Android"}),
    )

    device_key = str(existing.id)
    seen_inflight: list[bool] = []

    async def fake_probe_session(_caps: object, _timeout: int, *, target: str | None) -> tuple[bool, None]:
        seen_inflight.append(probe_inflight.is_probe_inflight(device_key))
        return True, None

    assert probe_inflight.is_probe_inflight(device_key) is False
    await VerificationExecutionService(
        review=build_review_service(),
        publisher=event_bus,
        settings=FakeSettingsReader({}),
        circuit_breaker=Mock(),
        crud=DeviceCrudService(
            settings=FakeSettingsReader({}), identity=DeviceIdentityConflictService(), publisher=event_bus
        ),
        viability=Mock(),
        capability=DeviceCapabilityService(),
        reconciler=AsyncMock(),
        node_manager=nm_inflight,
    ).run_probe(
        _job(),
        db_session,
        existing,
        probe_session_fn=fake_probe_session,
    )
    assert seen_inflight == [True]
    assert probe_inflight.is_probe_inflight(device_key) is False


async def test_run_probe_clears_inflight_when_probe_session_raises(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exceptions inside the probe must not leak inflight entries."""
    from app.sessions import probe_inflight

    monkeypatch.setattr("app.verification.services.job_state.publish", AsyncMock())
    existing = await create_device_record(
        db_session,
        host_id=db_host.id,
        identity_value="verify-inflight-raises",
        connection_target="verify-inflight-raises",
        name="Verify Inflight Raises",
        operational_state=DeviceOperationalState.available,
    )
    with state_write_guard.bypass():
        running_node = AppiumNode(
            id=__import__("uuid").uuid4(),
            device_id=existing.id,
            port=4723,
            grid_url="http://grid",
            pid=1,
            active_connection_target="live",
        )
    nm_raises = AsyncMock()
    nm_raises.start_node = AsyncMock(return_value=running_node)
    nm_raises.wait_for_node_running = AsyncMock(return_value=running_node)
    monkeypatch.setattr(
        "app.devices.services.capability.DeviceCapabilityService.get_device_capabilities",
        AsyncMock(return_value={"platformName": "Android"}),
    )

    device_key = str(existing.id)

    async def failing_probe_session(_caps: object, _timeout: int, *, target: str | None) -> tuple[bool, str | None]:
        raise RuntimeError("probe blew up")

    with pytest.raises(RuntimeError, match="probe blew up"):
        await VerificationExecutionService(
            review=build_review_service(),
            publisher=event_bus,
            settings=FakeSettingsReader({}),
            circuit_breaker=Mock(),
            crud=DeviceCrudService(
                settings=FakeSettingsReader({}), identity=DeviceIdentityConflictService(), publisher=event_bus
            ),
            viability=Mock(),
            capability=DeviceCapabilityService(),
            reconciler=AsyncMock(),
            node_manager=nm_raises,
        ).run_probe(
            _job(),
            db_session,
            existing,
            probe_session_fn=failing_probe_session,
        )
    assert probe_inflight.is_probe_inflight(device_key) is False


async def test_stop_verification_node_cleanup_error_path(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.verification.services.job_state.publish", AsyncMock())
    device = await create_device_record(
        db_session,
        host_id=db_host.id,
        identity_value="verify-save-001",
        connection_target="verify-save-001",
        name="Verify Save",
    )

    with state_write_guard.bypass():
        node = AppiumNode(device_id=device.id, port=4723, grid_url="http://grid")
    nm_stop_boom = AsyncMock()
    nm_stop_boom.stop_node = AsyncMock(side_effect=RuntimeError("boom"))
    cleanup_error = await execution._stop_verification_node_if_running(_job(), db_session, device, node, nm_stop_boom)
    assert cleanup_error == "Failed to stop verification node: boom"
    assert node.pid is None


async def test_verification_execution_remaining_error_branches(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.verification.services.job_state.publish", AsyncMock())
    device = await create_device_record(
        db_session,
        host_id=db_host.id,
        identity_value="verify-remaining-001",
        connection_target="verify-remaining-001",
        name="Verify Remaining",
    )

    fake_db = AsyncMock()
    with pytest.raises(NodeManagerError, match="No running node"):
        await execution._stop_managed_node_for_verification(
            fake_db,
            SimpleNamespace(id=__import__("uuid").uuid4(), appium_node=None),
        )

    with state_write_guard.bypass():
        node = AppiumNode(
            device_id=device.id, port=4723, grid_url="http://grid", pid=123, active_connection_target="live"
        )
    fake_device = SimpleNamespace(id=device.id, appium_node=node)
    monkeypatch.setattr("app.verification.services.execution.write_desired_state", AsyncMock())
    stopped = await execution._stop_managed_node_for_verification(fake_db, fake_device)
    assert stopped is node
    assert node.pid is None

    nm_stop_already = AsyncMock()
    nm_stop_already.stop_node = AsyncMock(side_effect=NodeManagerError("already stopped"))
    assert await execution._stop_verification_node_if_running(_job(), db_session, device, node, nm_stop_already) is None

    target = _device(db_host)
    execution._restore_create_payload_fields(
        target,
        {
            "pack_id": "pack",
            "platform_id": "platform",
            "identity_scheme": "scheme",
            "identity_scope": "scope",
            "identity_value": "identity",
            "connection_target": "target",
            "name": "Restored",
            "os_version": "15",
            "manufacturer": "Maker",
            "model": "Model",
            "model_number": "M1",
            "software_versions": {"driver": "1"},
            "device_type": DeviceType.emulator,
            "connection_type": ConnectionType.virtual,
            "ip_address": None,
            "device_config": {"a": 1},
            "ignored": "value",
        },
    )
    assert target.name == "Restored"
    assert target.device_config == {"a": 1}


async def test_finalize_and_execute_success_guard_branches(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.verification.services.job_state.publish", AsyncMock())
    db = AsyncMock()
    device_id = __import__("uuid").uuid4()
    created = SimpleNamespace(id=device_id, verified_at=None)
    context = PreparedVerificationContext(
        mode="create",
        transient_device=created,
        save_payload={
            "pack_id": "appium-uiautomator2",
            "platform_id": "android_mobile",
            "identity_scheme": "android_serial",
            "identity_scope": "host",
            "identity_value": "create-save",
            "connection_target": "create-save",
            "name": "Create Save",
            "host_id": __import__("uuid").uuid4(),
        },
        save_device_id=device_id,
    )

    target = SimpleNamespace(name="unchanged")
    execution._restore_update_original_fields(target, None)
    assert target.name == "unchanged"

    context.mode = "update"
    mock_crud_none = AsyncMock()
    mock_crud_none.update_device = AsyncMock(return_value=None)
    failed = await execution._finalize_success(
        db,
        context,
        job=_job(),
        node=None,
        publisher=event_bus,
        crud=mock_crud_none,
        viability=AsyncMock(),
        node_manager=AsyncMock(),
    )
    assert failed.status == "failed"
    assert failed.error == "Device was not found"

    svc = VerificationExecutionService(
        review=build_review_service(),
        publisher=event_bus,
        settings=FakeSettingsReader({}),
        circuit_breaker=Mock(),
        crud=DeviceCrudService(
            settings=FakeSettingsReader({}), identity=DeviceIdentityConflictService(), publisher=event_bus
        ),
        viability=Mock(),
        capability=DeviceCapabilityService(),
        reconciler=AsyncMock(),
        node_manager=AsyncMock(),
    )
    svc.stop_existing_managed_node_for_update = AsyncMock(return_value="stop failed")  # type: ignore[method-assign]
    outcome = await svc.execute_verification_context(
        _job(),
        db,
        context,
        http_client_factory=object,
        probe_session_fn=AsyncMock(),
    )
    assert outcome.status == "failed"
    assert outcome.error == "stop failed"


async def test_finalize_success_and_execute_update_branches(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.verification.services.job_state.publish", AsyncMock())
    db = AsyncMock()
    device_id = __import__("uuid").uuid4()
    locked = SimpleNamespace(
        id=device_id,
        operational_state=DeviceOperationalState.offline,
        verified_at=None,
    )
    context = PreparedVerificationContext(
        mode="update",
        transient_device=locked,
        save_payload={"name": "updated", "host_id": __import__("uuid").uuid4()},
        save_device_id=device_id,
    )
    monkeypatch.setattr("app.verification.services.execution._revoke_verification_node_intent", AsyncMock())
    machine_spy = AsyncMock()
    monkeypatch.setattr("app.verification.services.execution.set_operational_state", machine_spy)
    mock_crud_upd = AsyncMock()
    mock_crud_upd.update_device = AsyncMock(return_value=locked)

    node = SimpleNamespace(port=4723, pid=22)
    viability_mock = AsyncMock()
    viability_mock.record_session_viability_result = AsyncMock()
    outcome = await execution._finalize_success(
        db,
        context,
        job=_job(),
        node=node,
        publisher=event_bus,
        crud=mock_crud_upd,
        viability=viability_mock,
        node_manager=AsyncMock(),
    )
    assert outcome.status == "completed"
    # PASS is reconciler-authoritative: the revoke (carrying the publisher) is the writer,
    # not a direct set_operational_state.
    machine_spy.assert_not_awaited()
    execution._revoke_verification_node_intent.assert_awaited()

    update_device = SimpleNamespace(id=device_id, identity_value="update-device", name="old")
    update_context = PreparedVerificationContext(
        mode="update",
        transient_device=update_device,
        save_payload={"name": "new", "replace_device_config": True},
        save_device_id=device_id,
    )
    monkeypatch.setattr(
        "app.verification.services.execution.device_locking.lock_device", AsyncMock(return_value=update_device)
    )
    monkeypatch.setattr("app.verification.services.execution._finalize_failure", AsyncMock())

    svc2 = VerificationExecutionService(
        review=build_review_service(),
        publisher=event_bus,
        settings=FakeSettingsReader({}),
        circuit_breaker=Mock(),
        crud=DeviceCrudService(
            settings=FakeSettingsReader({}), identity=DeviceIdentityConflictService(), publisher=event_bus
        ),
        viability=Mock(),
        capability=DeviceCapabilityService(),
        reconciler=AsyncMock(),
        node_manager=AsyncMock(),
    )
    svc2.stop_existing_managed_node_for_update = AsyncMock(return_value=None)  # type: ignore[method-assign]
    svc2.run_device_health = AsyncMock(side_effect=RuntimeError("boom"))  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="boom"):
        await svc2.execute_verification_context(
            _job(),
            db,
            update_context,
            http_client_factory=object,
            probe_session_fn=AsyncMock(),
        )

    assert update_device.name == "new"
    execution._finalize_failure.assert_awaited()


async def test_preparation_resolution_and_validation_error_paths(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.verification.services.job_state.publish", AsyncMock())
    assert preparation._is_transport_identity(None, None, None) is True
    assert preparation._payload_requests_virtual_lane({"device_type": "emulator"}) is True
    assert (
        preparation.build_transient_device(
            {
                "pack_id": "appium-uiautomator2",
                "platform_id": "android_mobile",
                "identity_scheme": "android_serial",
                "identity_scope": "host",
                "identity_value": "transient",
                "connection_target": "transient",
                "name": "transient",
                "os_version": "14",
                "device_type": "real_device",
                "connection_type": "usb",
            },
            db_host,
        ).host_id
        == db_host.id
    )

    _prep_svc = VerificationPreparationService(
        settings=FakeSettingsReader({}),
        circuit_breaker=Mock(),
        crud=DeviceCrudService(
            settings=FakeSettingsReader({}), identity=DeviceIdentityConflictService(), publisher=event_bus
        ),
        identity=DeviceIdentityConflictService(),
    )
    assert await _prep_svc.resolve_host_derived_payload({}, None, http_client_factory=object, db=db_session) == (
        "Assigned host is required"
    )

    monkeypatch.setattr(
        "app.verification.services.preparation.resolve_pack_platform",
        AsyncMock(
            return_value=SimpleNamespace(
                pack_id="pack",
                release="1",
                platform_id="platform",
                connection_behavior={"host_resolution_action": "resolve"},
            )
        ),
    )
    monkeypatch.setattr(
        "app.verification.services.preparation.normalize_pack_device",
        AsyncMock(return_value={"field_errors": [{"field_id": "serial", "message": "bad"}]}),
    )
    payload = {
        "pack_id": "pack",
        "platform_id": "platform",
        "identity_value": "10.0.0.1:5555",
        "connection_target": "10.0.0.1:5555",
        "device_type": DeviceType.real_device,
        "connection_type": ConnectionType.network,
    }
    assert await _prep_svc.resolve_host_derived_payload(
        payload,
        db_host,
        http_client_factory=object,
        db=db_session,
    ) == ("serial: bad")

    monkeypatch.setattr(
        "app.verification.services.preparation.normalize_pack_device",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "app.verification.services.preparation.pack_device_lifecycle_action",
        AsyncMock(side_effect=AgentCallError("10.0.0.1", "404 resolve")),
    )
    assert await _prep_svc.resolve_host_derived_payload(
        payload,
        db_host,
        http_client_factory=object,
        db=db_session,
    ) == ("Device must resolve to a stable identity before save (action: resolve)")

    monkeypatch.setattr(
        "app.verification.services.preparation.pack_device_lifecycle_action",
        AsyncMock(return_value={"identity_value": "stable", "connection_target": "10.0.0.1:5555", "name": "Resolved"}),
    )
    assert (
        await _prep_svc.resolve_host_derived_payload(
            payload,
            db_host,
            http_client_factory=object,
            db=db_session,
        )
        is None
    )
    assert payload["identity_value"] == "stable"
    assert payload["name"] == "Resolved"

    bad_create = DeviceVerificationCreate(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value="missing-host",
        connection_target="missing-host",
        name="Missing Host",
        os_version="14",
        host_id=__import__("uuid").uuid4(),
    )
    _prep_svc2 = VerificationPreparationService(
        settings=FakeSettingsReader(),
        circuit_breaker=Mock(),
        crud=DeviceCrudService(
            settings=FakeSettingsReader(), identity=DeviceIdentityConflictService(), publisher=event_bus
        ),
        identity=DeviceIdentityConflictService(),
    )
    context, error = await _prep_svc2.validate_create_request(
        _job(),
        db_session,
        bad_create,
        http_client_factory=object,
    )
    assert context is None
    assert error == "Assigned host was not found"

    context, error = await _prep_svc2.validate_update_request(
        _job(),
        db_session,
        __import__("uuid").uuid4(),
        DeviceVerificationUpdate(name="missing", host_id=db_host.id),
        http_client_factory=object,
    )
    assert context is None
    assert error == "Device was not found"


async def test_preparation_more_resolution_and_create_conflict_branches(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.verification.services.job_state.publish", AsyncMock())
    assert await preparation._load_host(db_session, None) is None
    assert preparation._is_transport_identity("10.0.0.5", "other", "10.0.0.5") is True

    monkeypatch.setattr(
        "app.verification.services.preparation.resolve_pack_platform",
        AsyncMock(
            return_value=SimpleNamespace(
                pack_id="pack",
                release="1",
                platform_id="platform",
                connection_behavior={},
            )
        ),
    )
    _prep_svc = VerificationPreparationService(
        settings=FakeSettingsReader({}),
        circuit_breaker=Mock(),
        crud=DeviceCrudService(
            settings=FakeSettingsReader({}), identity=DeviceIdentityConflictService(), publisher=event_bus
        ),
        identity=DeviceIdentityConflictService(),
    )
    for exc in (AgentCallError("10.0.0.1", "down"), LookupError("missing"), TypeError("bad mock")):
        monkeypatch.setattr(
            "app.verification.services.preparation.normalize_pack_device",
            AsyncMock(side_effect=exc),
        )
        assert (
            await _prep_svc.resolve_host_derived_payload(
                {
                    "pack_id": "pack",
                    "platform_id": "platform",
                    "identity_value": "stable",
                    "connection_target": "stable",
                    "device_type": DeviceType.real_device,
                    "connection_type": ConnectionType.usb,
                },
                db_host,
                http_client_factory=object,
                db=db_session,
            )
            is None
        )

    monkeypatch.setattr(
        "app.verification.services.preparation.normalize_pack_device",
        AsyncMock(return_value={"field_errors": ["bad"]}),
    )
    assert (
        await _prep_svc.resolve_host_derived_payload(
            {
                "pack_id": "pack",
                "platform_id": "platform",
                "identity_value": "stable",
                "connection_target": "stable",
                "device_type": DeviceType.real_device,
                "connection_type": ConnectionType.usb,
            },
            db_host,
            http_client_factory=object,
            db=db_session,
        )
        == "Adapter rejected device input"
    )

    payload = {
        "pack_id": "appium-uiautomator2",
        "platform_id": "android_mobile",
        "identity_scheme": "android_serial",
        "identity_scope": "host",
        "identity_value": "10.0.0.8:5555",
        "connection_target": "10.0.0.8:5555",
        "name": "Create Conflict",
        "os_version": "14",
        "host_id": db_host.id,
        "device_type": DeviceType.real_device,
        "connection_type": ConnectionType.network,
        "ip_address": "10.0.0.8",
    }
    monkeypatch.setattr(
        "app.verification.services.preparation.device_write.prepare_device_create_payload_async",
        AsyncMock(return_value=dict(payload)),
    )
    _prep_svc2 = VerificationPreparationService(
        settings=FakeSettingsReader(),
        circuit_breaker=Mock(),
        crud=DeviceCrudService(
            settings=FakeSettingsReader(), identity=DeviceIdentityConflictService(), publisher=event_bus
        ),
        identity=DeviceIdentityConflictService(),
    )
    monkeypatch.setattr(
        _prep_svc2._identity,
        "ensure_device_payload_identity_available",
        AsyncMock(side_effect=preparation.DeviceIdentityConflictError("late duplicate")),
    )
    _prep_svc2.resolve_host_derived_payload = AsyncMock(return_value=None)  # type: ignore[method-assign]
    context, error = await _prep_svc2.validate_create_request(
        _job(),
        db_session,
        DeviceVerificationCreate(**payload),
        http_client_factory=object,
    )
    assert context is None
    assert error == "late duplicate"

    monkeypatch.setattr(
        "app.verification.services.preparation.device_write.prepare_device_create_payload_async",
        AsyncMock(side_effect=ValueError("bad create")),
    )
    _prep_svc3 = VerificationPreparationService(
        settings=FakeSettingsReader(),
        circuit_breaker=Mock(),
        crud=DeviceCrudService(
            settings=FakeSettingsReader(), identity=DeviceIdentityConflictService(), publisher=event_bus
        ),
        identity=DeviceIdentityConflictService(),
    )
    context, error = await _prep_svc3.validate_create_request(
        _job(),
        db_session,
        DeviceVerificationCreate(**payload),
        http_client_factory=object,
    )
    assert context is None
    assert error == "bad create"


async def test_preparation_normalization_success_and_resolution_errors(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "app.verification.services.preparation.resolve_pack_platform",
        AsyncMock(
            return_value=SimpleNamespace(
                pack_id="pack",
                release="1",
                platform_id="platform",
                identity_scheme="resolved_scheme",
                identity_scope="resolved_scope",
                connection_behavior={"host_resolution_action": "resolve"},
            )
        ),
    )
    payload = {
        "pack_id": "pack",
        "platform_id": "platform",
        "identity_value": "10.0.0.2:5555",
        "connection_target": "10.0.0.2:5555",
        "name": "stable-serial",
        "device_type": DeviceType.real_device,
        "connection_type": ConnectionType.network,
        "ip_address": "10.0.0.2",
    }
    monkeypatch.setattr(
        "app.verification.services.preparation.normalize_pack_device",
        AsyncMock(
            return_value={
                "identity_scheme": "adapter_serial",
                "identity_scope": "host",
                "identity_value": "stable-serial",
                "connection_target": "stable-target",
                "os_version": "15",
                "manufacturer": "Maker",
                "model": "Model X",
                "model_number": "MX",
                "software_versions": {"os": "15"},
                "device_type": "real_device",
                "connection_type": "network",
                "ip_address": "10.0.0.2",
            }
        ),
    )
    monkeypatch.setattr(
        "app.verification.services.preparation.pack_device_lifecycle_action",
        AsyncMock(return_value={"identity_value": "resolved-stable"}),
    )

    _prep_svc = VerificationPreparationService(
        settings=FakeSettingsReader({}),
        circuit_breaker=Mock(),
        crud=DeviceCrudService(
            settings=FakeSettingsReader({}), identity=DeviceIdentityConflictService(), publisher=event_bus
        ),
        identity=DeviceIdentityConflictService(),
    )
    assert (
        await _prep_svc.resolve_host_derived_payload(
            payload,
            db_host,
            http_client_factory=object,
            db=db_session,
        )
        is None
    )
    assert payload["identity_value"] == "stable-serial"
    assert payload["manufacturer"] == "Maker"
    assert payload["name"] == "Model X"
    # Regression: agent normalize returns plain string device_type / connection_type.
    # resolve_host_derived_payload must coerce them back to enum types so later
    # `setattr` onto Mapped[Enum] columns does not corrupt the device row.
    assert isinstance(payload["device_type"], DeviceType)
    assert isinstance(payload["connection_type"], ConnectionType)

    monkeypatch.setattr(
        "app.verification.services.preparation.normalize_pack_device",
        AsyncMock(return_value=None),
    )
    assert (
        await _prep_svc.resolve_host_derived_payload(
            {
                "pack_id": "pack",
                "platform_id": "platform",
                "identity_value": "10.0.0.22:5555",
                "connection_target": "10.0.0.22:5555",
                "device_type": DeviceType.real_device,
                "connection_type": ConnectionType.network,
            },
            db_host,
            http_client_factory=object,
            db=db_session,
        )
        is None
    )

    monkeypatch.setattr(
        "app.verification.services.preparation.pack_device_lifecycle_action",
        AsyncMock(side_effect=AgentUnreachableError("10.0.0.20", "agent down")),
    )
    assert (
        await _prep_svc.resolve_host_derived_payload(
            {
                "pack_id": "pack",
                "platform_id": "platform",
                "identity_value": "10.0.0.3:5555",
                "connection_target": "10.0.0.3:5555",
                "device_type": DeviceType.real_device,
                "connection_type": ConnectionType.network,
            },
            db_host,
            http_client_factory=object,
            db=db_session,
        )
        == "Host resolution failed: agent down"
    )

    assert await preparation._payload_needs_host_resolution(db_session, {"pack_id": "", "platform_id": "p"}) == (
        False,
        None,
    )
    monkeypatch.setattr(
        "app.verification.services.preparation.resolve_pack_platform",
        AsyncMock(side_effect=LookupError("missing")),
    )
    assert await preparation._payload_needs_host_resolution(
        db_session,
        {"pack_id": "missing", "platform_id": "missing"},
    ) == (False, None)


async def test_preparation_validation_conflict_and_update_branches(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base_payload = {
        "pack_id": "appium-uiautomator2",
        "platform_id": "android_mobile",
        "identity_scheme": "android_serial",
        "identity_scope": "host",
        "identity_value": "verify-conflict",
        "connection_target": "verify-conflict",
        "name": "Verify Conflict",
        "os_version": "14",
        "host_id": db_host.id,
        "device_type": DeviceType.real_device,
        "connection_type": ConnectionType.usb,
    }
    monkeypatch.setattr("app.verification.services.job_state.publish", AsyncMock())
    monkeypatch.setattr(
        "app.verification.services.preparation.device_write.prepare_device_create_payload_async",
        AsyncMock(return_value=dict(base_payload)),
    )
    _prep_svc = VerificationPreparationService(
        settings=FakeSettingsReader(),
        circuit_breaker=Mock(),
        crud=DeviceCrudService(
            settings=FakeSettingsReader(), identity=DeviceIdentityConflictService(), publisher=event_bus
        ),
        identity=DeviceIdentityConflictService(),
    )
    monkeypatch.setattr(
        _prep_svc._identity,
        "ensure_device_payload_identity_available",
        AsyncMock(side_effect=preparation.DeviceIdentityConflictError("duplicate")),
    )
    _prep_svc.resolve_host_derived_payload = AsyncMock(return_value=None)  # type: ignore[method-assign]
    context, error = await _prep_svc.validate_create_request(
        _job(),
        db_session,
        DeviceVerificationCreate(**base_payload),
        http_client_factory=object,
    )
    assert context is None
    assert error == "duplicate"

    existing = SimpleNamespace(
        id=__import__("uuid").uuid4(),
        host_id=db_host.id,
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value="existing-verify",
        connection_target="existing-verify",
        name="Existing Verify",
        os_version="14",
        os_version_display=None,
        manufacturer=None,
        model=None,
        model_number=None,
        software_versions=None,
        tags={},
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
        ip_address=None,
        device_config={},
    )
    _mock_crud2 = AsyncMock()
    _mock_crud2.get_device = AsyncMock(return_value=existing)
    monkeypatch.setattr(
        "app.verification.services.preparation.device_write.prepare_device_update_payload_async",
        AsyncMock(side_effect=ValueError("bad update")),
    )
    _prep_svc2 = VerificationPreparationService(
        settings=FakeSettingsReader(),
        circuit_breaker=Mock(),
        crud=_mock_crud2,
        identity=DeviceIdentityConflictService(),
    )
    context, error = await _prep_svc2.validate_update_request(
        _job(),
        db_session,
        existing.id,
        DeviceVerificationUpdate(name="bad", host_id=existing.host_id),
        http_client_factory=object,
    )
    assert context is None
    assert error == "bad update"

    payload = dict(base_payload)
    payload["host_id"] = __import__("uuid").uuid4()
    monkeypatch.setattr(
        "app.verification.services.preparation.device_write.prepare_device_update_payload_async",
        AsyncMock(return_value=payload),
    )
    context, error = await _prep_svc2.validate_update_request(
        _job(),
        db_session,
        existing.id,
        DeviceVerificationUpdate(name="missing host", host_id=existing.host_id),
        http_client_factory=object,
    )
    assert context is None
    assert error == "Assigned host was not found"

    payload["host_id"] = db_host.id
    _mock_crud3 = AsyncMock()
    _mock_crud3.get_device = AsyncMock(return_value=existing)
    _prep_svc3 = VerificationPreparationService(
        settings=FakeSettingsReader(),
        circuit_breaker=Mock(),
        crud=_mock_crud3,
        identity=DeviceIdentityConflictService(),
    )
    _prep_svc3.resolve_host_derived_payload = AsyncMock(return_value="resolution failed")  # type: ignore[method-assign]
    context, error = await _prep_svc3.validate_update_request(
        _job(),
        db_session,
        existing.id,
        DeviceVerificationUpdate(name="resolution", host_id=existing.host_id),
        http_client_factory=object,
    )
    assert context is None
    assert error == "resolution failed"

    _mock_crud4 = AsyncMock()
    _mock_crud4.get_device = AsyncMock(return_value=existing)
    _prep_svc4 = VerificationPreparationService(
        settings=FakeSettingsReader(),
        circuit_breaker=Mock(),
        crud=_mock_crud4,
        identity=DeviceIdentityConflictService(),
    )
    monkeypatch.setattr(
        _prep_svc4._identity,
        "ensure_device_payload_identity_available",
        AsyncMock(side_effect=preparation.DeviceIdentityConflictError("update duplicate")),
    )
    _prep_svc4.resolve_host_derived_payload = AsyncMock(return_value=None)  # type: ignore[method-assign]
    context, error = await _prep_svc4.validate_update_request(
        _job(),
        db_session,
        existing.id,
        DeviceVerificationUpdate(name="duplicate", host_id=existing.host_id),
        http_client_factory=object,
    )
    assert context is None
    assert error == "update duplicate"
