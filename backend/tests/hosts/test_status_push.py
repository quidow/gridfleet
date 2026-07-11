from __future__ import annotations

import uuid
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

from sqlalchemy import select

from app.core.leader import state_store as control_plane_state_store
from app.hosts.models import Host, HostStatus, OSType
from app.hosts.service_status_push import HOST_STATUS_NAMESPACE, HostStatusPushService
from app.packs.models import HostPackInstallation
from tests.packs.factories import seed_test_packs

if TYPE_CHECKING:
    import pytest
    from httpx2 import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession

PACK_ENTRY = {
    "pack_id": "appium-uiautomator2",
    "pack_release": "2026.04.0",
    "runtime_id": "abc123",
    "status": "installed",
    "resolved_install_spec": {"appium": "2.11.5", "uiautomator2": "3.6.0"},
    "installer_log_excerpt": "ok",
    "resolver_version": "1",
    "blocked_reason": None,
}


async def _make_host(db_session: AsyncSession, *, status: HostStatus, hostname: str) -> Host:
    host = Host(
        hostname=hostname,
        ip="10.0.0.9",
        os_type=OSType.linux,
        agent_port=5100,
        status=status,
    )
    db_session.add(host)
    await db_session.commit()
    await db_session.refresh(host)
    return host


async def test_status_push_unknown_host_404(client: AsyncClient) -> None:
    resp = await client.post("/agent/hosts/status", json={"host_id": str(uuid.uuid4())})
    assert resp.status_code == 404


async def test_status_push_stamps_liveness_and_stores_snapshot(client: AsyncClient, db_session: AsyncSession) -> None:
    online_host = await _make_host(db_session, status=HostStatus.online, hostname="status-push-online")
    body = {
        "host_id": str(online_host.id),
        "agent_version": "9.9.9",
        "capabilities": {"tools": {"node": "22.1.0"}, "orchestration_contract_version": 6},
        "missing_prerequisites": ["adb"],
        "appium_processes": {"running_nodes": [{"port": 4723, "pid": 111}]},
        "host_telemetry": {"recorded_at": "2026-07-09T00:00:00+00:00", "cpu_percent": 1.0},
        "node_health": {"reported_at": "2026-07-09T00:00:01+00:00", "nodes": []},
        "device_health": {"reported_at": "2026-07-09T00:00:02+00:00", "devices": {}},
        "device_telemetry": {"reported_at": "2026-07-09T00:00:03+00:00", "devices": {}},
        "device_properties": {"reported_at": "2026-07-09T00:00:04+00:00", "devices": {}},
    }
    resp = await client.post("/agent/hosts/status", json=body)
    assert resp.status_code == 204
    await db_session.refresh(online_host)
    assert online_host.last_heartbeat is not None
    assert online_host.agent_version == "9.9.9"
    assert online_host.capabilities["tools"] == {"node": "22.1.0"}
    assert "adb" in online_host.missing_prerequisites
    stored = await control_plane_state_store.get_value(db_session, HOST_STATUS_NAMESPACE, str(online_host.id))
    assert stored["payload"]["appium_processes"]["running_nodes"][0]["port"] == 4723
    assert stored["payload"]["node_health"]["reported_at"] == "2026-07-09T00:00:01+00:00"
    assert stored["payload"]["device_health"]["reported_at"] == "2026-07-09T00:00:02+00:00"
    assert stored["payload"]["device_telemetry"]["reported_at"] == "2026-07-09T00:00:03+00:00"
    assert stored["payload"]["device_properties"]["reported_at"] == "2026-07-09T00:00:04+00:00"


async def test_status_push_never_writes_status(client: AsyncClient, db_session: AsyncSession) -> None:
    offline_host = await _make_host(db_session, status=HostStatus.offline, hostname="status-push-offline")
    resp = await client.post("/agent/hosts/status", json={"host_id": str(offline_host.id)})
    assert resp.status_code == 204
    await db_session.refresh(offline_host)
    # Ledger untouched — the sweep's edge detector owns the online flip; reads compute online.
    assert offline_host.status == HostStatus.offline
    assert offline_host.last_heartbeat is not None


