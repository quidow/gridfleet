import asyncio
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.appium_node import AppiumNode, NodeState
from app.models.device import ConnectionType, Device, DeviceOperationalState, DeviceType
from app.models.driver_pack import DriverPack
from app.models.host import Host
from app.models.job import Job
from app.services.device_verification import clear_verification_jobs
from app.services.device_verification_execution import _health_failure_detail
from app.services.job_queue import reset_stale_running_jobs, run_pending_jobs_once
from app.services.node_service_types import NodeManagerError, TemporaryNodeHandle
from app.services.session_viability import get_session_viability
from tests.helpers import create_device_record
from tests.pack.factories import seed_test_packs

DEVICE_PAYLOAD = {
    "identity_value": "verify-001",
    "name": "Verify Pixel",
    "pack_id": "appium-uiautomator2",
    "platform_id": "android_mobile",
    "identity_scheme": "android_serial",
    "identity_scope": "host",
    "os_version": "14",
}

HOST_PAYLOAD = {
    "hostname": "verify-host",
    "ip": "10.0.0.20",
    "os_type": "linux",
    "agent_port": 5100,
}


@pytest_asyncio.fixture(autouse=True)
async def reset_verification_jobs(db_session: AsyncSession) -> AsyncGenerator[None]:
    session_factory = async_sessionmaker(db_session.bind, class_=AsyncSession, expire_on_commit=False)
    await seed_test_packs(db_session)
    await clear_verification_jobs(session_factory=session_factory)
    yield
    await clear_verification_jobs(session_factory=session_factory)


@pytest_asyncio.fixture
async def default_host_id(client: AsyncClient) -> str:
    resp = await client.post("/api/hosts", json=HOST_PAYLOAD)
    assert resp.status_code == 201
    return str(resp.json()["id"])


def device_payload(host_id: str, **overrides: object) -> dict[str, Any]:
    payload = {**DEVICE_PAYLOAD, "host_id": host_id, **overrides}
    if payload.get("platform_id") == "android_mobile" and payload.get("device_type") == "emulator":
        payload["platform_id"] = "android_mobile"
    return payload


async def test_create_verification_requires_pack_id(client: AsyncClient, default_host_id: str) -> None:
    payload = device_payload(default_host_id)
    payload.pop("pack_id")

    resp = await client.post("/api/devices/verification-jobs", json=payload)

    assert resp.status_code == 422


async def test_create_verification_requires_platform_id(client: AsyncClient, default_host_id: str) -> None:
    payload = device_payload(default_host_id)
    payload.pop("platform_id")

    resp = await client.post("/api/devices/verification-jobs", json=payload)

    assert resp.status_code == 422


async def _wait_for_job(
    client: AsyncClient,
    job_id: str,
    *,
    session_factory: async_sessionmaker[AsyncSession],
) -> dict[str, Any]:
    for _ in range(100):
        resp = await client.get(f"/api/devices/verification-jobs/{job_id}")
        assert resp.status_code == 200
        job = resp.json()
        if job["status"] in {"completed", "failed"}:
            return dict(job)
        await run_pending_jobs_once(session_factory)
        await asyncio.sleep(0.01)
    raise AssertionError(f"Job {job_id} did not finish in time")


def _assert_job_stage(
    job: dict[str, Any],
    *,
    stage: str,
    status: str,
    detail_contains: str | None = None,
) -> None:
    assert job["current_stage"] == stage
    assert job["current_stage_status"] == status
    if detail_contains is not None:
        assert detail_contains in (job["detail"] or "")


def _mock_http_client(*, payload: dict[str, Any]) -> MagicMock:
    mock_client = MagicMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = False

    response = MagicMock()
    response.raise_for_status.return_value = None
    response.json.return_value = payload
    mock_client.get = AsyncMock(return_value=response)
    return mock_client


def _mock_resolution_http_client(
    *,
    resolution_payload: dict[str, Any] | None = None,
    resolution_status: int = 200,
    health_payload: dict[str, Any] | None = None,
) -> MagicMock:
    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    health_response = MagicMock()
    health_response.raise_for_status.return_value = None
    health_response.json.return_value = health_payload or {"healthy": True, "adb_connected": {"connected": True}}

    resolution_response = MagicMock()
    resolution_response.status_code = resolution_status
    resolution_response.json.return_value = resolution_payload or {}
    if resolution_status >= 400:
        request = MagicMock()
        resolution_response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "resolution failed",
            request=request,
            response=resolution_response,
        )
    else:
        resolution_response.raise_for_status.return_value = None

    mock_client.get = AsyncMock(return_value=health_response)
    mock_client.post = AsyncMock(return_value=resolution_response)
    return mock_client


def _mock_node_manager_http_client(
    *,
    start_payload: dict[str, Any] | None = None,
    status_payload: dict[str, Any] | None = None,
    stop_payload: dict[str, Any] | None = None,
) -> MagicMock:
    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    start_response = MagicMock()
    start_response.raise_for_status.return_value = None
    start_response.json.return_value = start_payload or {"pid": 12345, "port": 4723}

    stop_response = MagicMock()
    stop_response.raise_for_status.return_value = None
    stop_response.json.return_value = stop_payload or {"stopped": True, "port": 4723}

    status_response = MagicMock()
    status_response.status_code = 200
    status_response.json.return_value = status_payload or {"running": True, "port": 4723}

    mock_client.post = AsyncMock(side_effect=[start_response, stop_response, stop_response])
    mock_client.get = AsyncMock(return_value=status_response)
    return mock_client


