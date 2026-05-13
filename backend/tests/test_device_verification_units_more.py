from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import AgentCallError
from app.models.appium_node import AppiumNode
from app.models.device import ConnectionType, Device, DeviceOperationalState, DeviceType
from app.models.host import Host
from app.schemas.device import DeviceVerificationCreate, DeviceVerificationUpdate
from app.services import device_verification_execution as execution
from app.services import device_verification_preparation as preparation
from app.services.device_verification_job_state import new_job
from app.services.device_verification_preparation import PreparedVerificationContext
from app.services.node_service_types import NodeManagerError
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


async def test_run_device_health_covers_skip_agent_success_and_failure(
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.services.device_verification_job_state.publish", AsyncMock())
    job = _job()
    assert await execution.run_device_health(job, _device(None), http_client_factory=object) is None
    assert job["current_stage"] == "device_health"
    assert job["stages"][1]["status"] == "skipped"

    device = _device(db_host)
    monkeypatch.setattr(
        "app.services.device_verification_execution.fetch_pack_device_health",
        AsyncMock(side_effect=AgentCallError("10.0.0.1", "down")),
    )
    detail = await execution.run_device_health(_job(), device, http_client_factory=object)
    assert detail == "Agent health check failed: down"

    monkeypatch.setattr(
        "app.services.device_verification_execution.fetch_pack_device_health",
        AsyncMock(return_value={"healthy": True, "avd_launched": {"serial": "emulator-5554"}}),
    )
    assert await execution.run_device_health(_job(), device, http_client_factory=object) is None
    assert device.connection_target == "emulator-5554"

    monkeypatch.setattr(
        "app.services.device_verification_execution.fetch_pack_device_health",
        AsyncMock(return_value={"healthy": False, "checks": [{"check_id": "adb_ready", "ok": False, "message": "no"}]}),
    )
    assert await execution.run_device_health(_job(), device, http_client_factory=object) == "adb ready failed (no)"


async def test_stop_existing_node_and_run_probe_failure_paths(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.services.device_verification_job_state.publish", AsyncMock())
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
        "app.services.device_verification_execution._stop_managed_node_for_verification",
        AsyncMock(side_effect=NodeManagerError("stop failed")),
    )
    detail = await execution.stop_existing_managed_node_for_update(_job(), db_session, context)
    assert detail is not None
    assert "stop failed" in detail

    monkeypatch.setattr(
        "app.services.device_verification_execution.start_node", AsyncMock(side_effect=NodeManagerError("no node"))
    )
    started, error = await execution.run_probe(_job(), db_session, existing, probe_session_fn=AsyncMock())
    assert started is None
    assert error == "no node"

    fake_node = AppiumNode(id=__import__("uuid").uuid4(), device_id=existing.id, port=4723, grid_url="http://grid")
    monkeypatch.setattr("app.services.device_verification_execution.start_node", AsyncMock(return_value=fake_node))
    monkeypatch.setattr(
        "app.services.device_verification_execution.wait_for_node_running", AsyncMock(return_value=None)
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
        "app.services.device_verification_execution.wait_for_node_running", AsyncMock(return_value=running_node)
    )
    monkeypatch.setattr(
        "app.services.device_verification_execution.capability_service.get_device_capabilities",
        AsyncMock(return_value={"platformName": "Android"}),
    )
    started, error = await execution.run_probe(
        _job(),
        db_session,
        existing,
        probe_session_fn=AsyncMock(return_value=(False, "probe failed")),
    )
    assert started is running_node
    assert error == "probe failed"


async def test_save_verified_context_and_cleanup_error_paths(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.services.device_verification_job_state.publish", AsyncMock())
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
        "app.services.device_verification_execution.device_service.update_device", AsyncMock(return_value=None)
    )
    saved, error = await execution.save_verified_context(_job(), db_session, context)
    assert saved is None
    assert error == "Device was not found"

    node = AppiumNode(device_id=device.id, port=4723, grid_url="http://grid")
    monkeypatch.setattr(
        "app.services.device_verification_execution.stop_node", AsyncMock(side_effect=RuntimeError("boom"))
    )
    cleanup_error = await execution._stop_verification_node_if_running(_job(), db_session, device, node)
    assert cleanup_error == "Failed to stop verification node: boom"
    assert node.pid is None


async def test_preparation_resolution_and_validation_error_paths(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.services.device_verification_job_state.publish", AsyncMock())
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
        "app.services.device_verification_preparation.resolve_pack_platform",
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
        "app.services.device_verification_preparation.normalize_pack_device",
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
        "app.services.device_verification_preparation.normalize_pack_device",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "app.services.device_verification_preparation.pack_device_lifecycle_action",
        AsyncMock(side_effect=AgentCallError("10.0.0.1", "404 resolve")),
    )
    assert await preparation.resolve_host_derived_payload(
        payload, db_host, http_client_factory=object, db=db_session
    ) == ("Device must resolve to a stable identity before save (action: resolve)")

    monkeypatch.setattr(
        "app.services.device_verification_preparation.pack_device_lifecycle_action",
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
