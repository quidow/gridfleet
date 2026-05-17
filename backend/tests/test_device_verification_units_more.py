from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.appium_nodes.exceptions import NodeManagerError
from app.appium_nodes.models import AppiumNode
from app.core.errors import AgentCallError, AgentUnreachableError
from app.devices.models import ConnectionType, Device, DeviceOperationalState, DeviceType
from app.devices.schemas.device import DeviceVerificationCreate, DeviceVerificationUpdate
from app.devices.services import verification as device_verification
from app.devices.services import verification_execution as execution
from app.devices.services import verification_preparation as preparation
from app.devices.services.verification_job_state import new_job
from app.devices.services.verification_preparation import PreparedVerificationContext
from app.hosts.models import Host
from tests.helpers import create_device_record

pytestmark = pytest.mark.usefixtures("seeded_driver_packs")


def _job() -> dict[str, object]:
    return new_job("unit-job")


def _device(host: Host | None = None) -> Device:
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
    assert await device_verification.get_verification_job("not-a-uuid", session_factory=AsyncMock()) is None

    class Session:
        async def __aenter__(self) -> "Session":
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def get(self, *_args: object) -> object:
            return SimpleNamespace(kind="other", snapshot={"status": "queued"})

    assert (
        await device_verification.get_verification_job(str(__import__("uuid").uuid4()), session_factory=Session) is None
    )