async def test_verification_job_success_keeps_verified_node_when_auto_manage_enabled(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    session_factory = async_sessionmaker(db_session.bind, class_=AsyncSession, expire_on_commit=False)
    start_mock = AsyncMock(
        return_value=TemporaryNodeHandle(port=4723, pid=12345, active_connection_target="emulator-5554")
    )
    stop_mock = AsyncMock()
    healthy_http_client = _mock_http_client(payload={"healthy": True, "adb_connected": {"connected": True}})

    with (
        patch("app.services.device_verification_execution.start_temporary_node", start_mock),
        patch("app.services.device_verification_execution.stop_temporary_node", stop_mock),
        patch("app.services.device_verification_runner.httpx.AsyncClient", return_value=healthy_http_client),
        patch(
            "app.services.device_verification_runner.session_viability.probe_session_via_grid",
            new=AsyncMock(return_value=(True, None)),
        ),
    ):
        resp = await client.post("/api/devices/verification-jobs", json=device_payload(default_host_id))
        assert resp.status_code == 202
        assert resp.json()["current_stage"] is None
        assert resp.json()["current_stage_status"] is None
        assert resp.json()["detail"] is None
        job = await _wait_for_job(client, resp.json()["job_id"], session_factory=session_factory)

    assert job["status"] == "completed"
    _assert_job_stage(job, stage="save_device", status="passed", detail_contains="Device saved after verification")
    stop_mock.assert_not_awaited()

    devices = (await client.get("/api/devices")).json()
    assert len(devices) == 1
    assert devices[0]["identity_value"] == DEVICE_PAYLOAD["identity_value"]
    assert devices[0]["operational_state"] == "offline"

    detail = (await client.get(f"/api/devices/{devices[0]['id']}")).json()
    assert detail["appium_node"] is not None
    assert detail["appium_node"]["state"] == "stopped"
    assert detail["appium_node"]["desired_state"] == "running"
    assert detail["appium_node"]["active_connection_target"] is None

    async with session_factory() as verify_db:
        persisted_device = await verify_db.get(Device, uuid.UUID(devices[0]["id"]))
        assert persisted_device is not None
        viability = await get_session_viability(verify_db, persisted_device)
    assert viability is not None
    assert viability["status"] == "passed"
    assert viability["checked_by"] == "verification"


async def test_create_verification_refreshes_retained_temporary_node_with_saved_device_id(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    session_factory = async_sessionmaker(db_session.bind, class_=AsyncSession, expire_on_commit=False)
    start_mock = AsyncMock(
        return_value=TemporaryNodeHandle(
            port=4723,
            pid=12345,
            active_connection_target=DEVICE_PAYLOAD["identity_value"],
            owner_key=f"temp:{default_host_id}:{DEVICE_PAYLOAD['identity_value']}",
        )
    )
    healthy_http_client = _mock_http_client(payload={"healthy": True, "adb_connected": {"connected": True}})

    with (
        patch("app.services.device_verification_execution.start_temporary_node", start_mock),
        patch("app.services.device_verification_runner.httpx.AsyncClient", return_value=healthy_http_client),
        patch(
            "app.services.device_verification_runner.session_viability.probe_session_via_grid",
            new=AsyncMock(return_value=(True, None)),
        ),
    ):
        resp = await client.post("/api/devices/verification-jobs", json=device_payload(default_host_id))
        assert resp.status_code == 202
        job_id = resp.json()["job_id"]
        job = await _wait_for_job(client, job_id, session_factory=session_factory)

    assert job["status"] == "completed"

    async with session_factory() as verify_db:
        job_row = await verify_db.get(Job, uuid.UUID(job_id))
        assert job_row is not None
        cleanup_stage = next(s for s in job_row.snapshot["stages"] if s["name"] == "cleanup")
        node = (await verify_db.execute(select(AppiumNode))).scalar_one()
    assert cleanup_stage["status"] == "passed"
    assert cleanup_stage["data"] == {"port": 4723, "pid": 12345}
    assert node.port == 4723
    assert node.pid is None
    assert node.state == NodeState.stopped
    assert node.desired_state == NodeState.running
    assert node.transition_token is not None


async def test_retain_verified_node_acquires_row_lock(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    session_factory = async_sessionmaker(db_session.bind, class_=AsyncSession, expire_on_commit=False)
    start_mock = AsyncMock(
        return_value=TemporaryNodeHandle(port=4723, pid=12345, active_connection_target="emulator-5554")
    )
    healthy_http_client = _mock_http_client(payload={"healthy": True, "adb_connected": {"connected": True}})

    from app.services import device_verification_execution

    real_lock = device_verification_execution.device_locking.lock_device
    spy = AsyncMock(side_effect=real_lock)

    with (
        patch("app.services.device_verification_execution.start_temporary_node", start_mock),
        patch("app.services.device_verification_execution.stop_temporary_node", AsyncMock()),
        patch("app.services.device_verification_runner.httpx.AsyncClient", return_value=healthy_http_client),
        patch(
            "app.services.device_verification_runner.session_viability.probe_session_via_grid",
            new=AsyncMock(return_value=(True, None)),
        ),
        patch("app.services.device_verification_execution.device_locking.lock_device", spy),
    ):
        resp = await client.post("/api/devices/verification-jobs", json=device_payload(default_host_id))
        assert resp.status_code == 202
        job = await _wait_for_job(client, resp.json()["job_id"], session_factory=session_factory)

    assert job["status"] == "completed"
    spy.assert_awaited()


async def test_create_verification_marks_cleanup_failed_when_restart_intent_raises(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    session_factory = async_sessionmaker(db_session.bind, class_=AsyncSession, expire_on_commit=False)
    start_mock = AsyncMock(
        return_value=TemporaryNodeHandle(
            port=4723,
            pid=12345,
            active_connection_target=DEVICE_PAYLOAD["identity_value"],
            owner_key=f"temp:{default_host_id}:{DEVICE_PAYLOAD['identity_value']}",
        )
    )
    healthy_http_client = _mock_http_client(payload={"healthy": True, "adb_connected": {"connected": True}})
    desired_state_mock = AsyncMock(side_effect=[None, NodeManagerError("grid registration refresh exploded")])

    with (
        patch("app.services.device_verification_execution.start_temporary_node", start_mock),
        patch("app.services.device_verification_execution.write_desired_state", desired_state_mock),
        patch("app.services.device_verification_runner.httpx.AsyncClient", return_value=healthy_http_client),
        patch(
            "app.services.device_verification_runner.session_viability.probe_session_via_grid",
            new=AsyncMock(return_value=(True, None)),
        ),
    ):
        resp = await client.post("/api/devices/verification-jobs", json=device_payload(default_host_id))
        assert resp.status_code == 202
        job = await _wait_for_job(client, resp.json()["job_id"], session_factory=session_factory)

    assert job["status"] == "failed"
    _assert_job_stage(job, stage="cleanup", status="failed", detail_contains="grid registration refresh exploded")
    assert desired_state_mock.await_count == 2

    devices = (await client.get("/api/devices")).json()
    assert len(devices) == 1
    assert devices[0]["operational_state"] != "available"


async def test_avd_verification_uses_live_serial_but_saves_stable_avd_identity(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    session_factory = async_sessionmaker(db_session.bind, class_=AsyncSession, expire_on_commit=False)
    start_mock = AsyncMock(
        return_value=TemporaryNodeHandle(port=4723, pid=12345, active_connection_target="emulator-5554")
    )
    probe_mock = AsyncMock(return_value=(True, None))
    health_http_client = _mock_http_client(
        payload={
            "healthy": True,
            "avd_launched": {"avd_name": "Pixel_8_API_35", "serial": "emulator-5554"},
        }
    )

    with (
        patch("app.services.device_verification_execution.start_temporary_node", start_mock),
        patch("app.services.device_verification_execution.stop_temporary_node", new_callable=AsyncMock),
        patch(
            "app.services.device_verification_runner.httpx.AsyncClient", return_value=health_http_client
        ) as client_factory,
        patch(
            "app.services.device_verification_runner.session_viability.probe_session_via_grid",
            new=probe_mock,
        ),
    ):
        resp = await client.post(
            "/api/devices/verification-jobs",
            json=device_payload(
                default_host_id,
                identity_value="avd:Pixel_8_API_35",
                connection_target="Pixel_8_API_35",
                name="Pixel 8",
                device_type="emulator",
                connection_type=None,
            ),
        )
        assert resp.status_code == 202
        job = await _wait_for_job(client, resp.json()["job_id"], session_factory=session_factory)

    assert job["status"] == "completed"
    client_factory.assert_called_with(timeout=190)
    assert start_mock.await_args is not None
    transient_device = start_mock.await_args.args[1]
    assert transient_device.connection_target == "emulator-5554"
    assert transient_device.identity_value == "avd:Pixel_8_API_35"
    probe_caps = probe_mock.await_args.args[0]
    assert probe_caps["appium:udid"] == "emulator-5554"

    devices = (await client.get("/api/devices")).json()
    assert len(devices) == 1
    assert devices[0]["identity_scheme"] == "android_serial"
    assert devices[0]["identity_value"] == "avd:Pixel_8_API_35"
    assert devices[0]["connection_target"] == "Pixel_8_API_35"
    assert devices[0]["connection_type"] == "virtual"
    detail = (await client.get(f"/api/devices/{devices[0]['id']}")).json()
    assert detail["appium_node"]["desired_state"] == "running"
    assert detail["appium_node"]["active_connection_target"] is None


async def test_avd_verification_probe_uses_node_resolved_serial_when_already_running(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    session_factory = async_sessionmaker(db_session.bind, class_=AsyncSession, expire_on_commit=False)
    start_mock = AsyncMock(
        return_value=TemporaryNodeHandle(port=4723, pid=12345, active_connection_target="emulator-5554")
    )
    probe_mock = AsyncMock(return_value=(True, None))
    health_http_client = _mock_http_client(payload={"healthy": True})

    with (
        patch("app.services.device_verification_execution.start_temporary_node", start_mock),
        patch("app.services.device_verification_execution.stop_temporary_node", new_callable=AsyncMock),
        patch("app.services.device_verification_runner.httpx.AsyncClient", return_value=health_http_client),
        patch(
            "app.services.device_verification_runner.session_viability.probe_session_via_grid",
            new=probe_mock,
        ),
    ):
        resp = await client.post(
            "/api/devices/verification-jobs",
            json=device_payload(
                default_host_id,
                identity_value="avd:Pixel_8_API_35",
                connection_target="Pixel_8_API_35",
                name="Pixel 8",
                device_type="emulator",
                connection_type="virtual",
            ),
        )
        assert resp.status_code == 202
        job = await _wait_for_job(client, resp.json()["job_id"], session_factory=session_factory)

    assert job["status"] == "completed"
    probe_caps = probe_mock.await_args.args[0]
    assert probe_caps["appium:udid"] == "emulator-5554"

    devices = (await client.get("/api/devices")).json()
    assert len(devices) == 1
    assert devices[0]["identity_value"] == "avd:Pixel_8_API_35"
    assert devices[0]["connection_target"] == "Pixel_8_API_35"


async def test_avd_verification_preserves_explicit_virtual_lane_after_normalize(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    session_factory = async_sessionmaker(db_session.bind, class_=AsyncSession, expire_on_commit=False)
    start_mock = AsyncMock(
        return_value=TemporaryNodeHandle(port=4723, pid=12345, active_connection_target="emulator-5554")
    )
    probe_mock = AsyncMock(return_value=(True, None))
    http_client = _mock_resolution_http_client(
        resolution_payload={
            "identity_scheme": "android_serial",
            "identity_scope": "host",
            "identity_value": "avd:Pixel_8_API_35",
            "connection_target": "Pixel_8_API_35",
            "device_type": "real_device",
            "connection_type": "usb",
            "os_version": "unknown",
        },
        health_payload={"healthy": True},
    )

    with (
        patch("app.services.device_verification_execution.start_temporary_node", start_mock),
        patch("app.services.device_verification_execution.stop_temporary_node", new_callable=AsyncMock),
        patch("app.services.device_verification_runner.httpx.AsyncClient", return_value=http_client),
        patch(
            "app.services.device_verification_runner.session_viability.probe_session_via_grid",
            new=probe_mock,
        ),
    ):
        resp = await client.post(
            "/api/devices/verification-jobs",
            json=device_payload(
                default_host_id,
                identity_value="avd:Pixel_8_API_35",
                connection_target="Pixel_8_API_35",
                name="Pixel 8",
                device_type="emulator",
                connection_type="virtual",
            ),
        )
        assert resp.status_code == 202
        job = await _wait_for_job(client, resp.json()["job_id"], session_factory=session_factory)

    assert job["status"] == "completed"
    devices = (await client.get("/api/devices")).json()
    assert devices[0]["device_type"] == "emulator"
    assert devices[0]["connection_type"] == "virtual"


async def test_avd_verification_allows_same_avd_name_on_different_hosts(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    other_host_resp = await client.post(
        "/api/hosts",
        json={**HOST_PAYLOAD, "hostname": "verify-host-2", "ip": "10.0.0.21"},
    )
    assert other_host_resp.status_code == 201
    other_host_id = str(other_host_resp.json()["id"])

    session_factory = async_sessionmaker(db_session.bind, class_=AsyncSession, expire_on_commit=False)
    start_mock = AsyncMock(
        side_effect=[
            TemporaryNodeHandle(port=4723, pid=12345, active_connection_target="emulator-5554"),
            TemporaryNodeHandle(port=4724, pid=12346, active_connection_target="emulator-5554"),
        ]
    )
    healthy_http_client = _mock_http_client(
        payload={
            "healthy": True,
            "avd_launched": {"avd_name": "Pixel_8_API_35", "serial": "emulator-5554"},
        }
    )

    with (
        patch("app.services.device_verification_execution.start_temporary_node", start_mock),
        patch("app.services.device_verification_execution.stop_temporary_node", new_callable=AsyncMock),
        patch("app.services.device_verification_runner.httpx.AsyncClient", return_value=healthy_http_client),
        patch(
            "app.services.device_verification_runner.session_viability.probe_session_via_grid",
            new=AsyncMock(return_value=(True, None)),
        ),
    ):
        first_resp = await client.post(
            "/api/devices/verification-jobs",
            json=device_payload(
                default_host_id,
                identity_value="avd:Pixel_8_API_35",
                connection_target="Pixel_8_API_35",
                name="Pixel 8 Host One",
                device_type="emulator",
            ),
        )
        second_resp = await client.post(
            "/api/devices/verification-jobs",
            json=device_payload(
                other_host_id,
                identity_value="avd:Pixel_8_API_35",
                connection_target="Pixel_8_API_35",
                name="Pixel 8 Host Two",
                device_type="emulator",
            ),
        )
        first_job = await _wait_for_job(client, first_resp.json()["job_id"], session_factory=session_factory)
        second_job = await _wait_for_job(client, second_resp.json()["job_id"], session_factory=session_factory)

    assert first_job["status"] == "completed"
    assert second_job["status"] == "completed"

    devices = (await client.get("/api/devices")).json()
    avd_devices = [device for device in devices if device["identity_value"] == "avd:Pixel_8_API_35"]
    assert len(avd_devices) == 2
    assert {device["host_id"] for device in avd_devices} == {default_host_id, other_host_id}
    assert {device["connection_type"] for device in avd_devices} == {"virtual"}


async def test_verification_rejects_virtual_connection_type_for_real_device(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    session_factory = async_sessionmaker(db_session.bind, class_=AsyncSession, expire_on_commit=False)

    resp = await client.post(
        "/api/devices/verification-jobs",
        json=device_payload(
            default_host_id,
            identity_value="verify-virtual-real-device",
            connection_target="verify-virtual-real-device",
            connection_type="virtual",
            device_type="real_device",
        ),
    )
    job = await _wait_for_job(client, resp.json()["job_id"], session_factory=session_factory)

    assert job["status"] == "failed"
    _assert_job_stage(
        job,
        stage="validation",
        status="failed",
        detail_contains="only supported for emulators and simulators",
    )


async def test_verification_job_health_failure_blocks_save(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    session_factory = async_sessionmaker(db_session.bind, class_=AsyncSession, expire_on_commit=False)
    mock_http_client = _mock_http_client(
        payload={
            "healthy": False,
            "checks": [
                {"check_id": "adb_connected", "ok": False, "message": "offline"},
                {"check_id": "boot_completed", "ok": True, "message": "1"},
            ],
        }
    )

    with patch("app.services.device_verification_runner.httpx.AsyncClient", return_value=mock_http_client):
        resp = await client.post(
            "/api/devices/verification-jobs",
            json=device_payload(default_host_id, identity_value="verify-health"),
        )
        assert resp.status_code == 202
        job = await _wait_for_job(client, resp.json()["job_id"], session_factory=session_factory)

    assert job["status"] == "failed"
    _assert_job_stage(job, stage="device_health", status="failed", detail_contains="adb connected failed")
    assert (await client.get("/api/devices")).json() == []


async def test_verification_job_probe_failure_runs_cleanup_and_does_not_save(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    session_factory = async_sessionmaker(db_session.bind, class_=AsyncSession, expire_on_commit=False)
    start_mock = AsyncMock(return_value=TemporaryNodeHandle(port=4723, pid=12345))
    stop_mock = AsyncMock()
    healthy_http_client = _mock_http_client(payload={"healthy": True, "adb_connected": {"connected": True}})

    with (
        patch("app.services.device_verification_execution.start_temporary_node", start_mock),
        patch("app.services.device_verification_execution.stop_temporary_node", stop_mock),
        patch("app.services.device_verification_runner.httpx.AsyncClient", return_value=healthy_http_client),
        patch(
            "app.services.device_verification_runner.session_viability.probe_session_via_grid",
            new=AsyncMock(return_value=(False, "Session startup failed")),
        ),
    ):
        resp = await client.post(
            "/api/devices/verification-jobs",
            json=device_payload(default_host_id, identity_value="verify-probe-fail"),
        )
        job = await _wait_for_job(client, resp.json()["job_id"], session_factory=session_factory)

    assert job["status"] == "failed"
    _assert_job_stage(job, stage="session_probe", status="failed", detail_contains="Session startup failed")
    stop_mock.assert_awaited_once()
    assert (await client.get("/api/devices")).json() == []


async def test_verification_job_cleanup_failure_blocks_save(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    session_factory = async_sessionmaker(db_session.bind, class_=AsyncSession, expire_on_commit=False)
    start_mock = AsyncMock(return_value=TemporaryNodeHandle(port=4723, pid=12345))
    stop_mock = AsyncMock(side_effect=RuntimeError("stop failed"))
    healthy_http_client = _mock_http_client(payload={"healthy": True, "adb_connected": {"connected": True}})

    with (
        patch("app.services.device_verification_execution.start_temporary_node", start_mock),
        patch("app.services.device_verification_execution.stop_temporary_node", stop_mock),
        patch("app.services.device_verification_runner.httpx.AsyncClient", return_value=healthy_http_client),
        patch(
            "app.services.device_verification_runner.session_viability.probe_session_via_grid",
            new=AsyncMock(return_value=(True, None)),
        ),
    ):
        resp = await client.post(
            "/api/devices/verification-jobs",
            json=device_payload(default_host_id, identity_value="verify-cleanup-fail", auto_manage=False),
        )
        job = await _wait_for_job(client, resp.json()["job_id"], session_factory=session_factory)

    assert job["status"] == "failed"
    _assert_job_stage(job, stage="cleanup", status="failed", detail_contains="stop failed")
    assert (await client.get("/api/devices")).json() == []


async def test_verification_job_duplicate_identity_is_reported_during_validation(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    session_factory = async_sessionmaker(db_session.bind, class_=AsyncSession, expire_on_commit=False)
    await create_device_record(
        db_session,
        host_id=default_host_id,
        identity_value="verify-dup",
        connection_target="verify-dup",
        name=DEVICE_PAYLOAD["name"],
        os_version=DEVICE_PAYLOAD["os_version"],
    )

    resp = await client.post(
        "/api/devices/verification-jobs",
        json=device_payload(default_host_id, identity_value="verify-dup"),
    )
    job = await _wait_for_job(client, resp.json()["job_id"], session_factory=session_factory)

    assert job["status"] == "failed"
    _assert_job_stage(job, stage="validation", status="failed", detail_contains="already registered")


async def test_verification_rejects_global_scoped_duplicate_identity_across_hosts(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    """Global-scoped identity values (e.g. iOS UDIDs) must be unique across all hosts."""
    other_host_resp = await client.post(
        "/api/hosts",
        json={**HOST_PAYLOAD, "hostname": "verify-host-3", "ip": "10.0.0.22"},
    )
    assert other_host_resp.status_code == 201
    other_host_id = str(other_host_resp.json()["id"])

    await create_device_record(
        db_session,
        host_id=default_host_id,
        identity_value="ios-udid-dup-001",
        connection_target="ios-udid-dup-001",
        name="Host One iPhone",
        pack_id="appium-xcuitest",
        platform_id="ios",
        identity_scheme="apple_udid",
        identity_scope="global",
        os_version=DEVICE_PAYLOAD["os_version"],
    )

    session_factory = async_sessionmaker(db_session.bind, class_=AsyncSession, expire_on_commit=False)
    resp = await client.post(
        "/api/devices/verification-jobs",
        json={
            "host_id": other_host_id,
            "identity_value": "ios-udid-dup-001",
            "name": "Host Two iPhone",
            "pack_id": "appium-xcuitest",
            "platform_id": "ios",
            "identity_scheme": "apple_udid",
            "identity_scope": "global",
            "os_version": DEVICE_PAYLOAD["os_version"],
        },
    )
    job = await _wait_for_job(client, resp.json()["job_id"], session_factory=session_factory)

    assert job["status"] == "failed"
    _assert_job_stage(job, stage="validation", status="failed", detail_contains="already registered")


async def test_verification_with_host_still_reports_node_start_failures(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    session_factory = async_sessionmaker(db_session.bind, class_=AsyncSession, expire_on_commit=False)
    healthy_http_client = _mock_http_client(payload={"healthy": True, "adb_connected": {"connected": True}})
    with (
        patch(
            "app.services.device_verification_execution.start_temporary_node",
            new=AsyncMock(side_effect=NodeManagerError("appium missing")),
        ),
        patch("app.services.device_verification_runner.httpx.AsyncClient", return_value=healthy_http_client),
    ):
        resp = await client.post(
            "/api/devices/verification-jobs",
            json=device_payload(default_host_id, identity_value="verify-node-fail"),
        )
        job = await _wait_for_job(client, resp.json()["job_id"], session_factory=session_factory)

    assert job["status"] == "failed"
    _assert_job_stage(job, stage="node_start", status="failed", detail_contains="appium missing")


async def test_verification_fails_when_started_appium_never_becomes_reachable(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    session_factory = async_sessionmaker(db_session.bind, class_=AsyncSession, expire_on_commit=False)

    with (
        patch(
            "app.services.device_verification_execution.start_temporary_node",
            new=AsyncMock(side_effect=NodeManagerError("Appium is not reachable")),
        ),
        patch(
            "app.services.device_verification_execution.run_device_health",
            new_callable=AsyncMock,
            return_value=None,
        ),
    ):
        resp = await client.post(
            "/api/devices/verification-jobs",
            json=device_payload(default_host_id, identity_value="verify-node-unreachable"),
        )
        job = await _wait_for_job(client, resp.json()["job_id"], session_factory=session_factory)

    assert job["status"] == "failed"
    _assert_job_stage(job, stage="node_start", status="failed", detail_contains="Appium is not reachable")
    assert (await client.get("/api/devices")).json() == []


async def test_existing_device_verification_marks_device_verified(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    session_factory = async_sessionmaker(db_session.bind, class_=AsyncSession, expire_on_commit=False)
    device = Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value=f"discovered-{uuid.uuid4()}",
        connection_target=f"discovered-{uuid.uuid4()}",
        name="Discovered Pixel",
        os_version="14",
        operational_state=DeviceOperationalState.offline,
        host_id=uuid.UUID(default_host_id),
        verified_at=None,
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )
    db_session.add(device)
    await db_session.commit()
    await db_session.refresh(device)

    start_temporary = AsyncMock(return_value=TemporaryNodeHandle(port=4723, pid=12345))
    with (
        patch("app.services.device_verification_execution.start_temporary_node", new=start_temporary),
        patch("app.services.device_verification_execution.stop_temporary_node", new=AsyncMock()),
        patch(
            "app.services.device_verification_runner.httpx.AsyncClient",
            return_value=_mock_http_client(payload={"healthy": True, "adb_connected": {"connected": True}}),
        ),
        patch(
            "app.services.device_verification_runner.session_viability.probe_session_via_grid",
            new=AsyncMock(return_value=(True, None)),
        ),
    ):
        resp = await client.post(
            f"/api/devices/{device.id}/verification-jobs",
            json={"host_id": default_host_id},
        )
        assert resp.status_code == 202
        job = await _wait_for_job(client, resp.json()["job_id"], session_factory=session_factory)

    assert job["status"] == "completed"
    transient_device = start_temporary.await_args.args[1]
    assert transient_device.pack_id == "appium-uiautomator2"
    updated = (await client.get(f"/api/devices/{device.id}")).json()
    assert updated["readiness_state"] == "verified"
    assert updated["verified_at"] is not None


async def test_existing_device_verification_requires_missing_setup_fields(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    session_factory = async_sessionmaker(db_session.bind, class_=AsyncSession, expire_on_commit=False)
    device = Device(
        pack_id="appium-roku-dlenroc",
        platform_id="roku_network",
        identity_scheme="roku_serial",
        identity_scope="global",
        identity_value="roku-serial-1",
        connection_target="192.168.1.60",
        name="Roku Discovery",
        os_version="unknown",
        operational_state=DeviceOperationalState.offline,
        device_type="real_device",
        connection_type="network",
        ip_address="192.168.1.60",
        host_id=uuid.UUID(default_host_id),
        verified_at=None,
        device_config={},
    )
    db_session.add(device)
    await db_session.commit()
    await db_session.refresh(device)

    resp = await client.post(
        f"/api/devices/{device.id}/verification-jobs",
        json={"host_id": default_host_id},
    )
    assert resp.status_code == 202
    job = await _wait_for_job(client, resp.json()["job_id"], session_factory=session_factory)
    assert job["status"] == "failed"
    assert "roku_password" in (job["error"] or "")


async def test_existing_device_verification_can_replace_device_config(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    session_factory = async_sessionmaker(db_session.bind, class_=AsyncSession, expire_on_commit=False)
    device = Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value=f"config-verify-{uuid.uuid4()}",
        connection_target="config-verify-target",
        name="Config Verify Device",
        os_version="14",
        operational_state=DeviceOperationalState.offline,
        device_config={"old": True},
        host_id=uuid.UUID(default_host_id),
        verified_at=None,
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )
    db_session.add(device)
    await db_session.commit()
    await db_session.refresh(device)

    with (
        patch(
            "app.services.device_verification_execution.start_temporary_node",
            new=AsyncMock(return_value=TemporaryNodeHandle(port=4723, pid=12345)),
        ),
        patch("app.services.device_verification_execution.stop_temporary_node", new=AsyncMock()),
        patch(
            "app.services.device_verification_runner.httpx.AsyncClient",
            return_value=_mock_http_client(payload={"healthy": True, "ecp_reachable": {"reachable": True}}),
        ),
        patch(
            "app.services.device_verification_runner.session_viability.probe_session_via_grid",
            new=AsyncMock(return_value=(True, None)),
        ),
    ):
        resp = await client.post(
            f"/api/devices/{device.id}/verification-jobs",
            json={
                "host_id": default_host_id,
                "device_config": {"new": True},
                "replace_device_config": True,
            },
        )
        assert resp.status_code == 202
        job = await _wait_for_job(client, resp.json()["job_id"], session_factory=session_factory)

    assert job["status"] == "completed"
    config_resp = await client.get(f"/api/devices/{device.id}/config")
    assert config_resp.status_code == 200
    assert config_resp.json() == {"new": True}


async def test_existing_device_verification_config_replace_writes_verbatim(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    session_factory = async_sessionmaker(db_session.bind, class_=AsyncSession, expire_on_commit=False)
    device = Device(
        pack_id="appium-roku-dlenroc",
        platform_id="roku_network",
        identity_scheme="roku_serial",
        identity_scope="global",
        identity_value=f"roku-mask-{uuid.uuid4()}",
        connection_target="192.168.1.55",
        name="Masked Roku",
        os_version="12.5",
        operational_state=DeviceOperationalState.offline,
        device_config={"roku_password": "super-secret", "label": "den"},
        host_id=uuid.UUID(default_host_id),
        verified_at=None,
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.network,
        ip_address="192.168.1.55",
    )
    db_session.add(device)
    await db_session.commit()
    await db_session.refresh(device)

    start_temporary = AsyncMock(return_value=TemporaryNodeHandle(port=4723, pid=12345))
    with (
        patch("app.services.device_verification_execution.start_temporary_node", new=start_temporary),
        patch("app.services.device_verification_execution.stop_temporary_node", new=AsyncMock()),
        patch(
            "app.services.device_verification_runner.httpx.AsyncClient",
            return_value=_mock_http_client(
                payload={"healthy": True, "checks": [{"check_id": "ecp_reachable", "ok": True, "message": ""}]}
            ),
        ),
        patch(
            "app.services.device_verification_runner.session_viability.probe_session_via_grid",
            new=AsyncMock(return_value=(True, None)),
        ),
    ):
        resp = await client.post(
            f"/api/devices/{device.id}/verification-jobs",
            json={
                "host_id": default_host_id,
                "device_config": {"roku_password": "rotated-secret", "label": "living room"},
                "replace_device_config": True,
            },
        )
        assert resp.status_code == 202
        job = await _wait_for_job(client, resp.json()["job_id"], session_factory=session_factory)

    assert job["status"] == "completed"
    config_resp = await client.get(f"/api/devices/{device.id}/config")
    assert config_resp.status_code == 200
    assert config_resp.json() == {"roku_password": "rotated-secret", "label": "living room"}


async def test_existing_device_verification_stops_running_node_before_updated_probe(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    session_factory = async_sessionmaker(db_session.bind, class_=AsyncSession, expire_on_commit=False)
    device = Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value=f"running-verify-{uuid.uuid4()}",
        connection_target="running-verify-target",
        name="Running Verify Device",
        os_version="14",
        operational_state=DeviceOperationalState.available,
        device_config={"newCommandTimeout": 60},
        host_id=uuid.UUID(default_host_id),
        verified_at=datetime.now(UTC),
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )
    db_session.add(device)
    await db_session.flush()
    db_session.add(
        AppiumNode(
            device_id=device.id,
            port=4723,
            grid_url="http://hub:4444",
            pid=12345,
            state=NodeState.running,
        )
    )
    await db_session.commit()
    await db_session.refresh(device)

    events: list[str] = []

    async def stop_running_node(
        _db: AsyncSession, stopped_device: Device, *, caller: str = "verification"
    ) -> AppiumNode:
        events.append(f"stop:{stopped_device.id}")
        assert stopped_device.id == device.id
        assert stopped_device.appium_node is not None
        return stopped_device.appium_node

    async def start_updated_node(_db: AsyncSession, probe_device: Device, **_kwargs: object) -> TemporaryNodeHandle:
        events.append(f"start:{probe_device.device_config}")
        assert probe_device.device_config == {"newCommandTimeout": 120}
        return TemporaryNodeHandle(port=4724, pid=67890)

    with (
        patch("app.services.device_verification_execution.stop_node", new=AsyncMock(side_effect=stop_running_node)),
        patch(
            "app.services.device_verification_execution.start_temporary_node",
            new=AsyncMock(side_effect=start_updated_node),
        ),
        patch(
            "app.services.device_verification_runner.httpx.AsyncClient",
            return_value=_mock_http_client(payload={"healthy": True, "adb_connected": {"connected": True}}),
        ),
        patch(
            "app.services.device_verification_runner.session_viability.probe_session_via_grid",
            new=AsyncMock(return_value=(True, None)),
        ),
    ):
        resp = await client.post(
            f"/api/devices/{device.id}/verification-jobs",
            json={
                "host_id": default_host_id,
                "device_config": {"newCommandTimeout": 120},
                "replace_device_config": True,
            },
        )
        assert resp.status_code == 202
        job = await _wait_for_job(client, resp.json()["job_id"], session_factory=session_factory)

    assert job["status"] == "completed"
    assert events[:2] == [f"stop:{device.id}", "start:{'newCommandTimeout': 120}"]


async def test_android_network_verification_resolves_stable_identity_before_save(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    session_factory = async_sessionmaker(db_session.bind, class_=AsyncSession, expire_on_commit=False)
    http_client = _mock_resolution_http_client(
        resolution_payload={
            "identity_scheme": "android_serial",
            "identity_scope": "host",
            "identity_value": "stable-serial-001",
            "connection_target": "192.168.1.55:5555",
            "name": "Network Pixel",
            "platform_id": "android_mobile",
            "os_version": "14",
            "device_type": "real_device",
            "connection_type": "network",
            "ip_address": "192.168.1.55",
        }
    )

    with (
        patch("app.services.device_verification_runner.httpx.AsyncClient", return_value=http_client),
        patch(
            "app.services.device_verification_execution.start_temporary_node",
            new=AsyncMock(return_value=TemporaryNodeHandle(port=4723, pid=12345)),
        ),
        patch("app.services.device_verification_execution.stop_temporary_node", new=AsyncMock()),
        patch(
            "app.services.device_verification_runner.session_viability.probe_session_via_grid",
            new=AsyncMock(return_value=(True, None)),
        ),
    ):
        resp = await client.post(
            "/api/devices/verification-jobs",
            json={
                "host_id": default_host_id,
                "name": "Network Pixel",
                "pack_id": "appium-uiautomator2",
                "platform_id": "android_mobile",
                "identity_scheme": "android_serial",
                "identity_scope": "host",
                "device_type": "real_device",
                "connection_type": "network",
                "connection_target": "192.168.1.55:5555",
                "ip_address": "192.168.1.55",
                "os_version": "unknown",
            },
        )
        assert resp.status_code == 202
        job = await _wait_for_job(client, resp.json()["job_id"], session_factory=session_factory)

    assert job["status"] == "completed"
    devices = (await client.get("/api/devices")).json()
    assert devices[0]["identity_value"] == "stable-serial-001"
    assert devices[0]["connection_target"] == "192.168.1.55:5555"


async def test_roku_verification_resolves_identity_from_ip_before_save(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    session_factory = async_sessionmaker(db_session.bind, class_=AsyncSession, expire_on_commit=False)
    http_client = _mock_resolution_http_client(
        resolution_payload={
            "identity_scheme": "roku_serial",
            "identity_scope": "global",
            "identity_value": "YJ1234567890",
            "connection_target": "192.168.1.50",
            "ip_address": "192.168.1.50",
            "device_type": "real_device",
            "connection_type": "network",
            "os_version": "12.5.0",
            "manufacturer": "Roku",
            "model": "Roku Ultra",
            "field_errors": [],
        },
        health_payload={"healthy": True, "checks": [{"check_id": "ecp_reachable", "ok": True, "message": ""}]},
    )
    start_temporary = AsyncMock(return_value=TemporaryNodeHandle(port=4723, pid=12345))

    with (
        patch("app.services.device_verification_runner.httpx.AsyncClient", return_value=http_client),
        patch("app.services.device_verification_execution.start_temporary_node", new=start_temporary),
        patch("app.services.device_verification_execution.stop_temporary_node", new=AsyncMock()),
        patch(
            "app.services.device_verification_runner.session_viability.probe_session_via_grid",
            new=AsyncMock(return_value=(True, None)),
        ),
    ):
        resp = await client.post(
            "/api/devices/verification-jobs",
            json={
                "host_id": default_host_id,
                "name": "Living Room Roku",
                "pack_id": "appium-roku-dlenroc",
                "platform_id": "roku_network",
                "identity_scheme": "roku_serial",
                "identity_scope": "global",
                "device_type": "real_device",
                "connection_type": "network",
                "ip_address": "192.168.1.50",
                "os_version": "unknown",
                "device_config": {"roku_password": "devpass"},
            },
        )
        assert resp.status_code == 202
        job = await _wait_for_job(client, resp.json()["job_id"], session_factory=session_factory)

    assert job["status"] == "completed", job
    transient_device = start_temporary.await_args.args[1]
    assert transient_device.identity_value == "YJ1234567890"
    assert transient_device.connection_target == "192.168.1.50"

    devices = (await client.get("/api/devices")).json()
    assert devices[0]["identity_value"] == "YJ1234567890"
    assert devices[0]["connection_target"] == "192.168.1.50"
    assert devices[0]["ip_address"] == "192.168.1.50"
    assert devices[0]["manufacturer"] == "Roku"
    assert devices[0]["model"] == "Roku Ultra"


async def test_android_network_verification_fails_when_stable_identity_cannot_be_resolved(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    session_factory = async_sessionmaker(db_session.bind, class_=AsyncSession, expire_on_commit=False)
    http_client = _mock_resolution_http_client(resolution_status=404)

    with patch("app.services.device_verification_runner.httpx.AsyncClient", return_value=http_client):
        resp = await client.post(
            "/api/devices/verification-jobs",
            json={
                "host_id": default_host_id,
                "name": "Broken Network Pixel",
                "pack_id": "appium-uiautomator2",
                "platform_id": "android_mobile",
                "identity_scheme": "android_serial",
                "identity_scope": "host",
                "device_type": "real_device",
                "connection_type": "network",
                "connection_target": "192.168.1.99:5555",
                "ip_address": "192.168.1.99",
                "os_version": "unknown",
            },
        )
        assert resp.status_code == 202
        job = await _wait_for_job(client, resp.json()["job_id"], session_factory=session_factory)

    assert job["status"] == "failed"
    assert "stable identity" in (job["error"] or "")


async def test_android_network_verification_fails_when_resolution_lacks_identity(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    session_factory = async_sessionmaker(db_session.bind, class_=AsyncSession, expire_on_commit=False)
    http_client = _mock_resolution_http_client(resolution_payload={"success": True, "state": "192.168.1.99"})

    with patch("app.services.device_verification_runner.httpx.AsyncClient", return_value=http_client):
        resp = await client.post(
            "/api/devices/verification-jobs",
            json={
                "host_id": default_host_id,
                "name": "Broken Fire TV",
                "pack_id": "appium-uiautomator2",
                "platform_id": "firetv_real",
                "identity_scheme": "android_serial",
                "identity_scope": "host",
                "device_type": "real_device",
                "connection_type": "network",
                "connection_target": "192.168.1.99",
                "ip_address": "192.168.1.99",
                "os_version": "unknown",
            },
        )
        assert resp.status_code == 202
        job = await _wait_for_job(client, resp.json()["job_id"], session_factory=session_factory)

    assert job["status"] == "failed"
    assert "stable identity" in (job["error"] or "")
    assert job["error"] != "Verification job crashed unexpectedly"


async def test_stale_running_verification_jobs_are_reset_and_resumed(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    session_factory = async_sessionmaker(db_session.bind, class_=AsyncSession, expire_on_commit=False)
    healthy_http_client = _mock_http_client(payload={"healthy": True, "adb_connected": {"connected": True}})

    with (
        patch(
            "app.services.device_verification_execution.start_temporary_node",
            new=AsyncMock(return_value=TemporaryNodeHandle(port=4723, pid=12345)),
        ),
        patch("app.services.device_verification_execution.stop_temporary_node", new=AsyncMock()),
        patch("app.services.device_verification_runner.httpx.AsyncClient", return_value=healthy_http_client),
        patch(
            "app.services.device_verification_runner.session_viability.probe_session_via_grid",
            new=AsyncMock(return_value=(True, None)),
        ),
    ):
        resp = await client.post(
            "/api/devices/verification-jobs",
            json=device_payload(default_host_id, identity_value="verify-stale"),
        )
        assert resp.status_code == 202
        job_id = resp.json()["job_id"]

        async with session_factory() as db:
            persisted = await db.get(Job, uuid.UUID(job_id))
            assert persisted is not None
            persisted.status = "running"
            persisted.started_at = datetime.now(UTC) - timedelta(minutes=11)
            persisted.snapshot = {
                **persisted.snapshot,
                "status": "running",
                "current_stage": "node_start",
            }
            await db.commit()

        recovered = await reset_stale_running_jobs(session_factory)
        assert recovered == 1
        job = await _wait_for_job(client, job_id, session_factory=session_factory)

    assert job["status"] == "completed"


def test_health_failure_detail_reads_checks_shape() -> None:
    assert _health_failure_detail({"detail": "ADB gone"}) == "ADB gone"
    assert (
        _health_failure_detail(
            {
                "healthy": False,
                "checks": [
                    {"check_id": "adb_connected", "ok": False, "message": "device not found"},
                    {"check_id": "boot_completed", "ok": True, "message": "1"},
                ],
            }
        )
        == "adb connected failed (device not found)"
    )
    assert _health_failure_detail({"healthy": False, "checks": []}) == "Device health checks failed"
    assert _health_failure_detail({"healthy": False}) == "Device health checks failed"


async def test_verification_rejects_disabled_pack(client: AsyncClient, db_session: AsyncSession, db_host: Host) -> None:
    await seed_test_packs(db_session)
    pack = await db_session.scalar(select(DriverPack).where(DriverPack.id == "appium-uiautomator2"))
    assert pack is not None
    pack.state = "disabled"
    await db_session.commit()
    payload = {
        **DEVICE_PAYLOAD,
        "host_id": str(db_host.id),
    }
    resp = await client.post("/api/devices/verification-jobs", json=payload)
    assert resp.status_code == 422
    body = resp.json()
    assert body["error"]["details"]["code"] == "pack_disabled"
