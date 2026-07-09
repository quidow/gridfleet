import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from httpx2 import AsyncClient, HTTPStatusError, Request, Response

from app.agent_comm.error_codes import AgentErrorCode
from app.appium_nodes.models import AppiumDesiredState, AppiumNode
from app.devices import locking as device_locking
from app.devices.models import ConnectionType, Device, DeviceOperationalState, DeviceType
from app.devices.services.intent import IntentService
from app.devices.services.intent_types import RECOVERY, IntentRegistration
from app.devices.services.lifecycle_policy_state import (
    clear_operator_start_suppression,
    clear_stale_escalation_residue,
    set_maintenance_reason,
)
from app.devices.services.lifecycle_policy_state import (
    write_state as write_lifecycle_policy_state,
)
from app.hosts.models import Host, HostStatus
from tests.helpers import create_device_record, create_host
from tests.packs.factories import seed_test_packs

if TYPE_CHECKING:
    from collections.abc import Generator

    from sqlalchemy.ext.asyncio import AsyncSession

DEVICE_PAYLOAD = {
    "identity_value": "emulator-5554",
    "connection_target": "emulator-5554",
    "name": "Pixel 7 Emulator",
    "pack_id": "appium-uiautomator2",
    "platform_id": "android_mobile",
    "identity_scheme": "android_serial",
    "identity_scope": "host",
    "os_version": "14",
}
PORT_CONFLICT_DETAIL = (
    "Port 4723 is already in use by another Appium listener; "
    "stop the existing process before starting a new managed node"
)
HOST_PAYLOAD = {
    "hostname": "nodes-host",
    "ip": "10.0.0.40",
    "os_type": "linux",
    "agent_port": 5100,
}


@pytest_asyncio.fixture(autouse=True)
async def seed_packs(db_session: AsyncSession) -> None:
    """Seed driver packs so the assert_runnable gate passes in all tests."""
    await seed_test_packs(db_session)
    await db_session.commit()