async def test_run_device_health_covers_skip_agent_success_and_failure(
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.devices.services.verification_job_state.publish", AsyncMock())
    job = _job()
    assert await execution.run_device_health(job, _device(None), http_client_factory=object) is None
    assert job["current_stage"] == "device_health"
    assert job["stages"][1]["status"] == "skipped"

    device = _device(db_host)
    monkeypatch.setattr(
        "app.devices.services.verification_execution.fetch_pack_device_health",
        AsyncMock(side_effect=AgentCallError("10.0.0.1", "down")),
    )
    detail = await execution.run_device_health(_job(), device, http_client_factory=object)
    assert detail == "Agent health check failed: down"

    monkeypatch.setattr(
        "app.devices.services.verification_execution.fetch_pack_device_health",
        AsyncMock(return_value={"healthy": True, "avd_launched": {"serial": "emulator-5554"}}),
    )
    assert await execution.run_device_health(_job(), device, http_client_factory=object) is None
    assert device.connection_target == "emulator-5554"

    monkeypatch.setattr(
        "app.devices.services.verification_execution.fetch_pack_device_health",
        AsyncMock(return_value={"healthy": False, "checks": [{"check_id": "adb_ready", "ok": False, "message": "no"}]}),
    )
    assert await execution.run_device_health(_job(), device, http_client_factory=object) == "adb ready failed (no)"


async def test_stop_existing_node_and_run_probe_failure_paths(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.devices.services.verification_job_state.publish", AsyncMock())
    existing = await create_device_record(
        db_session,
        host_id=db_host.id,
        identity_value="verify-existing-node",
        connection_target="verify-existing-node",
        name="Verify Existing",
        operational_state=DeviceOperationalState.available,
    )
    node = AppiumNode(device_id=existing.id, port=4723, grid_url="http://grid", pid=1, active_connection_target="live")
    existing.appium_node = node
    context = PreparedVerificationContext(
        mode="update",
        transient_device=existing,
        save_payload={},
        existing_device=existing,
        save_device_id=existing.id,
    )

    monkeypatch.setattr(
        "app.devices.services.verification_execution._stop_managed_node_for_verification",
        AsyncMock(side_effect=NodeManagerError("stop failed")),
    )
    detail = await execution.stop_existing_managed_node_for_update(_job(), db_session, context)
    assert detail is not None
    assert "stop failed" in detail

    monkeypatch.setattr(
        "app.devices.services.verification_execution.start_node", AsyncMock(side_effect=NodeManagerError("no node"))
    )
    started, error = await execution.run_probe(_job(), db_session, existing, probe_session_fn=AsyncMock())
    assert started is None
    assert error == "no node"

    fake_node = AppiumNode(id=__import__("uuid").uuid4(), device_id=existing.id, port=4723, grid_url="http://grid")
    monkeypatch.setattr("app.devices.services.verification_execution.start_node", AsyncMock(return_value=fake_node))
    monkeypatch.setattr(
        "app.devices.services.verification_execution.wait_for_node_running", AsyncMock(return_value=None)
    )
    started, error = await execution.run_probe(_job(), db_session, existing, probe_session_fn=AsyncMock())
    assert started is fake_node
    assert error == "Verification node did not reach running state within timeout"

    running_node = AppiumNode(
        id=__import__("uuid").uuid4(),
        device_id=existing.id,
        port=4723,
        grid_url="http://grid",
        pid=1,
        active_connection_target="live",
    )
    monkeypatch.setattr(
        "app.devices.services.verification_execution.wait_for_node_running", AsyncMock(return_value=running_node)
    )
    monkeypatch.setattr(
        "app.devices.services.verification_execution.capability_service.get_device_capabilities",
        AsyncMock(return_value={"platformName": "Android"}),
    )
    probe_session = AsyncMock(return_value=(False, "probe failed"))
    started, error = await execution.run_probe(
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
        grid_url="http://grid",
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
    monkeypatch.setattr("app.devices.services.verification_job_state.publish", AsyncMock())
    existing = await create_device_record(
        db_session,
        host_id=db_host.id,
        identity_value="verify-converge-immediate",
        connection_target="verify-converge-immediate",
        name="Verify Converge",
        operational_state=DeviceOperationalState.available,
    )
    fake_node = AppiumNode(id=__import__("uuid").uuid4(), device_id=existing.id, port=4723, grid_url="http://grid")
    monkeypatch.setattr("app.devices.services.verification_execution.start_node", AsyncMock(return_value=fake_node))
    monkeypatch.setattr(
        "app.devices.services.verification_execution.wait_for_node_running", AsyncMock(return_value=None)
    )
    converge_mock = AsyncMock(return_value=None)
    monkeypatch.setattr(
        "app.devices.services.verification_execution.converge_device_now",
        converge_mock,
        raising=False,
    )

    await execution.run_probe(_job(), db_session, existing, probe_session_fn=AsyncMock())

    converge_mock.assert_awaited_once()
    call_args = converge_mock.await_args
    assert call_args is not None
    assert call_args.args[0] == existing.id or call_args.kwargs.get("device_id") == existing.id


async def test_save_verified_context_and_cleanup_error_paths(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.devices.services.verification_job_state.publish", AsyncMock())
    device = await create_device_record(
        db_session,
        host_id=db_host.id,
        identity_value="verify-save-001",
        connection_target="verify-save-001",
        name="Verify Save",
    )
    context = PreparedVerificationContext(
        mode="update",
        transient_device=device,
        save_payload={
            "pack_id": device.pack_id,
            "platform_id": device.platform_id,
            "identity_scheme": device.identity_scheme,
            "identity_scope": device.identity_scope,
            "identity_value": device.identity_value,
            "connection_target": device.connection_target,
            "name": device.name,
            "os_version": device.os_version,
            "host_id": device.host_id,
            "device_type": device.device_type,
            "connection_type": device.connection_type,
        },
        save_device_id=None,
    )
    saved, error = await execution.save_verified_context(_job(), db_session, context)
    assert saved is None
    assert error == "Verification context is missing the persisted device id"

    context.save_device_id = __import__("uuid").uuid4()
    monkeypatch.setattr(
        "app.devices.services.verification_execution.device_service.update_device", AsyncMock(return_value=None)
    )
    saved, error = await execution.save_verified_context(_job(), db_session, context)
    assert saved is None
    assert error == "Device was not found"

    node = AppiumNode(device_id=device.id, port=4723, grid_url="http://grid")
    monkeypatch.setattr(
        "app.devices.services.verification_execution.stop_node", AsyncMock(side_effect=RuntimeError("boom"))
    )
    cleanup_error = await execution._stop_verification_node_if_running(_job(), db_session, device, node)
    assert cleanup_error == "Failed to stop verification node: boom"
    assert node.pid is None


async def test_verification_execution_remaining_error_branches(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.devices.services.verification_job_state.publish", AsyncMock())
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

    node = AppiumNode(device_id=device.id, port=4723, grid_url="http://grid", pid=123, active_connection_target="live")
    fake_device = SimpleNamespace(id=device.id, appium_node=node)
    monkeypatch.setattr("app.devices.services.verification_execution.write_desired_state", AsyncMock())
    stopped = await execution._stop_managed_node_for_verification(fake_db, fake_device)
    assert stopped is node
    assert node.pid is None

    context = PreparedVerificationContext(
        mode="create",
        transient_device=device,
        save_payload={
            "pack_id": device.pack_id,
            "platform_id": device.platform_id,
            "identity_scheme": device.identity_scheme,
            "identity_scope": device.identity_scope,
            "identity_value": device.identity_value,
            "connection_target": device.connection_target,
            "name": device.name,
            "os_version": device.os_version,
            "host_id": device.host_id,
            "device_type": device.device_type,
            "connection_type": device.connection_type,
        },
        save_device_id=device.id,
    )
    monkeypatch.setattr(
        "app.devices.services.verification_execution.device_service.create_device",
        AsyncMock(side_effect=execution.DeviceIdentityConflictError("duplicate identity")),
    )
    saved, error = await execution.save_verified_context(_job(), db_session, context)
    assert saved is None
    assert error == "duplicate identity"

    monkeypatch.setattr(
        "app.devices.services.verification_execution.device_service.create_device",
        AsyncMock(side_effect=ValueError("bad payload")),
    )
    saved, error = await execution.save_verified_context(_job(), db_session, context)
    assert saved is None
    assert error == "bad payload"

    monkeypatch.setattr(
        "app.devices.services.verification_execution.stop_node",
        AsyncMock(side_effect=NodeManagerError("already stopped")),
    )
    assert await execution._stop_verification_node_if_running(_job(), db_session, device, node) is None

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


async def test_save_finalize_and_execute_success_guard_branches(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.devices.services.verification_job_state.publish", AsyncMock())
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
    monkeypatch.setattr(
        "app.devices.services.verification_execution.device_service.create_device",
        AsyncMock(return_value=created),
    )

    saved, error = await execution.save_verified_context(_job(), db, context)

    assert saved is created
    assert error is None

    updated = SimpleNamespace(id=device_id, verified_at=None)
    context.mode = "update"
    monkeypatch.setattr(
        "app.devices.services.verification_execution.device_service.update_device",
        AsyncMock(return_value=updated),
    )
    saved, error = await execution.save_verified_context(_job(), db, context)
    assert saved is updated
    assert error is None
    db.refresh.assert_awaited_with(updated)

    target = SimpleNamespace(name="unchanged")
    execution._restore_update_original_fields(target, None)
    assert target.name == "unchanged"

    monkeypatch.setattr(
        "app.devices.services.verification_execution.device_service.update_device",
        AsyncMock(return_value=None),
    )
    failed = await execution._finalize_success(db, context, job=_job(), node=None)
    assert failed.status == "failed"
    assert failed.error == "Device was not found"

    monkeypatch.setattr(
        "app.devices.services.verification_execution.stop_existing_managed_node_for_update",
        AsyncMock(return_value="stop failed"),
    )
    outcome = await execution.execute_verification_context(
        _job(),
        db,
        context,
        http_client_factory=object,
        probe_session_fn=AsyncMock(),
    )
    assert outcome.status == "failed"
    assert outcome.error == "stop failed"


async def test_finalize_success_and_execute_update_branches(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.devices.services.verification_job_state.publish", AsyncMock())
    db = AsyncMock()
    device_id = __import__("uuid").uuid4()
    locked = SimpleNamespace(
        id=device_id,
        operational_state=DeviceOperationalState.offline,
        verified_at=None,
    )
    context = PreparedVerificationContext(
        mode="create",
        transient_device=locked,
        save_payload={"name": "created"},
        save_device_id=device_id,
        keep_running_after_verify=False,
    )
    monkeypatch.setattr(
        "app.devices.services.verification_execution.device_locking.lock_device", AsyncMock(return_value=locked)
    )
    monkeypatch.setattr(
        "app.devices.services.verification_execution._restore_create_payload_fields", lambda *args: None
    )
    monkeypatch.setattr(
        "app.devices.services.verification_execution._stop_verification_node_if_running",
        AsyncMock(return_value="cleanup failed"),
    )
    monkeypatch.setattr("app.devices.services.verification_execution.device_service.delete_device", AsyncMock())

    outcome = await execution._finalize_success(db, context, job=_job(), node=None)
    assert outcome.status == "failed"
    assert outcome.device_id is None

    context.mode = "update"
    context.save_payload = {"name": "updated", "host_id": __import__("uuid").uuid4()}
    context.keep_running_after_verify = False
    monkeypatch.setattr(
        "app.devices.services.verification_execution.device_service.update_device", AsyncMock(return_value=locked)
    )
    monkeypatch.setattr(
        "app.devices.services.verification_execution.DeviceStateMachine",
        lambda: SimpleNamespace(transition=AsyncMock()),
    )
    outcome = await execution._finalize_success(db, context, job=_job(), node=None)
    assert outcome.status == "failed"
    assert outcome.device_id == str(device_id)

    context.keep_running_after_verify = True
    node = SimpleNamespace(port=4723, pid=22)
    monkeypatch.setattr(
        "app.devices.services.verification_execution.ready_operational_state",
        AsyncMock(return_value=DeviceOperationalState.available),
    )
    monkeypatch.setattr("app.devices.services.verification_execution.set_operational_state", AsyncMock())
    monkeypatch.setattr(
        "app.devices.services.verification_execution.session_viability.record_session_viability_result",
        AsyncMock(),
    )
    outcome = await execution._finalize_success(db, context, job=_job(), node=node)
    assert outcome.status == "completed"
    execution.set_operational_state.assert_awaited_once()

    update_device = SimpleNamespace(id=device_id, identity_value="update-device", name="old")
    update_context = PreparedVerificationContext(
        mode="update",
        transient_device=update_device,
        save_payload={"name": "new", "replace_device_config": True},
        save_device_id=device_id,
    )
    monkeypatch.setattr(
        "app.devices.services.verification_execution.stop_existing_managed_node_for_update",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "app.devices.services.verification_execution.device_locking.lock_device", AsyncMock(return_value=update_device)
    )
    monkeypatch.setattr(
        "app.devices.services.verification_execution.run_device_health", AsyncMock(side_effect=RuntimeError("boom"))
    )
    monkeypatch.setattr("app.devices.services.verification_execution._finalize_failure", AsyncMock())

    with pytest.raises(RuntimeError, match="boom"):
        await execution.execute_verification_context(
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
    monkeypatch.setattr("app.devices.services.verification_job_state.publish", AsyncMock())
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

    assert await preparation.resolve_host_derived_payload({}, None, http_client_factory=object, db=db_session) == (
        "Assigned host is required"
    )

    monkeypatch.setattr(
        "app.devices.services.verification_preparation.resolve_pack_platform",
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
        "app.devices.services.verification_preparation.normalize_pack_device",
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
    assert await preparation.resolve_host_derived_payload(
        payload, db_host, http_client_factory=object, db=db_session
    ) == ("serial: bad")

    monkeypatch.setattr(
        "app.devices.services.verification_preparation.normalize_pack_device",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "app.devices.services.verification_preparation.pack_device_lifecycle_action",
        AsyncMock(side_effect=AgentCallError("10.0.0.1", "404 resolve")),
    )
    assert await preparation.resolve_host_derived_payload(
        payload, db_host, http_client_factory=object, db=db_session
    ) == ("Device must resolve to a stable identity before save (action: resolve)")

    monkeypatch.setattr(
        "app.devices.services.verification_preparation.pack_device_lifecycle_action",
        AsyncMock(return_value={"identity_value": "stable", "connection_target": "10.0.0.1:5555", "name": "Resolved"}),
    )
    assert (
        await preparation.resolve_host_derived_payload(payload, db_host, http_client_factory=object, db=db_session)
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
    context, error = await preparation.validate_create_request(
        _job(), db_session, bad_create, http_client_factory=object
    )
    assert context is None
    assert error == "Assigned host was not found"

    context, error = await preparation.validate_update_request(
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
    monkeypatch.setattr("app.devices.services.verification_job_state.publish", AsyncMock())
    assert await preparation._load_host(db_session, None) is None
    assert preparation._is_transport_identity("10.0.0.5", "other", "10.0.0.5") is True

    monkeypatch.setattr(
        "app.devices.services.verification_preparation.resolve_pack_platform",
        AsyncMock(
            return_value=SimpleNamespace(
                pack_id="pack",
                release="1",
                platform_id="platform",
                connection_behavior={},
            )
        ),
    )
    for exc in (AgentCallError("10.0.0.1", "down"), LookupError("missing"), TypeError("bad mock")):
        monkeypatch.setattr(
            "app.devices.services.verification_preparation.normalize_pack_device",
            AsyncMock(side_effect=exc),
        )
        assert (
            await preparation.resolve_host_derived_payload(
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
        "app.devices.services.verification_preparation.normalize_pack_device",
        AsyncMock(return_value={"field_errors": ["bad"]}),
    )
    assert (
        await preparation.resolve_host_derived_payload(
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
        "app.devices.services.verification_preparation.device_write.prepare_device_create_payload_async",
        AsyncMock(return_value=dict(payload)),
    )
    monkeypatch.setattr(
        "app.devices.services.verification_preparation.resolve_host_derived_payload",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "app.devices.services.verification_preparation.ensure_device_payload_identity_available",
        AsyncMock(side_effect=preparation.DeviceIdentityConflictError("late duplicate")),
    )
    context, error = await preparation.validate_create_request(
        _job(),
        db_session,
        DeviceVerificationCreate(**payload),
        http_client_factory=object,
    )
    assert context is None
    assert error == "late duplicate"

    monkeypatch.setattr(
        "app.devices.services.verification_preparation.device_write.prepare_device_create_payload_async",
        AsyncMock(side_effect=ValueError("bad create")),
    )
    context, error = await preparation.validate_create_request(
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
        "app.devices.services.verification_preparation.resolve_pack_platform",
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
        "app.devices.services.verification_preparation.normalize_pack_device",
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
        "app.devices.services.verification_preparation.pack_device_lifecycle_action",
        AsyncMock(return_value={"identity_value": "resolved-stable"}),
    )

    assert (
        await preparation.resolve_host_derived_payload(
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
        "app.devices.services.verification_preparation.normalize_pack_device",
        AsyncMock(return_value=None),
    )
    assert (
        await preparation.resolve_host_derived_payload(
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
        "app.devices.services.verification_preparation.pack_device_lifecycle_action",
        AsyncMock(side_effect=AgentUnreachableError("10.0.0.20", "agent down")),
    )
    assert (
        await preparation.resolve_host_derived_payload(
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
        "app.devices.services.verification_preparation.resolve_pack_platform",
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
    monkeypatch.setattr("app.devices.services.verification_job_state.publish", AsyncMock())
    monkeypatch.setattr(
        "app.devices.services.verification_preparation.device_write.prepare_device_create_payload_async",
        AsyncMock(return_value=dict(base_payload)),
    )
    monkeypatch.setattr(
        "app.devices.services.verification_preparation.resolve_host_derived_payload",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "app.devices.services.verification_preparation.ensure_device_payload_identity_available",
        AsyncMock(side_effect=preparation.DeviceIdentityConflictError("duplicate")),
    )
    context, error = await preparation.validate_create_request(
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
        auto_manage=True,
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
        ip_address=None,
        device_config={},
    )
    monkeypatch.setattr(
        "app.devices.services.verification_preparation.device_service.get_device", AsyncMock(return_value=existing)
    )
    monkeypatch.setattr(
        "app.devices.services.verification_preparation.device_write.prepare_device_update_payload_async",
        AsyncMock(side_effect=ValueError("bad update")),
    )
    context, error = await preparation.validate_update_request(
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
        "app.devices.services.verification_preparation.device_write.prepare_device_update_payload_async",
        AsyncMock(return_value=payload),
    )
    context, error = await preparation.validate_update_request(
        _job(),
        db_session,
        existing.id,
        DeviceVerificationUpdate(name="missing host", host_id=existing.host_id),
        http_client_factory=object,
    )
    assert context is None
    assert error == "Assigned host was not found"

    payload["host_id"] = db_host.id
    monkeypatch.setattr(
        "app.devices.services.verification_preparation.resolve_host_derived_payload",
        AsyncMock(return_value="resolution failed"),
    )
    context, error = await preparation.validate_update_request(
        _job(),
        db_session,
        existing.id,
        DeviceVerificationUpdate(name="resolution", host_id=existing.host_id),
        http_client_factory=object,
    )
    assert context is None
    assert error == "resolution failed"

    monkeypatch.setattr(
        "app.devices.services.verification_preparation.resolve_host_derived_payload",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "app.devices.services.verification_preparation.ensure_device_payload_identity_available",
        AsyncMock(side_effect=preparation.DeviceIdentityConflictError("update duplicate")),
    )
    context, error = await preparation.validate_update_request(
        _job(),
        db_session,
        existing.id,
        DeviceVerificationUpdate(name="duplicate", host_id=existing.host_id),
        http_client_factory=object,
    )
    assert context is None
    assert error == "update duplicate"
