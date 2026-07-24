import asyncio
import uuid
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import httpx2 as httpx
import pytest
import pytest_asyncio
from httpx2 import AsyncClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.appium_nodes.exceptions import NodeManagerError
from app.appium_nodes.models import AppiumDesiredState, AppiumNode
from app.appium_nodes.services.reconciler_agent import ReconcilerAgentService
from app.devices.models import ConnectionType, Device, DeviceIntent, DeviceOperationalState, DeviceType
from app.devices.schemas.device import DeviceVerificationCreate, DeviceVerificationUpdate
from app.devices.services.capability import DeviceCapabilityService
from app.devices.services.identity_conflicts import DeviceIdentityConflictService
from app.devices.services.intent import IntentService
from app.devices.services.intent_types import VERIFICATION_OPERATION_ID_KEY, verification_intent_source
from app.devices.services.service import DeviceCrudService
from app.jobs import JOB_KIND_DEVICE_VERIFICATION
from app.jobs.models import Job
from app.jobs.queue import DurableJobService
from app.lifecycle.services.operator_node import (
    OperatorNodeLifecycleService,
    operator_stop_active,
    operator_stop_intents,
)
from app.lifecycle.services.recovery_job import RecoveryJobService
from app.packs.models import DriverPack
from app.sessions.models import Session, SessionStatus
from app.sessions.service_viability import SessionViabilityService, get_session_viability
from app.verification.services.execution import (
    AgentCallContext,
    NodeEffectSnapshot,
    VerificationExecutionService,
    _health_failure_detail,
)
from app.verification.services.job_state import new_job, reset_snapshot_for_retry
from app.verification.services.preparation import (
    PreparedVerificationEffect,
    VerificationPreparationService,
    _PackCoords,
)
from app.verification.services.runner import VerificationRunnerService
from tests.conftest import settings_service
from tests.fakes import build_review_service
from tests.helpers import create_device_record, delete_jobs_by_kind
from tests.helpers import test_event_bus as event_bus
from tests.packs.factories import seed_test_packs
from tests.verification._lease_helpers import register_verification_node_intent

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from contextlib import AbstractContextManager

    from app.agent_comm.client import AgentClientFactory
    from app.hosts.models import Host


def _noop_circuit_breaker() -> Mock:
    """Return a permissive circuit-breaker mock (before_request always allows)."""
    cb = Mock()
    cb.before_request = AsyncMock(return_value=None)
    cb.record_success = AsyncMock()
    cb.record_failure = AsyncMock()
    return cb


def _publisher_mock() -> AsyncMock:
    """EventPublisher mock whose ``queue_for_session`` stays synchronous.

    ``EventPublisher.queue_for_session`` is a sync method; a bare ``AsyncMock``
    would return an un-awaited coroutine (RuntimeWarning) when production calls
    it without ``await`` (e.g. ``set_operational_state``).
    """
    publisher = AsyncMock()
    publisher.queue_for_session = Mock()
    return publisher


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
    async with session_factory() as db:
        await delete_jobs_by_kind(db, kind=JOB_KIND_DEVICE_VERIFICATION)
    yield
    async with session_factory() as db:
        await delete_jobs_by_kind(db, kind=JOB_KIND_DEVICE_VERIFICATION)


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

    resp = await client.post("/api/verification/jobs", json=payload)

    assert resp.status_code == 422


async def test_create_verification_requires_platform_id(client: AsyncClient, default_host_id: str) -> None:
    payload = device_payload(default_host_id)
    payload.pop("platform_id")

    resp = await client.post("/api/verification/jobs", json=payload)

    assert resp.status_code == 422


async def _wait_for_job(
    client: AsyncClient,
    job_id: str,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    probe_result: tuple[bool, str | None] = (True, None),
    node_manager: object = None,
) -> dict[str, Any]:
    for _ in range(100):
        resp = await client.get(f"/api/verification/jobs/{job_id}")
        assert resp.status_code == 200
        job = resp.json()
        if job["status"] in {"completed", "failed"}:
            return dict(job)
        _viability = SessionViabilityService(
            publisher=_publisher_mock(),
            settings=settings_service,
            session_factory=session_factory,
            capability=DeviceCapabilityService(),
            health=AsyncMock(),
        )
        _viability.probe_session_direct = AsyncMock(return_value=probe_result)  # type: ignore[method-assign]
        await DurableJobService(
            session_factory=session_factory,
            publisher=_publisher_mock(),
            settings=settings_service,
            circuit_breaker=_noop_circuit_breaker(),
            remediation_runner=AsyncMock(),
            verification_runner=VerificationRunnerService(
                session_factory=session_factory,
                publisher=_publisher_mock(),
                settings=settings_service,
                circuit_breaker=_noop_circuit_breaker(),
                preparation=VerificationPreparationService(
                    settings=settings_service,
                    circuit_breaker=_noop_circuit_breaker(),
                    crud=DeviceCrudService(identity=DeviceIdentityConflictService(), publisher=event_bus),
                    identity=DeviceIdentityConflictService(),
                    publisher=event_bus,
                    session_factory=session_factory,
                ),
                execution=VerificationExecutionService(
                    review=build_review_service(),
                    publisher=_publisher_mock(),
                    agent=AgentCallContext(settings=settings_service, circuit_breaker=_noop_circuit_breaker()),
                    crud=DeviceCrudService(identity=DeviceIdentityConflictService(), publisher=event_bus),
                    viability=_viability,
                    capability=DeviceCapabilityService(),
                    reconciler=AsyncMock(),
                    session_factory=session_factory,
                    node_manager=node_manager
                    if node_manager is not None
                    else ReconcilerAgentService(
                        settings=settings_service,
                        operator=OperatorNodeLifecycleService(
                            review=build_review_service(), settings=settings_service, publisher=event_bus
                        ),
                    ),
                ),
            ),
            recovery_runner=RecoveryJobService(
                session_factory=session_factory,
                publisher=_publisher_mock(),
                settings=settings_service,
                lifecycle_policy=AsyncMock(),
                viability=AsyncMock(),
            ),
            run_teardown_runner=AsyncMock(),
            session_kill_runner=AsyncMock(),
        ).run_pending_once()
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


def _patch_running_node(
    *,
    port: int = 4723,
    pid: int = 12345,
    active_connection_target: str = "emulator-5554",
) -> AbstractContextManager[Any]:
    async def wait_for_node(
        self: VerificationExecutionService,
        node_id: uuid.UUID,
        *,
        timeout_sec: int,
    ) -> NodeEffectSnapshot:
        del timeout_sec
        async with self._session_factory.begin() as db:
            node = await db.get(AppiumNode, node_id)
            assert node is not None
            node.port = port
            node.pid = pid
            node.active_connection_target = active_connection_target
        return NodeEffectSnapshot(node_id, active_connection_target)

    return patch.object(
        VerificationExecutionService,
        "wait_for_node_running",
        new=wait_for_node,
    )


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