@pytest.fixture(autouse=True)
def _stub_node_poke(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the agent wake-hint poke so node-control/run-creation tests here
    don't make a real network call. ``converge_device_now`` (restart route)
    binds ``agent_nodes_refresh`` directly into the reconciler module, while
    run creation reaches it via ``node_poke``'s ``agent_operations`` module
    attribute — both call sites need patching independently."""
    monkeypatch.setattr("app.appium_nodes.services.reconciler.agent_nodes_refresh", AsyncMock())
    monkeypatch.setattr("app.agent_comm.operations.agent_nodes_refresh", AsyncMock())


@pytest_asyncio.fixture
async def default_host_id(client: AsyncClient) -> str:
    host = await create_host(client, **HOST_PAYLOAD)
    return str(host["id"])


async def _create_device(db_session: AsyncSession, host_id: str, **overrides: object) -> dict[str, Any]:
    payload = {
        **DEVICE_PAYLOAD,
        "identity_value": f"{DEVICE_PAYLOAD['identity_value']}-{uuid.uuid4().hex[:8]}",
        "connection_target": f"{DEVICE_PAYLOAD['connection_target']}-{uuid.uuid4().hex[:8]}",
        "name": f"{DEVICE_PAYLOAD['name']} {uuid.uuid4().hex[:4]}",
        "host_id": host_id,
        **overrides,
    }
    device = await create_device_record(
        db_session,
        host_id=host_id,
        identity_value=payload["identity_value"],
        connection_target=payload["connection_target"],
        name=payload["name"],
        pack_id=payload["pack_id"],
        platform_id=payload["platform_id"],
        identity_scheme=payload["identity_scheme"],
        identity_scope=payload["identity_scope"],
        os_version=payload["os_version"],
        operational_state=payload.get("operational_state", "offline"),
        device_type=payload.get("device_type", "real_device"),
        connection_type=payload.get("connection_type"),
        ip_address=payload.get("ip_address"),
        verified=payload.get("verified", True),
    )
    return {"id": str(device.id)}


@pytest.fixture
def remote_manager_client() -> Generator[AsyncMock]:
    """Historical push-path scaffolding.

    Node start/stop/restart routes are desired-state writes only — no agent
    HTTP call happens synchronously in-request — so this mock is not wired
    into any live code path. It is kept (unpatched) purely so existing tests
    can still set ``.return_value``/``.side_effect`` on it without error;
    those assignments are inert.
    """
    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get.return_value = _mock_agent_response(
        {"running": True, "port": 4723, "appium_status": {"value": {"ready": True}}}
    )
    yield mock_client


def _mock_agent_response(json_data: dict[str, Any], status_code: int = 200) -> MagicMock:
    mock_resp = MagicMock()
    mock_resp.status_code = status_code
    mock_resp.json.return_value = json_data
    mock_resp.raise_for_status = MagicMock()
    return mock_resp


def _mock_agent_http_error(detail: str, *, status_code: int = 400) -> MagicMock:
    payload = {
        "detail": {
            "code": AgentErrorCode.PORT_OCCUPIED.value,
            "message": detail,
        }
    }
    response = _mock_agent_response(payload, status_code=status_code)
    response.raise_for_status.side_effect = HTTPStatusError(
        f"{status_code} Server Error",
        request=Request("POST", "http://10.0.0.40:5100/agent/appium/start"),
        response=Response(status_code, json=payload),
    )
    return response


async def test_start_node(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
    remote_manager_client: AsyncMock,
) -> None:
    device = await _create_device(db_session, default_host_id, operational_state="available")
    device_id = device["id"]
    remote_manager_client.post.return_value = _mock_agent_response(
        {"pid": 12345, "port": 4723, "connection_target": "emulator-5554"}
    )

    resp = await client.post(f"/api/devices/{device_id}/node/start")
    assert resp.status_code == 200
    data = resp.json()
    assert data["desired_state"] == AppiumDesiredState.running.value
    assert data["pid"] is None
    assert data["port"] == 4723
    assert data["active_connection_target"] is None

    device_resp = await client.get(f"/api/devices/{device_id}")
    # After Task 10: reconciler derives offline while node is starting (pid not yet set).
    assert device_resp.json()["operational_state"] in (
        DeviceOperationalState.available.value,
        DeviceOperationalState.offline.value,
    )


async def test_start_node_already_running(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
    remote_manager_client: AsyncMock,
) -> None:
    # A genuinely observed-running node (pid + active_connection_target set) is
    # rejected with 409 — start must not restart a live node out from under a
    # session. (A node that is desired-running but DOWN is recovered instead; see
    # test_start_node_recovers_down_but_desired_running_node.)
    device = await _create_device(db_session, default_host_id)
    device_id = device["id"]
    db_session.add(
        AppiumNode(
            device_id=uuid.UUID(device_id),
            port=4723,
            pid=12345,
            active_connection_target="emulator-5554",
            desired_state=AppiumDesiredState.running,
            desired_port=4723,
        )
    )
    await db_session.commit()

    resp = await client.post(f"/api/devices/{device_id}/node/start")
    assert resp.status_code == 409
    assert "already running" in resp.json()["error"]["message"]
    assert remote_manager_client.post.await_count == 0


async def test_start_node_recovers_down_but_desired_running_node(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
    remote_manager_client: AsyncMock,
) -> None:
    # A node that is desired-running but down (pid=None, e.g. after a crash) used
    # to make /node/start a dead lever: it 400'd "already desired-running" and the
    # operator had to know to use /node/restart instead (F2). Start now recovers it
    # via the restart path (re-spawn + immediate convergence kick).
    device = await _create_device(db_session, default_host_id, operational_state="offline")
    device_id = device["id"]
    host = await db_session.get(Host, uuid.UUID(default_host_id))
    assert host is not None
    host.status = HostStatus.online
    db_session.add(
        AppiumNode(
            device_id=uuid.UUID(device_id),
            port=4723,
            pid=None,
            active_connection_target=None,
            desired_state=AppiumDesiredState.running,
            desired_port=4723,
        )
    )
    await db_session.commit()
    remote_manager_client.post.return_value = _mock_agent_response(
        {"pid": 12345, "port": 4723, "connection_target": "emulator-5554"}
    )

    resp = await client.post(f"/api/devices/{device_id}/node/start")
    # Was a dead-lever 400 "already desired-running"; now routes through the restart
    # recovery path and returns the node still desired-running (re-spawn converges).
    assert resp.status_code == 200
    assert resp.json()["desired_state"] == AppiumDesiredState.running.value


async def test_start_node_device_not_found(client: AsyncClient) -> None:
    resp = await client.post("/api/devices/00000000-0000-0000-0000-000000000000/node/start")
    assert resp.status_code == 404


async def test_stop_node(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
    remote_manager_client: AsyncMock,
) -> None:
    device = await _create_device(db_session, default_host_id)
    device_id = device["id"]
    db_session.add(
        AppiumNode(
            device_id=uuid.UUID(device_id),
            port=4723,
            pid=12345,
            desired_state=AppiumDesiredState.running,
            desired_port=4723,
            active_connection_target="emulator-5554",
        )
    )
    await db_session.commit()

    resp = await client.post(f"/api/devices/{device_id}/node/stop")
    assert resp.status_code == 200
    data = resp.json()
    assert data["effective_state"] == "stopping"
    assert data["desired_state"] == AppiumDesiredState.stopped.value
    assert data["pid"] == 12345
    assert data["active_connection_target"] == "emulator-5554"

    device_resp = await client.get(f"/api/devices/{device_id}")
    assert device_resp.json()["operational_state"] == DeviceOperationalState.offline.value


async def test_stop_node_not_running(client: AsyncClient, db_session: AsyncSession, default_host_id: str) -> None:
    device = await _create_device(db_session, default_host_id)
    device_id = device["id"]

    resp = await client.post(f"/api/devices/{device_id}/node/stop")
    assert resp.status_code == 400
    assert "No running node" in resp.json()["error"]["message"]


async def test_restart_node(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
    remote_manager_client: AsyncMock,
) -> None:
    device = await _create_device(db_session, default_host_id)
    device_id = device["id"]
    db_session.add(
        AppiumNode(
            device_id=uuid.UUID(device_id),
            port=4723,
            pid=12345,
            active_connection_target="",
            desired_state=AppiumDesiredState.running,
            desired_port=4723,
        )
    )
    await db_session.commit()
    resp = await client.post(f"/api/devices/{device_id}/node/restart")
    assert resp.status_code == 200
    data = resp.json()
    assert data["desired_state"] == AppiumDesiredState.running.value
    assert data["restart_requested_at"] is not None


async def test_restart_node_cold_start(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
    remote_manager_client: AsyncMock,
) -> None:
    """Restart on a device with no running node should just start it."""
    device = await _create_device(db_session, default_host_id)
    device_id = device["id"]
    remote_manager_client.post.return_value = _mock_agent_response(
        {"pid": 12345, "port": 4723, "connection_target": "emulator-5554"}
    )

    resp = await client.post(f"/api/devices/{device_id}/node/restart")
    assert resp.status_code == 200
    assert resp.json()["desired_state"] == AppiumDesiredState.running.value


async def test_restart_node_clears_stale_recovery_suppression(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
    remote_manager_client: AsyncMock,
) -> None:
    """A successful manual restart must clear stale lifecycle suppression so the
    device leaves the "Recovery paused — admin review needed" state without
    waiting for the next auto-recovery tick (which never runs while the node
    is healthy)."""
    device = await _create_device(db_session, default_host_id)
    device_id = device["id"]
    remote_manager_client.post.side_effect = [
        _mock_agent_response({"pid": 12345, "port": 4723, "connection_target": "emulator-5554"}),
        _mock_agent_response({"stopped": True, "port": 4723}),
        _mock_agent_response({"pid": 12346, "port": 4723, "connection_target": "emulator-5554"}),
    ]

    db_session.add(
        AppiumNode(
            device_id=uuid.UUID(device_id),
            port=4723,
            pid=12345,
            active_connection_target="",
            desired_state=AppiumDesiredState.running,
            desired_port=4723,
        )
    )
    await db_session.commit()

    locked = await device_locking.lock_device(db_session, uuid.UUID(device_id))
    write_lifecycle_policy_state(
        locked,
        {
            "last_failure_source": "device_checks",
            "last_failure_reason": "Agent failed to start node: Appium already running for target",
            "last_action": "recovery_failed",
            "last_action_at": "2026-05-10T18:00:00+00:00",
            "stop_pending": False,
            "stop_pending_reason": None,
            "stop_pending_since": None,
            "recovery_suppressed_reason": "Node restart failed",
            "backoff_until": None,
            "recovery_backoff_attempts": 0,
        },
    )
    await db_session.commit()

    resp = await client.post(f"/api/devices/{device_id}/node/restart")
    assert resp.status_code == 200
    assert resp.json()["restart_requested_at"] is not None

    # recovery_suppressed_reason is no longer stored — the "Recovery Paused" badge
    # is projected from live facts. With no review flag, backoff, or operator deny,
    # the restarted device derives a clear (non-suppressed) lifecycle state.
    detail = await client.get(f"/api/devices/{device_id}")
    assert detail.json()["lifecycle_policy_summary"]["state"] != "suppressed"


async def test_start_node_clears_operator_stop_suppression(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
    remote_manager_client: AsyncMock,
) -> None:
    """An explicit operator start must clear the ``recovery_suppressed_reason``
    residue left behind by a prior operator stop.

    Operator stop registers a sticky RECOVERY-axis deny intent; the recovery
    loop then records ``recovery_suppressed_reason="Operator stopped the node"``
    onto ``lifecycle_policy_state``. The start path revokes the deny intent but
    must also clear the JSON residue, otherwise the device keeps deriving
    ``lifecycle_policy_summary.state == "suppressed"`` ("Recovery Paused" badge)
    while it is actually running and available.
    """
    device = await _create_device(db_session, default_host_id, operational_state="available")
    device_id = device["id"]
    remote_manager_client.post.return_value = _mock_agent_response(
        {"pid": 12345, "port": 4723, "connection_target": "emulator-5554"}
    )

    locked = await device_locking.lock_device(db_session, uuid.UUID(device_id))
    write_lifecycle_policy_state(
        locked,
        {
            "last_failure_source": "node_health",
            "last_failure_reason": "Node health checks recovered",
            "last_action": "recovery_suppressed",
            "last_action_at": "2026-05-10T18:00:00+00:00",
            "stop_pending": False,
            "stop_pending_reason": None,
            "stop_pending_since": None,
            "recovery_suppressed_reason": "Operator stopped the node",
            "backoff_until": None,
            "recovery_backoff_attempts": 0,
        },
    )
    # The badge is projected from the sticky operator deny intent (the fact an
    # operator stop leaves behind), not from the JSON residue above.
    await IntentService(db_session).register_intents(
        device_id=uuid.UUID(device_id),
        intents=[
            IntentRegistration(
                source=f"operator:stop:recovery:{device_id}",
                axis=RECOVERY,
                payload={"allowed": False, "reason": "Operator stopped the node"},
            )
        ],
    )
    await db_session.commit()

    # Sanity: the deny intent derives a suppressed summary before the start. It no
    # longer drives needs_attention — attention follows the operational axis,
    # so suppression on an available device is not flagged.
    before = await client.get(f"/api/devices/{device_id}")
    assert before.json()["lifecycle_policy_summary"]["state"] == "suppressed"
    assert before.json()["needs_attention"] is False

    resp = await client.post(f"/api/devices/{device_id}/node/start")
    assert resp.status_code == 200

    await db_session.refresh(locked)
    assert locked.lifecycle_policy_state["last_action"] == "operator_started"

    after = await client.get(f"/api/devices/{device_id}")
    # Lifecycle no longer derives "suppressed", so it no longer drives
    # needs_attention. (Any residual attention here is the transient
    # node-still-starting health signal — pid is not yet observed in this
    # harness — not the suppression residue this fix targets.)
    assert after.json()["lifecycle_policy_summary"]["state"] != "suppressed"


async def test_clear_operator_start_suppression_noop_on_clean_state(
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    """A device with no backoff/attempt/failure residue emits no action churn."""
    device = await _create_device(db_session, default_host_id)
    locked = await device_locking.lock_device(db_session, uuid.UUID(device["id"]))
    write_lifecycle_policy_state(locked, {**locked.lifecycle_policy_state, "last_action": "sentinel"})
    clear_operator_start_suppression(locked)
    assert locked.lifecycle_policy_state["last_action"] == "sentinel"


async def test_clear_stale_escalation_residue_noop_on_clean_state(
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    """A device with no backoff/attempt/failure residue emits no action churn."""
    device = await _create_device(db_session, default_host_id)
    locked = await device_locking.lock_device(db_session, uuid.UUID(device["id"]))
    write_lifecycle_policy_state(locked, {**locked.lifecycle_policy_state, "last_action": "sentinel"})
    assert clear_stale_escalation_residue(locked, min_age_seconds=120.0) is False
    assert locked.lifecycle_policy_state["last_action"] == "sentinel"


async def test_clear_stale_escalation_residue_skips_fresh_residue(
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    """Residue recorded just now (in-flight failure sequence) must NOT be cleared
    by a racing healthy connectivity tick (regression S10)."""
    device = await _create_device(db_session, default_host_id)
    locked = await device_locking.lock_device(db_session, uuid.UUID(device["id"]))
    write_lifecycle_policy_state(
        locked,
        {
            **locked.lifecycle_policy_state,
            "last_failure_reason": "Recovery probe failed",
            "last_action": "recovery_failed",
            "last_action_at": datetime.now(UTC).isoformat(),
        },
    )
    assert clear_stale_escalation_residue(locked, min_age_seconds=120.0) is False
    assert locked.lifecycle_policy_state["last_failure_reason"] == "Recovery probe failed"


async def test_clear_stale_escalation_residue_clears_aged_residue(
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    """Residue older than the staleness threshold (hours-old leftover after a
    natural reconverge) is cleared and stamps ``self_healed``."""
    device = await _create_device(db_session, default_host_id)
    locked = await device_locking.lock_device(db_session, uuid.UUID(device["id"]))
    write_lifecycle_policy_state(
        locked,
        {
            **locked.lifecycle_policy_state,
            "last_failure_reason": "Recovery probe failed",
            "last_action": "recovery_failed",
            "last_action_at": (datetime.now(UTC) - timedelta(seconds=3600)).isoformat(),
        },
    )
    assert clear_stale_escalation_residue(locked, min_age_seconds=120.0) is True
    assert locked.lifecycle_policy_state["last_failure_reason"] is None
    assert locked.lifecycle_policy_state["last_action"] == "self_healed"


async def test_clear_stale_escalation_residue_skips_residue_without_timestamp(
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    """Residue with no parseable ``last_action_at`` is treated as not-yet-stale so
    an in-flight sequence is never cleared on an untrusted timestamp."""
    device = await _create_device(db_session, default_host_id)
    locked = await device_locking.lock_device(db_session, uuid.UUID(device["id"]))
    write_lifecycle_policy_state(
        locked,
        {
            **locked.lifecycle_policy_state,
            "last_failure_reason": "Recovery probe failed",
            "last_action": "recovery_failed",
            "last_action_at": None,
        },
    )
    assert clear_stale_escalation_residue(locked, min_age_seconds=120.0) is False
    assert locked.lifecycle_policy_state["last_failure_reason"] == "Recovery probe failed"


async def test_port_allocation_increments(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
    remote_manager_client: AsyncMock,
) -> None:
    """Starting nodes for two devices should allocate different ports."""
    d1 = await _create_device(db_session, default_host_id)
    second = await create_device_record(
        db_session,
        host_id=default_host_id,
        identity_value="emulator-5556",
        connection_target="emulator-5556",
        name="Pixel 8",
        pack_id=DEVICE_PAYLOAD["pack_id"],
        platform_id=DEVICE_PAYLOAD["platform_id"],
        identity_scheme=DEVICE_PAYLOAD["identity_scheme"],
        identity_scope=DEVICE_PAYLOAD["identity_scope"],
        os_version=DEVICE_PAYLOAD["os_version"],
    )
    d2 = {"id": str(second.id)}
    remote_manager_client.post.side_effect = [
        _mock_agent_response({"pid": 12345, "port": 4723, "connection_target": "emulator-5554"}),
        _mock_agent_response({"pid": 12346, "port": 4724, "connection_target": "emulator-5556"}),
    ]

    await client.post(f"/api/devices/{d1['id']}/node/start")
    await client.post(f"/api/devices/{d2['id']}/node/start")

    r1 = await client.get(f"/api/devices/{d1['id']}")
    r2 = await client.get(f"/api/devices/{d2['id']}")

    assert r1.json()["appium_node"]["port"] == 4723
    assert r2.json()["appium_node"]["port"] == 4724
    assert r1.json()["appium_node"]["desired_port"] == 4723
    assert r2.json()["appium_node"]["desired_port"] == 4724


async def test_start_node_agent_failure(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
    remote_manager_client: AsyncMock,
) -> None:
    response = _mock_agent_response({"detail": "startup failed"}, status_code=500)
    response.raise_for_status.side_effect = HTTPStatusError(
        "500 Server Error",
        request=Request("POST", "http://10.0.0.40:5100/appium/start"),
        response=Response(500, json={"detail": "startup failed"}),
    )
    remote_manager_client.post.return_value = response

    device = await _create_device(db_session, default_host_id)
    resp = await client.post(f"/api/devices/{device['id']}/node/start")
    assert resp.status_code == 200
    assert resp.json()["desired_state"] == AppiumDesiredState.running.value


async def test_start_node_fails_when_appium_is_not_reachable_after_agent_start(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
    remote_manager_client: AsyncMock,
) -> None:
    device = await _create_device(db_session, default_host_id, operational_state="available")
    remote_manager_client.post.side_effect = [
        _mock_agent_response({"pid": 12345, "port": 4723, "connection_target": "emulator-5554"}),
        _mock_agent_response({"stopped": True, "port": 4723}),
    ]
    remote_manager_client.get.return_value = _mock_agent_response({"running": False, "port": 4723})

    resp = await client.post(f"/api/devices/{device['id']}/node/start")
    assert resp.status_code == 200

    detail = await client.get(f"/api/devices/{device['id']}")
    assert detail.status_code == 200
    node = detail.json()["appium_node"]
    assert node["desired_state"] == "running"
    assert node["desired_port"] == 4723


async def test_start_node_retries_next_port_when_agent_reports_port_conflict(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
    remote_manager_client: AsyncMock,
) -> None:
    device = await _create_device(db_session, default_host_id, operational_state="available")
    remote_manager_client.post.side_effect = [
        _mock_agent_http_error(PORT_CONFLICT_DETAIL),
        _mock_agent_response({"pid": 12345, "port": 4724, "connection_target": "emulator-5554"}),
    ]

    resp = await client.post(f"/api/devices/{device['id']}/node/start")
    assert resp.status_code == 200
    assert resp.json()["port"] == 4723

    detail = await client.get(f"/api/devices/{device['id']}")
    assert detail.status_code == 200
    assert detail.json()["appium_node"]["desired_port"] == 4723


async def test_restart_node_retries_next_port_when_preferred_port_conflicts(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
    remote_manager_client: AsyncMock,
) -> None:
    device = await _create_device(db_session, default_host_id, operational_state="available")
    remote_manager_client.post.side_effect = [
        _mock_agent_response({"pid": 12345, "port": 4723, "connection_target": "emulator-5554"}),
        _mock_agent_response({"stopped": True, "port": 4723}),
        _mock_agent_http_error(PORT_CONFLICT_DETAIL),
        _mock_agent_response({"pid": 12346, "port": 4724, "connection_target": "emulator-5554"}),
    ]

    db_session.add(
        AppiumNode(
            device_id=uuid.UUID(device["id"]),
            port=4723,
            pid=12345,
            active_connection_target="",
            desired_state=AppiumDesiredState.running,
            desired_port=4723,
        )
    )
    await db_session.commit()

    resp = await client.post(f"/api/devices/{device['id']}/node/restart")
    assert resp.status_code == 200
    assert resp.json()["port"] == 4723
    assert resp.json()["restart_requested_at"] is not None

    detail = await client.get(f"/api/devices/{device['id']}")
    assert detail.status_code == 200
    assert detail.json()["appium_node"]["desired_port"] == 4723


async def test_reserved_device_blocks_node_controls(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
    remote_manager_client: AsyncMock,
) -> None:
    device = await _create_device(db_session, default_host_id, operational_state="available")
    remote_manager_client.post.return_value = _mock_agent_response(
        {"pid": 12345, "port": 4723, "connection_target": "emulator-5554"}
    )
    run_resp = await client.post(
        "/api/runs",
        json={
            "name": "Reserved Run",
            "requirements": [{"pack_id": "appium-uiautomator2", "platform_id": "android_mobile", "count": 1}],
        },
    )
    assert run_resp.status_code == 201

    for action in ("start", "stop", "restart"):
        resp = await client.post(f"/api/devices/{device['id']}/node/{action}")
        assert resp.status_code == 409
        assert "Reserved Run" in resp.json()["error"]["message"]


async def test_maintenance_blocks_start_and_restart_but_not_stop(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
    remote_manager_client: AsyncMock,
) -> None:
    device = await _create_device(db_session, default_host_id)
    device_id = device["id"]
    db_session.add(
        AppiumNode(
            device_id=uuid.UUID(device_id),
            port=4723,
            pid=12345,
            active_connection_target="",
            desired_state=AppiumDesiredState.running,
            desired_port=4723,
        )
    )
    await db_session.commit()

    # Stop the node explicitly while it is observed running. Maintenance is not
    # entered yet, so this proves the stop endpoint is not blocked by
    # maintenance state.
    stop_resp = await client.post(f"/api/devices/{device_id}/node/stop")
    assert stop_resp.status_code == 200
    assert stop_resp.json()["effective_state"] == "stopping"
    assert stop_resp.json()["desired_state"] == AppiumDesiredState.stopped.value

    # Simulate the reconciler observing the stop before entering maintenance.
    node = await db_session.get(AppiumNode, uuid.UUID(stop_resp.json()["id"]))
    assert node is not None
    node.pid = None
    node.active_connection_target = None
    node.pid = None
    await db_session.commit()

    maintenance_resp = await client.post(f"/api/devices/{device_id}/maintenance", json={})
    assert maintenance_resp.status_code == 200
    # hold is now derived by the reconciler (Task 7+8); just verify the call succeeds

    for action in ("start", "restart"):
        resp = await client.post(f"/api/devices/{device_id}/node/{action}")
        assert resp.status_code == 409
        assert "maintenance" in resp.json()["error"]["message"]


async def test_maintenance_signal_without_hold_blocks_start_and_restart(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
    remote_manager_client: AsyncMock,
) -> None:
    """Signal-only maintenance (hold=NULL, maintenance_reason set) must block start/restart."""
    device = await _create_device(db_session, default_host_id)
    device_id = device["id"]

    # Set maintenance_reason via signal without touching the hold column.
    locked = await device_locking.lock_device(db_session, uuid.UUID(device_id))
    set_maintenance_reason(locked, "signal-only test")
    await db_session.commit()

    for action in ("start", "restart"):
        resp = await client.post(f"/api/devices/{device_id}/node/{action}")
        assert resp.status_code == 409
        assert "maintenance" in resp.json()["error"]["message"]


async def test_unverified_device_blocks_node_start(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    device = Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value=f"unverified-{uuid.uuid4()}",
        connection_target=f"unverified-{uuid.uuid4()}",
        name="Needs Verification",
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

    resp = await client.post(f"/api/devices/{device.id}/node/start")
    assert resp.status_code == 409
    assert "verification succeeds" in resp.json()["error"]["message"]


async def test_readiness_downgrade_blocks_node_start(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    device = await _create_device(
        db_session,
        default_host_id,
        identity_value="android-network-stable",
        connection_target="192.168.1.10:5555",
        device_type="real_device",
        connection_type="network",
        ip_address="192.168.1.10",
    )

    patch_resp = await client.patch(
        f"/api/devices/{device['id']}",
        json={
            "connection_target": "192.168.1.20:5555",
            "ip_address": "192.168.1.20",
        },
    )
    assert patch_resp.status_code == 200
    assert patch_resp.json()["verified_at"] is None

    start_resp = await client.post(f"/api/devices/{device['id']}/node/start")
    assert start_resp.status_code == 409
    assert "verification succeeds" in start_resp.json()["error"]["message"]
