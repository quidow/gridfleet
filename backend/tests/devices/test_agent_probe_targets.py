from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import pytest

from app.appium_nodes.models import AppiumDesiredState, AppiumNode, AppiumNodeResourceClaim
from app.hosts.models import Host
from tests.helpers import create_device_record

if TYPE_CHECKING:
    from httpx2 import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession


@pytest.mark.db
async def test_probe_targets_lists_all_host_devices(
    client: AsyncClient, db_session: AsyncSession, db_host: Host
) -> None:
    device = await create_device_record(
        db_session,
        host_id=db_host.id,
        identity_value="probe-target-device",
        connection_target="probe-target-device",
        name="Probe target device",
        pack_id="test-pack",
        platform_id="test-platform",
        device_type="emulator",
        connection_type="virtual",
    )

    response = await client.get("/agent/devices/probe-targets", params={"host_id": str(db_host.id)})

    assert response.status_code == 200
    body = response.json()
    assert body["host_id"] == str(db_host.id)
    entries = {entry["connection_target"]: entry for entry in body["devices"]}
    entry = entries[device.connection_target]
    for key in (
        "device_id",
        "pack_id",
        "platform_id",
        "device_type",
        "connection_type",
        "ip_address",
        "identity_value",
        "claimed_ports",
        "ip_ping_timeout_sec",
        "ip_ping_count",
    ):
        assert key in entry
    assert entry["allow_boot"] is False
    assert isinstance(entry["ip_ping_timeout_sec"], (int, float))
    assert isinstance(entry["ip_ping_count"], int)


@pytest.mark.db
async def test_probe_targets_scopes_to_host_and_falls_back_to_identity(
    client: AsyncClient, db_session: AsyncSession, db_host: Host
) -> None:
    fallback = await create_device_record(
        db_session,
        host_id=db_host.id,
        identity_value="identity-fallback",
        name="Fallback target device",
    )
    fallback.connection_target = None

    other_host = Host(
        hostname="other-probe-host",
        ip="10.0.0.251",
        os_type=db_host.os_type,
        agent_port=5100,
    )
    db_session.add(other_host)
    await db_session.flush()
    await create_device_record(
        db_session,
        host_id=other_host.id,
        identity_value="other-host-device",
        name="Other host device",
    )
    await db_session.commit()

    response = await client.get("/agent/devices/probe-targets", params={"host_id": str(db_host.id)})

    assert response.status_code == 200
    entries = {entry["connection_target"]: entry for entry in response.json()["devices"]}
    assert entries[fallback.identity_value]["device_id"] == str(fallback.id)
    assert "other-host-device" not in entries


@pytest.mark.db
async def test_probe_targets_includes_parallel_resource_claims(
    client: AsyncClient, db_session: AsyncSession, db_host: Host
) -> None:
    device = await create_device_record(
        db_session,
        host_id=db_host.id,
        identity_value="claimed-port-device",
        name="Claimed port device",
    )
    node = AppiumNode(
        device_id=device.id,
        port=4723,
        desired_state=AppiumDesiredState.running,
        desired_port=4723,
    )
    db_session.add(node)
    await db_session.flush()
    db_session.add(
        AppiumNodeResourceClaim(
            host_id=db_host.id,
            capability_key="appium:systemPort",
            port=8200,
            node_id=node.id,
        )
    )
    await db_session.commit()

    response = await client.get("/agent/devices/probe-targets", params={"host_id": str(db_host.id)})

    assert response.status_code == 200
    entries = {entry["connection_target"]: entry for entry in response.json()["devices"]}
    assert entries[device.connection_target]["claimed_ports"] == {"appium:systemPort": 8200}


@pytest.mark.db
async def test_probe_targets_returns_404_for_unknown_host(client: AsyncClient) -> None:
    response = await client.get("/agent/devices/probe-targets", params={"host_id": str(uuid.uuid4())})

    assert response.status_code == 404


@pytest.mark.db
async def test_probe_targets_excludes_maintenance_devices(
    client: AsyncClient, db_session: AsyncSession, db_host: Host
) -> None:
    active = await create_device_record(
        db_session,
        host_id=db_host.id,
        identity_value="active-device",
        connection_target="active-device",
        name="Active device",
    )
    await create_device_record(
        db_session,
        host_id=db_host.id,
        identity_value="maint-device",
        connection_target="maint-device",
        name="Maintenance device",
        lifecycle_policy_state={"maintenance_reason": "Operator entered maintenance"},
    )

    response = await client.get("/agent/devices/probe-targets", params={"host_id": str(db_host.id)})

    assert response.status_code == 200
    targets = {entry["connection_target"] for entry in response.json()["devices"]}
    assert targets == {active.connection_target}