async def test_verification_job_success_keeps_verified_node(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    session_factory = async_sessionmaker(db_session.bind, class_=AsyncSession, expire_on_commit=False)
    stop_mock = AsyncMock()
    healthy_http_client = _mock_http_client(payload={"healthy": True, "adb_connected": {"connected": True}})

    with (
        _patch_running_node(active_connection_target="emulator-5554"),
        patch.object(ReconcilerAgentService, "stop_node", stop_mock),
        patch("app.verification.services.runner.httpx.AsyncClient", return_value=healthy_http_client),
    ):
        resp = await client.post("/api/verification/jobs", json=device_payload(default_host_id))
        assert resp.status_code == 202
        assert resp.json()["current_stage"] is None
        assert resp.json()["current_stage_status"] is None
        assert resp.json()["detail"] is None
        job = await _wait_for_job(client, resp.json()["job_id"], session_factory=session_factory)

    assert job["status"] == "completed", job
    _assert_job_stage(job, stage="save_device", status="passed", detail_contains="Device saved after verification")
    stop_mock.assert_not_awaited()

    devices = (await client.get("/api/devices")).json()
    assert len(devices) == 1
    assert devices[0]["identity_value"] == DEVICE_PAYLOAD["identity_value"]
    assert devices[0]["operational_state"] == "available"

    detail = (await client.get(f"/api/devices/{devices[0]['id']}")).json()
    assert detail["appium_node"] is not None
    assert detail["appium_node"]["desired_state"] == "running"
    assert detail["appium_node"]["effective_state"] == "running"
    assert detail["appium_node"]["pid"] == 12345
    assert detail["appium_node"]["active_connection_target"] == "emulator-5554"

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
    healthy_http_client = _mock_http_client(payload={"healthy": True, "adb_connected": {"connected": True}})

    with (
        _patch_running_node(active_connection_target=DEVICE_PAYLOAD["identity_value"]),
        patch("app.verification.services.runner.httpx.AsyncClient", return_value=healthy_http_client),
    ):
        resp = await client.post("/api/verification/jobs", json=device_payload(default_host_id))
        assert resp.status_code == 202
        job_id = resp.json()["job_id"]
        job = await _wait_for_job(client, job_id, session_factory=session_factory)

    assert job["status"] == "completed", job

    async with session_factory() as verify_db:
        job_row = await verify_db.get(Job, uuid.UUID(job_id))
        assert job_row is not None
        cleanup_stage = next(s for s in job_row.snapshot["stages"] if s["name"] == "cleanup")
        node = (await verify_db.execute(select(AppiumNode))).scalar_one()
    assert cleanup_stage["status"] == "passed"
    assert node.port == 4723
    assert node.pid == 12345
    assert node.active_connection_target == DEVICE_PAYLOAD["identity_value"]
    assert node.observed_running
    assert node.desired_state == AppiumDesiredState.running
    assert node.restart_requested_at is None


async def test_retain_verified_node_acquires_row_lock(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    session_factory = async_sessionmaker(db_session.bind, class_=AsyncSession, expire_on_commit=False)
    healthy_http_client = _mock_http_client(payload={"healthy": True, "adb_connected": {"connected": True}})

    from app.verification.services import execution as device_verification_execution

    real_lock = device_verification_execution.device_locking.lock_device
    spy = AsyncMock(side_effect=real_lock)

    with (
        _patch_running_node(active_connection_target="emulator-5554"),
        patch.object(ReconcilerAgentService, "stop_node", AsyncMock()),
        patch("app.verification.services.runner.httpx.AsyncClient", return_value=healthy_http_client),
        patch("app.verification.services.execution.device_locking.lock_device", spy),
    ):
        resp = await client.post("/api/verification/jobs", json=device_payload(default_host_id))
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
    healthy_http_client = _mock_http_client(payload={"healthy": True, "adb_connected": {"connected": True}})

    with (
        _patch_running_node(active_connection_target=DEVICE_PAYLOAD["identity_value"]),
        patch("app.verification.services.runner.httpx.AsyncClient", return_value=healthy_http_client),
    ):
        resp = await client.post("/api/verification/jobs", json=device_payload(default_host_id))
        assert resp.status_code == 202
        job = await _wait_for_job(client, resp.json()["job_id"], session_factory=session_factory)

    assert job["status"] == "completed"
    _assert_job_stage(job, stage="save_device", status="passed")

    devices = (await client.get("/api/devices")).json()
    assert len(devices) == 1
    assert devices[0]["operational_state"] == "available"


async def test_avd_verification_uses_live_serial_but_saves_stable_avd_identity(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    session_factory = async_sessionmaker(db_session.bind, class_=AsyncSession, expire_on_commit=False)
    health_http_client = _mock_http_client(
        payload={
            "healthy": True,
        }
    )

    with (
        _patch_running_node(active_connection_target="emulator-5554"),
        patch.object(ReconcilerAgentService, "stop_node", new_callable=AsyncMock),
        patch("app.verification.services.runner.httpx.AsyncClient", return_value=health_http_client) as client_factory,
    ):
        resp = await client.post(
            "/api/verification/jobs",
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
    client_factory.assert_called_with(timeout=10)

    devices = (await client.get("/api/devices")).json()
    assert len(devices) == 1
    assert devices[0]["identity_scheme"] == "android_serial"
    assert devices[0]["identity_value"] == "avd:Pixel_8_API_35"
    assert devices[0]["connection_target"] == "Pixel_8_API_35"
    assert devices[0]["connection_type"] == "virtual"
    detail = (await client.get(f"/api/devices/{devices[0]['id']}")).json()
    assert detail["appium_node"]["desired_state"] == "running"
    assert detail["appium_node"]["active_connection_target"] == "emulator-5554"


async def test_avd_verification_probe_uses_node_resolved_serial_when_already_running(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    session_factory = async_sessionmaker(db_session.bind, class_=AsyncSession, expire_on_commit=False)
    health_http_client = _mock_http_client(payload={"healthy": True})

    with (
        _patch_running_node(active_connection_target="emulator-5554"),
        patch.object(ReconcilerAgentService, "stop_node", new_callable=AsyncMock),
        patch("app.verification.services.runner.httpx.AsyncClient", return_value=health_http_client),
    ):
        resp = await client.post(
            "/api/verification/jobs",
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
    assert len(devices) == 1
    assert devices[0]["identity_value"] == "avd:Pixel_8_API_35"
    assert devices[0]["connection_target"] == "Pixel_8_API_35"


async def test_avd_verification_preserves_explicit_virtual_lane_after_normalize(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    session_factory = async_sessionmaker(db_session.bind, class_=AsyncSession, expire_on_commit=False)
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
        _patch_running_node(active_connection_target="emulator-5554"),
        patch.object(ReconcilerAgentService, "stop_node", new_callable=AsyncMock),
        patch("app.verification.services.runner.httpx.AsyncClient", return_value=http_client),
    ):
        resp = await client.post(
            "/api/verification/jobs",
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
    healthy_http_client = _mock_http_client(
        payload={
            "healthy": True,
        }
    )

    with (
        _patch_running_node(active_connection_target="emulator-5554"),
        patch.object(ReconcilerAgentService, "stop_node", new_callable=AsyncMock),
        patch("app.verification.services.runner.httpx.AsyncClient", return_value=healthy_http_client),
    ):
        first_resp = await client.post(
            "/api/verification/jobs",
            json=device_payload(
                default_host_id,
                identity_value="avd:Pixel_8_API_35",
                connection_target="Pixel_8_API_35",
                name="Pixel 8 Host One",
                device_type="emulator",
            ),
        )
        second_resp = await client.post(
            "/api/verification/jobs",
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
        "/api/verification/jobs",
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

    with patch("app.verification.services.runner.httpx.AsyncClient", return_value=mock_http_client):
        resp = await client.post(
            "/api/verification/jobs",
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
    stop_mock = AsyncMock()
    healthy_http_client = _mock_http_client(payload={"healthy": True, "adb_connected": {"connected": True}})

    with (
        _patch_running_node(),
        patch.object(ReconcilerAgentService, "stop_node", stop_mock),
        patch("app.verification.services.runner.httpx.AsyncClient", return_value=healthy_http_client),
    ):
        resp = await client.post(
            "/api/verification/jobs",
            json=device_payload(default_host_id, identity_value="verify-probe-fail"),
        )
        job = await _wait_for_job(
            client,
            resp.json()["job_id"],
            session_factory=session_factory,
            probe_result=(False, "Session startup failed"),
        )

    assert job["status"] == "failed"
    _assert_job_stage(job, stage="session_probe", status="failed", detail_contains="Session startup failed")
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

    with patch(
        "app.verification.services.preparation.normalize_pack_device",
        new=AsyncMock(side_effect=AssertionError("duplicate stable identities must not call the agent")),
    ):
        resp = await client.post(
            "/api/verification/jobs",
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
    with patch(
        "app.verification.services.preparation.normalize_pack_device",
        new=AsyncMock(side_effect=AssertionError("duplicate stable identities must not call the agent")),
    ):
        resp = await client.post(
            "/api/verification/jobs",
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
        patch.object(
            ReconcilerAgentService,
            "start_node",
            new=AsyncMock(side_effect=NodeManagerError("appium missing")),
        ),
        patch("app.verification.services.runner.httpx.AsyncClient", return_value=healthy_http_client),
    ):
        resp = await client.post(
            "/api/verification/jobs",
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
        patch("app.verification.services.preparation.normalize_pack_device", new=AsyncMock(return_value=None)),
        patch.object(
            VerificationExecutionService,
            "wait_for_node_running",
            new=AsyncMock(return_value=None),
        ),
        patch.object(VerificationExecutionService, "run_device_health", new_callable=AsyncMock, return_value=None),
    ):
        resp = await client.post(
            "/api/verification/jobs",
            json=device_payload(default_host_id, identity_value="verify-node-unreachable"),
        )
        job = await _wait_for_job(client, resp.json()["job_id"], session_factory=session_factory)

    assert job["status"] == "failed"
    _assert_job_stage(
        job,
        stage="node_start",
        status="failed",
        detail_contains="Verification node did not reach running state within timeout",
    )
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

    with (
        _patch_running_node(),
        patch.object(ReconcilerAgentService, "stop_node", new=AsyncMock()),
        patch(
            "app.verification.services.runner.httpx.AsyncClient",
            return_value=_mock_http_client(payload={"healthy": True, "adb_connected": {"connected": True}}),
        ),
    ):
        resp = await client.post(
            f"/api/verification/devices/{device.id}/jobs",
            json={"host_id": default_host_id},
        )
        assert resp.status_code == 202
        job = await _wait_for_job(client, resp.json()["job_id"], session_factory=session_factory)

    assert job["status"] == "completed"
    updated = (await client.get(f"/api/devices/{device.id}")).json()
    assert updated["readiness_state"] == "verified"
    assert updated["verified_at"] is not None


async def test_existing_device_verification_update_persists_new_host_id(
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

    with (
        _patch_running_node(),
        patch.object(ReconcilerAgentService, "stop_node", new=AsyncMock()),
        patch(
            "app.verification.services.runner.httpx.AsyncClient",
            return_value=_mock_http_client(payload={"healthy": True, "adb_connected": {"connected": True}}),
        ),
    ):
        resp = await client.post(
            f"/api/verification/devices/{device.id}/jobs",
            json={"host_id": other_host_id},
        )
        assert resp.status_code == 202
        job = await _wait_for_job(client, resp.json()["job_id"], session_factory=session_factory)

    assert job["status"] == "completed"
    updated = (await client.get(f"/api/devices/{device.id}")).json()
    assert updated["readiness_state"] == "verified"
    assert updated["host_id"] == other_host_id


async def test_existing_running_device_verification_can_enter_verifying(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    session_factory = async_sessionmaker(db_session.bind, class_=AsyncSession, expire_on_commit=False)
    device = await create_device_record(
        db_session,
        host_id=default_host_id,
        identity_value=f"running-verify-{uuid.uuid4()}",
        connection_target="running-verify-target",
        name="Running Verify Device",
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        os_version="14",
    )
    device.operational_state_last_emitted = DeviceOperationalState.busy
    node = AppiumNode(
        device_id=device.id,
        port=4723,
        desired_state=AppiumDesiredState.running,
        desired_port=4723,
        pid=1234,
        active_connection_target=device.connection_target,
    )
    db_session.add(node)
    await db_session.commit()

    with (
        _patch_running_node(active_connection_target=device.connection_target),
        patch(
            "app.verification.services.runner.httpx.AsyncClient",
            return_value=_mock_http_client(payload={"healthy": True}),
        ),
    ):
        resp = await client.post(f"/api/verification/devices/{device.id}/jobs", json={"host_id": default_host_id})
        assert resp.status_code == 202
        job = await _wait_for_job(client, resp.json()["job_id"], session_factory=session_factory)

    assert job["status"] == "completed"
    updated = (await client.get(f"/api/devices/{device.id}")).json()
    assert updated["readiness_state"] == "verified"


async def test_update_verification_probe_failure_stops_persisted_node(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    session_factory = async_sessionmaker(db_session.bind, class_=AsyncSession, expire_on_commit=False)
    device = await create_device_record(
        db_session,
        host_id=default_host_id,
        identity_value=f"probe-update-{uuid.uuid4()}",
        connection_target="probe-update-target",
        name="Probe Update Device",
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        os_version="14",
        device_config={"stable": True},
    )

    with (
        _patch_running_node(active_connection_target=device.connection_target),
        patch(
            "app.verification.services.runner.httpx.AsyncClient",
            return_value=_mock_http_client(payload={"healthy": True}),
        ),
    ):
        resp = await client.post(
            f"/api/verification/devices/{device.id}/jobs",
            json={
                "host_id": default_host_id,
                "name": "Should Not Persist",
                "device_config": {"stable": False},
                "replace_device_config": True,
            },
        )
        assert resp.status_code == 202
        job = await _wait_for_job(
            client,
            resp.json()["job_id"],
            session_factory=session_factory,
            probe_result=(False, "Session startup failed"),
        )

    assert job["status"] == "failed"
    node = await db_session.scalar(select(AppiumNode).where(AppiumNode.device_id == device.id))
    assert node is not None
    assert node.desired_state == AppiumDesiredState.stopped
    await db_session.refresh(device)
    assert device.name == "Probe Update Device"
    assert device.device_config == {"stable": True}


async def test_failed_update_verification_does_not_strand_operator_stopped(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    """A failed update-mode verification must NOT brand the device operator-stopped:
    no ``operator:stop:*`` intents survive (AC1) and a subsequent verify-job POST is
    accepted rather than 409 (AC3). See spec bug-3 §6 R2."""
    session_factory = async_sessionmaker(db_session.bind, class_=AsyncSession, expire_on_commit=False)
    device = await create_device_record(
        db_session,
        host_id=default_host_id,
        identity_value=f"strand-{uuid.uuid4()}",
        connection_target="strand-target",
        name="Strand Device",
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        os_version="14",
        device_config={"stable": True},
    )

    with (
        _patch_running_node(active_connection_target=device.connection_target),
        patch(
            "app.verification.services.runner.httpx.AsyncClient",
            return_value=_mock_http_client(payload={"healthy": True}),
        ),
    ):
        resp = await client.post(
            f"/api/verification/devices/{device.id}/jobs",
            json={
                "host_id": default_host_id,
                "name": "Should Not Persist",
                "device_config": {"stable": False},
                "replace_device_config": True,
            },
        )
        assert resp.status_code == 202
        job = await _wait_for_job(
            client,
            resp.json()["job_id"],
            session_factory=session_factory,
            probe_result=(False, "Session startup failed"),
        )
    assert job["status"] == "failed"

    # AC1: no operator:stop:* intents remain for the device.
    stop_intents = (
        (
            await db_session.execute(
                select(DeviceIntent).where(
                    DeviceIntent.device_id == device.id,
                    DeviceIntent.source.like(f"operator:stop:%:{device.id}"),
                )
            )
        )
        .scalars()
        .all()
    )
    assert stop_intents == [], f"expected no operator:stop intents, found {[i.source for i in stop_intents]}"
    assert await operator_stop_active(db_session, device.id) is False

    # AC3: a subsequent verify-job POST is accepted, not 409.
    resp2 = await client.post(
        f"/api/verification/devices/{device.id}/jobs",
        json={"host_id": default_host_id},
    )
    assert resp2.status_code == 202


async def test_failed_then_successful_reverify_recovers_device(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    """After a failed verification the device is shelved (review_required, node stopped)
    but NOT operator-stopped, so a subsequent re-verify with a passing probe completes
    end-to-end — recovery without a DB edit (spec bug-3 §6 R5)."""
    session_factory = async_sessionmaker(db_session.bind, class_=AsyncSession, expire_on_commit=False)
    device = await create_device_record(
        db_session,
        host_id=default_host_id,
        identity_value=f"recover-{uuid.uuid4()}",
        connection_target="recover-target",
        name="Recover Device",
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        os_version="14",
        device_config={"stable": True},
    )

    with (
        _patch_running_node(active_connection_target=device.connection_target),
        patch(
            "app.verification.services.runner.httpx.AsyncClient",
            return_value=_mock_http_client(payload={"healthy": True}),
        ),
    ):
        # Leg 1: failing verification → device shelved, not operator-stopped.
        resp_fail = await client.post(
            f"/api/verification/devices/{device.id}/jobs",
            json={"host_id": default_host_id},
        )
        assert resp_fail.status_code == 202
        failed = await _wait_for_job(
            client,
            resp_fail.json()["job_id"],
            session_factory=session_factory,
            probe_result=(False, "Session startup failed"),
        )
        assert failed["status"] == "failed"

        # Leg 2: re-verify with a passing probe → accepted (not 409) and completes.
        resp_ok = await client.post(
            f"/api/verification/devices/{device.id}/jobs",
            json={"host_id": default_host_id},
        )
        assert resp_ok.status_code == 202, "re-verify must be accepted after a failed verify"
        recovered = await _wait_for_job(
            client,
            resp_ok.json()["job_id"],
            session_factory=session_factory,
            probe_result=(True, None),
        )
    assert recovered["status"] == "completed"


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

    with patch("app.verification.services.preparation.normalize_pack_device", new=AsyncMock(return_value=None)):
        resp = await client.post(
            f"/api/verification/devices/{device.id}/jobs",
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
        _patch_running_node(),
        patch.object(ReconcilerAgentService, "stop_node", new=AsyncMock()),
        patch(
            "app.verification.services.runner.httpx.AsyncClient",
            return_value=_mock_http_client(payload={"healthy": True, "ecp_reachable": {"reachable": True}}),
        ),
    ):
        resp = await client.post(
            f"/api/verification/devices/{device.id}/jobs",
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

    with (
        _patch_running_node(),
        patch.object(ReconcilerAgentService, "stop_node", new=AsyncMock()),
        patch(
            "app.verification.services.runner.httpx.AsyncClient",
            return_value=_mock_http_client(
                payload={"healthy": True, "checks": [{"check_id": "ecp_reachable", "ok": True, "message": ""}]}
            ),
        ),
    ):
        resp = await client.post(
            f"/api/verification/devices/{device.id}/jobs",
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
            pid=12345,
            desired_state=AppiumDesiredState.running,
            desired_port=4723,
            active_connection_target="",
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
        stopped_device.appium_node.pid = None
        stopped_device.appium_node.active_connection_target = None
        return stopped_device.appium_node

    async def wait_for_updated_node(
        self: VerificationExecutionService,
        node_id: uuid.UUID,
        *,
        timeout_sec: int,
    ) -> NodeEffectSnapshot:
        del timeout_sec
        events.append("start")
        async with self._session_factory.begin() as db:
            node = await db.get(AppiumNode, node_id)
            assert node is not None
            node.port = 4724
            node.pid = 67890
            node.active_connection_target = "running-verify-target"
        return NodeEffectSnapshot(node_id, "running-verify-target")

    with (
        patch(
            "app.verification.services.execution._stop_managed_node_for_verification",
            new=AsyncMock(side_effect=stop_running_node),
        ),
        patch.object(
            VerificationExecutionService,
            "wait_for_node_running",
            new=wait_for_updated_node,
        ),
        patch(
            "app.verification.services.runner.httpx.AsyncClient",
            return_value=_mock_http_client(payload={"healthy": True, "adb_connected": {"connected": True}}),
        ),
    ):
        resp = await client.post(
            f"/api/verification/devices/{device.id}/jobs",
            json={
                "host_id": default_host_id,
                "device_config": {"newCommandTimeout": 120},
                "replace_device_config": True,
            },
        )
        assert resp.status_code == 202
        job = await _wait_for_job(client, resp.json()["job_id"], session_factory=session_factory)

    assert job["status"] == "completed", job
    # The old managed node is stopped before the updated verification node starts.
    assert events[:2] == [f"stop:{device.id}", "start"]
    # The agent-normalized new config is the durable outcome of a passing verify.
    config = (await client.get(f"/api/devices/{device.id}/config")).json()
    assert config == {"newCommandTimeout": 120}


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
            "field_errors": [],
        }
    )

    with (
        patch("app.verification.services.runner.httpx.AsyncClient", return_value=http_client),
        _patch_running_node(),
        patch.object(ReconcilerAgentService, "stop_node", new=AsyncMock()),
    ):
        resp = await client.post(
            "/api/verification/jobs",
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
    with (
        patch("app.verification.services.runner.httpx.AsyncClient", return_value=http_client),
        _patch_running_node(active_connection_target="192.168.1.50"),
        patch.object(ReconcilerAgentService, "stop_node", new=AsyncMock()),
    ):
        resp = await client.post(
            "/api/verification/jobs",
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

    with patch("app.verification.services.runner.httpx.AsyncClient", return_value=http_client):
        resp = await client.post(
            "/api/verification/jobs",
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

    with patch("app.verification.services.runner.httpx.AsyncClient", return_value=http_client):
        resp = await client.post(
            "/api/verification/jobs",
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
        _patch_running_node(),
        patch.object(ReconcilerAgentService, "stop_node", new=AsyncMock()),
        patch("app.verification.services.runner.httpx.AsyncClient", return_value=healthy_http_client),
    ):
        resp = await client.post(
            "/api/verification/jobs",
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

        _viability2 = SessionViabilityService(
            publisher=_publisher_mock(),
            settings=settings_service,
            session_factory=session_factory,
            capability=DeviceCapabilityService(),
            health=AsyncMock(),
        )
        _viability2.probe_session_direct = AsyncMock(return_value=(True, None))  # type: ignore[method-assign]
        recovered = await DurableJobService(
            session_factory=session_factory,
            publisher=_publisher_mock(),
            settings=settings_service,
            circuit_breaker=_noop_circuit_breaker(),
            remediation_runner=AsyncMock(),
            verification_runner=VerificationRunnerService(
                session_factory=session_factory,
                publisher=_publisher_mock(),
                settings=settings_service,
                circuit_breaker=_noop_circuit_breaker(),
                preparation=VerificationPreparationService(
                    settings=settings_service,
                    circuit_breaker=_noop_circuit_breaker(),
                    crud=DeviceCrudService(identity=DeviceIdentityConflictService(), publisher=event_bus),
                    identity=DeviceIdentityConflictService(),
                    publisher=event_bus,
                    session_factory=session_factory,
                ),
                execution=VerificationExecutionService(
                    review=build_review_service(),
                    publisher=_publisher_mock(),
                    agent=AgentCallContext(settings=settings_service, circuit_breaker=_noop_circuit_breaker()),
                    crud=DeviceCrudService(identity=DeviceIdentityConflictService(), publisher=event_bus),
                    viability=_viability2,
                    capability=DeviceCapabilityService(),
                    reconciler=AsyncMock(),
                    session_factory=session_factory,
                    node_manager=ReconcilerAgentService(
                        settings=settings_service,
                        operator=OperatorNodeLifecycleService(
                            review=build_review_service(), settings=settings_service, publisher=event_bus
                        ),
                    ),
                ),
            ),
            recovery_runner=RecoveryJobService(
                session_factory=session_factory,
                publisher=_publisher_mock(),
                settings=settings_service,
                lifecycle_policy=AsyncMock(),
                viability=AsyncMock(),
            ),
            run_teardown_runner=AsyncMock(),
            session_kill_runner=AsyncMock(),
        ).reset_stale_running_jobs()
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
    resp = await client.post("/api/verification/jobs", json=payload)
    assert resp.status_code == 422
    body = resp.json()
    assert body["error"]["details"]["code"] == "pack_disabled"


async def test_existing_device_verification_rejected_when_session_running(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    """Re-verifying a device with a live session would tear down the node serving it
    (spec §14.1: verification never coexists with a live session, S08 = DEAD)."""
    device = await create_device_record(
        db_session,
        host_id=default_host_id,
        identity_value="busy-verify-001",
        name="Busy Verify",
        operational_state=DeviceOperationalState.busy,
    )
    db_session.add(Session(session_id="live-verify-sess", device_id=device.id, status=SessionStatus.running))
    await db_session.commit()

    resp = await client.post(f"/api/verification/devices/{device.id}/jobs", json={"host_id": default_host_id})
    assert resp.status_code == 409
    assert "session" in resp.json()["error"]["message"].lower()

    # The conflict must short-circuit before any verification job is enqueued.
    enqueued = await db_session.scalar(select(Job).where(Job.kind == JOB_KIND_DEVICE_VERIFICATION))
    assert enqueued is None


async def test_existing_device_verification_rejected_when_operator_stopped(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    """A re-verify must not silently revive a device the operator deliberately stopped:
    the verification node-start path runs through ``request_start``, which revokes
    ``operator_stop_sources`` — defeating the sticky stop and re-enabling auto-recovery
    (N13b). Refuse with 409, leaving the operator:stop intact (mirror of N13a/S17)."""
    device = await create_device_record(
        db_session,
        host_id=default_host_id,
        identity_value="opstop-verify-001",
        name="OpStop Verify",
        operational_state=DeviceOperationalState.offline,
    )
    await IntentService(db_session).register_intents_and_reconcile(
        device_id=device.id,
        intents=operator_stop_intents(device.id),
        publisher=event_bus,
    )
    await db_session.commit()
    assert await operator_stop_active(db_session, device.id) is True

    resp = await client.post(f"/api/verification/devices/{device.id}/jobs", json={"host_id": default_host_id})
    assert resp.status_code == 409
    assert "operator" in resp.json()["error"]["message"].lower()

    # The conflict short-circuits before any verification job is enqueued ...
    enqueued = await db_session.scalar(select(Job).where(Job.kind == JOB_KIND_DEVICE_VERIFICATION))
    assert enqueued is None
    # ... and the sticky operator:stop must NOT have been revoked.
    assert await operator_stop_active(db_session, device.id) is True


async def test_validate_update_request_rejects_operator_stopped(
    db_session: AsyncSession,
    default_host_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defense-in-depth: even if the operator stops the node in the enqueue→run window,
    the preparation step fails the job instead of running a verify that would revoke the
    sticky stop (N13b)."""
    monkeypatch.setattr("app.verification.services.job_state.publish", AsyncMock())
    device = await create_device_record(
        db_session,
        host_id=default_host_id,
        identity_value="opstop-prep-001",
        name="OpStop Prep",
        operational_state=DeviceOperationalState.offline,
    )
    await IntentService(db_session).register_intents_and_reconcile(
        device_id=device.id,
        intents=operator_stop_intents(device.id),
        publisher=event_bus,
    )
    await db_session.commit()

    session_factory = async_sessionmaker(db_session.bind, class_=AsyncSession, expire_on_commit=False)
    prep = VerificationPreparationService(
        settings=settings_service,
        circuit_breaker=_noop_circuit_breaker(),
        crud=DeviceCrudService(identity=DeviceIdentityConflictService(), publisher=event_bus),
        identity=DeviceIdentityConflictService(),
        publisher=event_bus,
        session_factory=session_factory,
    )
    context, error = await prep.prepare_update(
        new_job(str(uuid.uuid4())),
        uuid.uuid4(),
        device.id,
        DeviceVerificationUpdate(name="renamed", host_id=device.host_id),
        http_client_factory=httpx.AsyncClient,
    )
    assert context is None
    assert error is not None and "operator" in error.lower()
    # The backstop must not have revoked the sticky stop.
    assert await operator_stop_active(db_session, device.id) is True


async def test_validate_update_request_rejects_running_session(
    db_session: AsyncSession,
    default_host_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defense-in-depth: even if a session starts in the enqueue→run window, the
    preparation step fails the job instead of tearing down the live node."""
    monkeypatch.setattr("app.verification.services.job_state.publish", AsyncMock())
    device = await create_device_record(
        db_session,
        host_id=default_host_id,
        identity_value="busy-prep-001",
        name="Busy Prep",
        operational_state=DeviceOperationalState.busy,
    )
    db_session.add(Session(session_id="live-prep-sess", device_id=device.id, status=SessionStatus.running))
    await db_session.commit()

    session_factory = async_sessionmaker(db_session.bind, class_=AsyncSession, expire_on_commit=False)
    prep = VerificationPreparationService(
        settings=settings_service,
        circuit_breaker=_noop_circuit_breaker(),
        crud=DeviceCrudService(identity=DeviceIdentityConflictService(), publisher=event_bus),
        identity=DeviceIdentityConflictService(),
        publisher=event_bus,
        session_factory=session_factory,
    )
    context, error = await prep.prepare_update(
        new_job(str(uuid.uuid4())),
        uuid.uuid4(),
        device.id,
        DeviceVerificationUpdate(name="renamed", host_id=device.host_id),
        http_client_factory=httpx.AsyncClient,
    )
    assert context is None
    assert error is not None and "session" in error.lower()


class _TxTracker:
    def __init__(self) -> None:
        self.active = 0


class _TrackingCtx(AbstractAsyncContextManager["AsyncSession"]):
    def __init__(self, inner: AbstractAsyncContextManager[AsyncSession], tracker: _TxTracker) -> None:
        self._inner = inner
        self._tracker = tracker

    async def __aenter__(self) -> AsyncSession:
        db = await self._inner.__aenter__()
        self._tracker.active += 1
        return db

    async def __aexit__(self, *exc: object) -> bool | None:
        try:
            return await self._inner.__aexit__(*exc)
        finally:
            self._tracker.active -= 1


class _TrackingFactory:
    def __init__(self, inner: async_sessionmaker[AsyncSession], tracker: _TxTracker) -> None:
        self._inner = inner
        self._tracker = tracker

    def __call__(self) -> _TrackingCtx:
        return _TrackingCtx(self._inner(), self._tracker)

    def begin(self) -> _TrackingCtx:
        return _TrackingCtx(self._inner.begin(), self._tracker)


def _exec_with_factory(
    session_factory: object,
    *,
    viability: object | None = None,
    node_manager: object | None = None,
) -> VerificationExecutionService:
    return VerificationExecutionService(
        review=build_review_service(),
        publisher=_publisher_mock(),
        agent=AgentCallContext(settings=settings_service, circuit_breaker=_noop_circuit_breaker()),
        crud=DeviceCrudService(identity=DeviceIdentityConflictService(), publisher=event_bus),
        viability=viability if viability is not None else AsyncMock(),
        capability=DeviceCapabilityService(),
        reconciler=AsyncMock(),
        node_manager=node_manager if node_manager is not None else AsyncMock(),
        session_factory=session_factory,  # type: ignore[arg-type]
    )


def _effect_for(
    device: Device,
    operation_id: uuid.UUID,
    *,
    mode: str = "update",
    payload: dict[str, Any] | None = None,
    original_fields: dict[str, Any] | None = None,
) -> PreparedVerificationEffect:
    return PreparedVerificationEffect(
        operation_id=operation_id,
        mode=mode,  # type: ignore[arg-type]
        device_id=device.id,
        payload=payload if payload is not None else {},
        original_fields=original_fields,
        host_id=device.host_id,
        host_ip="10.0.0.20",
        host_agent_port=5100,
        pack_id=device.pack_id,
        pack_release="1.0.0",
        platform_id=device.platform_id,
        resolution_action=None,
    )


def test_stale_running_job_reset_preserves_operation_id() -> None:
    original = new_job("op-token-123")
    reset = reset_snapshot_for_retry(original)
    assert reset["operation_id"] == "op-token-123"
    assert reset["status"] == "pending"
    assert reset["device_id"] is None


async def test_agent_normalize_health_and_probe_run_without_open_transaction(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.verification.services.job_state.publish", AsyncMock())
    real = async_sessionmaker(db_session.bind, class_=AsyncSession, expire_on_commit=False)
    tracker = _TxTracker()
    tracking = _TrackingFactory(real, tracker)
    observed: list[tuple[str, int]] = []

    async def _normalize(*_a: object, **_k: object) -> None:
        observed.append(("normalize", tracker.active))

    async def _health(*_a: object, **_k: object) -> dict[str, Any]:
        observed.append(("health", tracker.active))
        return {"healthy": True}

    async def _probe(*_a: object, **_k: object) -> tuple[bool, str | None]:
        observed.append(("probe", tracker.active))
        return True, None

    monkeypatch.setattr("app.verification.services.preparation.normalize_pack_device", _normalize)
    monkeypatch.setattr("app.verification.services.execution.fetch_pack_device_health", _health)
    viability = SessionViabilityService(
        publisher=_publisher_mock(),
        settings=settings_service,
        session_factory=tracking,  # type: ignore[arg-type]
        capability=DeviceCapabilityService(),
        health=AsyncMock(),
    )
    viability.probe_session_direct = _probe  # type: ignore[method-assign]

    node_manager = ReconcilerAgentService(
        settings=settings_service,
        operator=OperatorNodeLifecycleService(
            review=build_review_service(), settings=settings_service, publisher=event_bus
        ),
    )
    prep = VerificationPreparationService(
        settings=settings_service,
        circuit_breaker=_noop_circuit_breaker(),
        crud=DeviceCrudService(identity=DeviceIdentityConflictService(), publisher=event_bus),
        identity=DeviceIdentityConflictService(),
        publisher=event_bus,
        session_factory=tracking,  # type: ignore[arg-type]
    )
    exec_svc = _exec_with_factory(tracking, viability=viability, node_manager=node_manager)

    operation_id = uuid.uuid4()
    job = new_job(str(operation_id))
    data = DeviceVerificationCreate(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value=f"notx-{operation_id}",
        connection_target=f"notx-{operation_id}",
        name="NoTx",
        os_version="14",
        host_id=uuid.UUID(default_host_id),
    )
    with _patch_running_node(active_connection_target=f"notx-{operation_id}"):
        effect, error = await prep.prepare_create(job, operation_id, data, http_client_factory=httpx.AsyncClient)
        assert error is None and effect is not None
        outcome = await exec_svc.execute_verification_effect(job, effect, http_client_factory=httpx.AsyncClient)

    assert outcome.status == "completed", outcome
    assert {name for name, _ in observed} == {"normalize", "health", "probe"}
    assert all(active == 0 for _, active in observed), f"remote work ran with an open transaction: {observed}"


async def test_crash_after_health_or_probe_reuses_same_operation_id(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.verification.services.job_state.publish", AsyncMock())
    session_factory = async_sessionmaker(db_session.bind, class_=AsyncSession, expire_on_commit=False)
    operation_id = uuid.uuid4()
    identity = f"resume-{operation_id}"
    device = await create_device_record(
        db_session,
        host_id=default_host_id,
        identity_value=identity,
        connection_target=identity,
        name="Resume",
        os_version="14",
    )
    async with session_factory.begin() as db:
        db.add(
            Job(
                id=operation_id,
                kind=JOB_KIND_DEVICE_VERIFICATION,
                status="running",
                payload={
                    "operation_id": str(operation_id),
                    "mode": "create",
                    "data": {},
                    "device_id": str(device.id),
                },
                snapshot=new_job(str(operation_id)),
                scheduled_at=datetime.now(UTC),
            )
        )

    prep = VerificationPreparationService(
        settings=settings_service,
        circuit_breaker=_noop_circuit_breaker(),
        crud=DeviceCrudService(identity=DeviceIdentityConflictService(), publisher=event_bus),
        identity=DeviceIdentityConflictService(),
        publisher=event_bus,
        session_factory=session_factory,
    )
    data = DeviceVerificationCreate(
        pack_id=device.pack_id,
        platform_id=device.platform_id,
        identity_scheme=device.identity_scheme,
        identity_scope=device.identity_scope,
        identity_value=identity,
        connection_target=identity,
        name="Resume",
        os_version="14",
        host_id=uuid.UUID(default_host_id),
    )
    with patch(
        "app.verification.services.preparation.normalize_pack_device",
        new=AsyncMock(side_effect=AssertionError("resume must not re-normalize / re-create the device")),
    ):
        effect, error = await prep.prepare_create(
            new_job(str(operation_id)), operation_id, data, http_client_factory=httpx.AsyncClient
        )

    assert error is None and effect is not None
    assert effect.operation_id == operation_id
    assert effect.device_id == device.id
    async with session_factory() as db:
        rows = (await db.execute(select(Device).where(Device.identity_value == identity))).scalars().all()
    assert len(rows) == 1, "resume reused the operation's device instead of creating a duplicate"


async def test_prepare_create_commits_device_and_lease_atomically(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.verification.services.job_state.publish", AsyncMock())
    session_factory = async_sessionmaker(db_session.bind, class_=AsyncSession, expire_on_commit=False)
    operation_id = uuid.uuid4()
    identity = f"atomic-{operation_id}"
    async with session_factory.begin() as db:
        db.add(
            Job(
                id=operation_id,
                kind=JOB_KIND_DEVICE_VERIFICATION,
                status="running",
                payload={"operation_id": str(operation_id), "mode": "create", "data": {}},
                snapshot=new_job(str(operation_id)),
                scheduled_at=datetime.now(UTC),
            )
        )

    prep = VerificationPreparationService(
        settings=settings_service,
        circuit_breaker=_noop_circuit_breaker(),
        crud=DeviceCrudService(identity=DeviceIdentityConflictService(), publisher=event_bus),
        identity=DeviceIdentityConflictService(),
        publisher=event_bus,
        session_factory=session_factory,
    )
    data = DeviceVerificationCreate(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value=identity,
        connection_target=identity,
        name="Atomic",
        os_version="14",
        host_id=uuid.UUID(default_host_id),
    )

    # Normalize is a remote agent call; stub it to pass the input payload straight through.
    async def _passthrough_normalize(
        payload: dict[str, Any],
        coords: _PackCoords,
        *,
        host_ip: str,
        host_agent_port: int,
        http_client_factory: AgentClientFactory,
    ) -> tuple[dict[str, Any], None]:
        return payload, None

    monkeypatch.setattr(prep, "normalize_effect", _passthrough_normalize)
    monkeypatch.setattr(prep, "_write_verification_lease", AsyncMock(side_effect=RuntimeError("boom")))

    with pytest.raises(RuntimeError, match="boom"):
        await prep.prepare_create(new_job(str(operation_id)), operation_id, data, http_client_factory=httpx.AsyncClient)

    async with session_factory() as db:
        rows = (await db.execute(select(Device).where(Device.identity_value == identity))).scalars().all()
        job_row = await db.get(Job, operation_id)
    assert rows == [], "device insert must roll back with the failed lease write (atomic)"
    assert job_row is not None and "device_id" not in job_row.payload, "device_id must not be stamped on rollback"


async def test_prepare_create_translates_concurrent_identity_integrity_error(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.verification.services.job_state.publish", AsyncMock())
    session_factory = async_sessionmaker(db_session.bind, class_=AsyncSession, expire_on_commit=False)
    operation_id = uuid.uuid4()
    # A transport-shaped (IP-literal) identity skips preparation.py's pre-insert
    # gate (only non-transport identities are checked there), so this exercises
    # the post-insert IntegrityError re-check instead of the earlier gate.
    identity = ":".join(operation_id.hex[i : i + 4] for i in range(0, 32, 4))
    # A peer already owns this identity — the re-check must find it.
    await create_device_record(
        db_session,
        host_id=default_host_id,
        identity_value=identity,
        connection_target=identity,
        name="Peer",
        os_version="14",
    )
    async with session_factory.begin() as db:
        db.add(
            Job(
                id=operation_id,
                kind=JOB_KIND_DEVICE_VERIFICATION,
                status="running",
                payload={"operation_id": str(operation_id), "mode": "create", "data": {}},
                snapshot=new_job(str(operation_id)),
                scheduled_at=datetime.now(UTC),
            )
        )

    prep = VerificationPreparationService(
        settings=settings_service,
        circuit_breaker=_noop_circuit_breaker(),
        crud=DeviceCrudService(identity=DeviceIdentityConflictService(), publisher=event_bus),
        identity=DeviceIdentityConflictService(),
        publisher=event_bus,
        session_factory=session_factory,
    )
    data = DeviceVerificationCreate(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value=identity,
        connection_target=identity,
        name="Racer",
        os_version="14",
        host_id=uuid.UUID(default_host_id),
    )

    async def _passthrough_normalize(
        payload: dict[str, Any],
        coords: _PackCoords,
        *,
        host_ip: str,
        host_agent_port: int,
        http_client_factory: AgentClientFactory,
    ) -> tuple[dict[str, Any], None]:
        return payload, None

    monkeypatch.setattr(prep, "normalize_effect", _passthrough_normalize)
    # Simulate the DB rejecting the duplicate at flush (past the pre-insert gate).
    monkeypatch.setattr(
        prep._crud, "create_device", AsyncMock(side_effect=IntegrityError("stmt", {}, Exception("dup")))
    )

    effect, error = await prep.prepare_create(
        new_job(str(operation_id)), operation_id, data, http_client_factory=httpx.AsyncClient
    )
    assert effect is None
    assert error is not None and "already" in error.lower(), f"expected a friendly conflict, got {error!r}"


async def test_old_finalizer_after_new_verification_is_superseded(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.verification.services.execution.set_stage", AsyncMock())
    session_factory = async_sessionmaker(db_session.bind, class_=AsyncSession, expire_on_commit=False)
    device = await create_device_record(
        db_session,
        host_id=default_host_id,
        identity_value=f"superseded-{uuid.uuid4()}",
        connection_target="superseded-target",
        name="Superseded",
        os_version="14",
        verified=False,
    )
    op_a = uuid.uuid4()
    op_b = uuid.uuid4()
    # Job B overwrote the lease token under the device lock.
    async with session_factory.begin() as db:
        locked = await db.get(Device, device.id)
        assert locked is not None
        await register_verification_node_intent(
            db, locked, settings=settings_service, publisher=event_bus, operation_id=op_b
        )

    svc = _exec_with_factory(session_factory)
    effect_a = _effect_for(
        device, op_a, mode="update", payload={"name": "stale"}, original_fields={"name": device.name}
    )

    success = await svc._finalize_success(effect_a, job=new_job(str(op_a)), node_id=None)
    assert success.superseded is True

    failure = await svc._finalize_failure(effect_a, error="stale", job=new_job(str(op_a)), node_id=None)
    assert failure.superseded is True

    async with session_factory() as db:
        refreshed = await db.get(Device, device.id)
        assert refreshed is not None
        assert refreshed.verified_at is None, "superseded success must not verify B's device"
        assert refreshed.review_required is False, "superseded failure must not shelve B's device"
        assert refreshed.name == "Superseded", "superseded finalizers must not overwrite B's fields"
        lease = (
            await db.execute(
                select(DeviceIntent).where(
                    DeviceIntent.device_id == device.id,
                    DeviceIntent.source == verification_intent_source(device.id),
                )
            )
        ).scalar_one()
        assert lease.payload[VERIFICATION_OPERATION_ID_KEY] == str(op_b), "B's lease token must be untouched"


async def test_session_start_between_prepare_and_finalize_blocks_stale_save(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.verification.services.execution.set_stage", AsyncMock())
    session_factory = async_sessionmaker(db_session.bind, class_=AsyncSession, expire_on_commit=False)
    device = await create_device_record(
        db_session,
        host_id=default_host_id,
        identity_value=f"racesess-{uuid.uuid4()}",
        connection_target="racesess-target",
        name="Race Session",
        os_version="14",
    )
    operation_id = uuid.uuid4()
    async with session_factory.begin() as db:
        locked = await db.get(Device, device.id)
        assert locked is not None
        await register_verification_node_intent(
            db, locked, settings=settings_service, publisher=event_bus, operation_id=operation_id
        )
        db.add(Session(session_id="race-live-sess", device_id=device.id, status=SessionStatus.running))

    svc = _exec_with_factory(session_factory)
    effect = _effect_for(
        device, operation_id, mode="update", payload={"name": "renamed"}, original_fields={"name": device.name}
    )
    outcome = await svc._finalize_success(effect, job=new_job(str(operation_id)), node_id=None)

    assert outcome.status == "failed"
    assert outcome.superseded is False
    async with session_factory() as db:
        refreshed = await db.get(Device, device.id)
        assert refreshed is not None
        assert refreshed.name == "Race Session", "a session appearing after prepare must block the destructive save"
        live = (await db.execute(select(Session).where(Session.device_id == device.id))).scalar_one()
        assert live.status == SessionStatus.running, "the client session must not be overwritten"