async def test_status_push_rejects_stale_orchestration_contract(client: AsyncClient, db_session: AsyncSession) -> None:
    host = await _make_host(db_session, status=HostStatus.online, hostname="status-push-stale-contract")
    body = {"host_id": str(host.id), "capabilities": {"orchestration_contract_version": 5}}
    resp = await client.post("/agent/hosts/status", json=body)
    assert resp.status_code == 426
    await db_session.refresh(host)
    # No liveness stamp -> the host goes stale -> reads offline within the recency window.
    assert host.last_heartbeat is None


async def test_status_push_without_capabilities_still_accepted(client: AsyncClient, db_session: AsyncSession) -> None:
    host = await _make_host(db_session, status=HostStatus.online, hostname="status-push-no-caps")
    resp = await client.post("/agent/hosts/status", json={"host_id": str(host.id)})
    assert resp.status_code == 204
    await db_session.refresh(host)
    assert host.last_heartbeat is not None


async def test_status_push_never_flips_pending_host(client: AsyncClient, db_session: AsyncSession) -> None:
    pending_host = await _make_host(db_session, status=HostStatus.pending, hostname="status-push-pending")
    resp = await client.post("/agent/hosts/status", json={"host_id": str(pending_host.id)})
    assert resp.status_code == 204
    await db_session.refresh(pending_host)
    assert pending_host.status == HostStatus.pending  # approval flow owns this transition
    assert pending_host.last_heartbeat is not None


async def test_status_push_applies_pack_section(client: AsyncClient, db_session: AsyncSession) -> None:
    await seed_test_packs(db_session)
    online_host = await _make_host(db_session, status=HostStatus.online, hostname="status-push-packs")
    body = {
        "host_id": str(online_host.id),
        "packs": {"runtimes": [], "packs": [PACK_ENTRY], "doctor": []},
    }
    resp = await client.post("/agent/hosts/status", json=body)
    assert resp.status_code == 204
    installs = (
        (await db_session.execute(select(HostPackInstallation).where(HostPackInstallation.host_id == online_host.id)))
        .scalars()
        .all()
    )
    assert len(installs) == 1
    assert installs[0].pack_id == "appium-uiautomator2"
    assert installs[0].status == "installed"


async def test_status_push_without_packs_section_is_fine(client: AsyncClient, db_session: AsyncSession) -> None:
    online_host = await _make_host(db_session, status=HostStatus.online, hostname="status-push-no-packs")
    resp = await client.post("/agent/hosts/status", json={"host_id": str(online_host.id)})
    assert resp.status_code == 204


async def test_status_push_processes_observations_after_liveness_commit(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    online_host = await _make_host(db_session, status=HostStatus.online, hostname="status-push-observations")
    process = AsyncMock()
    monkeypatch.setattr(HostStatusPushService, "process_observations", process)
    body = {
        "host_id": str(online_host.id),
        "device_health": {"reported_at": "2026-07-11T00:00:00+00:00", "devices": {}},
    }

    resp = await client.post("/agent/hosts/status", json=body)

    assert resp.status_code == 204
    process.assert_awaited_once()
    assert process.await_args.kwargs["host_id"] == online_host.id
    assert process.await_args.kwargs["payload"]["device_health"] == body["device_health"]
    await db_session.refresh(online_host)
    assert online_host.last_heartbeat is not None


async def test_status_push_returns_204_when_observation_processing_fails(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    online_host = await _make_host(db_session, status=HostStatus.online, hostname="status-push-observation-failure")

    async def boom(self: HostStatusPushService, **kwargs: object) -> None:
        raise RuntimeError("observation boom")

    monkeypatch.setattr(HostStatusPushService, "process_observations", boom)
    resp = await client.post("/agent/hosts/status", json={"host_id": str(online_host.id)})

    assert resp.status_code == 204
    await db_session.refresh(online_host)
    assert online_host.last_heartbeat is not None
