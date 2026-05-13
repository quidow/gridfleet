"""Tests for the typed Appium parallel-resource allocator."""

from __future__ import annotations

import uuid as uuidlib
from typing import TYPE_CHECKING

import pytest

from app.models.appium_node_resource_claim import AppiumNodeResourceClaim
from app.services import appium_node_resource_service as svc

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@pytest.mark.db
@pytest.mark.asyncio
async def test_reserve_managed_picks_first_free_port(db_session: AsyncSession) -> None:
    host_id = uuidlib.uuid4()
    node_id = await _make_node(db_session, host_id)
    port = await svc.reserve(
        db_session,
        host_id=host_id,
        capability_key="appium:mjpegServerPort",
        start_port=8001,
        node_id=node_id,
    )
    assert port == 8001
    await db_session.commit()


@pytest.mark.db
@pytest.mark.asyncio
async def test_reserve_skips_taken_port(db_session: AsyncSession) -> None:
    host_id = uuidlib.uuid4()
    other_node = await _make_node(db_session, host_id)
    db_session.add(
        AppiumNodeResourceClaim(
            host_id=host_id,
            capability_key="appium:mjpegServerPort",
            port=8001,
            node_id=other_node,
        )
    )
    await db_session.commit()

    target_node = await _make_node(db_session, host_id)
    port = await svc.reserve(
        db_session,
        host_id=host_id,
        capability_key="appium:mjpegServerPort",
        start_port=8001,
        node_id=target_node,
    )
    assert port == 8002


@pytest.mark.db
@pytest.mark.asyncio
async def test_reserve_returns_pool_exhausted_error(db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    host_id = uuidlib.uuid4()
    monkeypatch.setattr(svc, "POOL_SIZE", 8)
    for offset in range(svc.POOL_SIZE):
        node_id = await _make_node(db_session, host_id)
        db_session.add(
            AppiumNodeResourceClaim(
                host_id=host_id,
                capability_key="appium:mjpegServerPort",
                port=8001 + offset,
                node_id=node_id,
            )
        )
    await db_session.commit()

    target_node = await _make_node(db_session, host_id)
    with pytest.raises(svc.PoolExhaustedError):
        await svc.reserve(
            db_session,
            host_id=host_id,
            capability_key="appium:mjpegServerPort",
            start_port=8001,
            node_id=target_node,
        )


@pytest.mark.db
@pytest.mark.asyncio
async def test_release_managed_deletes_claim(db_session: AsyncSession) -> None:
    host_id = uuidlib.uuid4()
    node_id = await _make_node(db_session, host_id)
    await svc.reserve(
        db_session,
        host_id=host_id,
        capability_key="appium:mjpegServerPort",
        start_port=8001,
        node_id=node_id,
    )
    await db_session.commit()
    await svc.release_managed(db_session, node_id=node_id)
    await db_session.commit()
    assert await svc.get_capabilities(db_session, node_id=node_id) == {}


@pytest.mark.db
@pytest.mark.asyncio
async def test_list_claims_for_node_orders_by_capability_key(db_session: AsyncSession) -> None:
    host_id = uuidlib.uuid4()
    node_id = await _make_node(db_session, host_id)
    db_session.add_all(
        [
            AppiumNodeResourceClaim(
                host_id=host_id,
                capability_key="appium:zPort",
                port=9002,
                node_id=node_id,
            ),
            AppiumNodeResourceClaim(
                host_id=host_id,
                capability_key="appium:aPort",
                port=9001,
                node_id=node_id,
            ),
        ]
    )
    await db_session.commit()

    claims = await svc.list_claims_for_node(db_session, node_id=node_id)

    assert [claim.capability_key for claim in claims] == ["appium:aPort", "appium:zPort"]


@pytest.mark.db
@pytest.mark.asyncio
async def test_reserve_is_race_safe_under_contention(
    db_session_maker: async_sessionmaker[AsyncSession],
) -> None:
    import asyncio

    host_id = uuidlib.uuid4()

    async with db_session_maker() as bootstrap:
        node_ids = [await _make_node(bootstrap, host_id) for _ in range(32)]
        await bootstrap.commit()

    async def one_reserver(idx: int) -> int:
        async with db_session_maker() as session:
            port = await svc.reserve(
                session,
                host_id=host_id,
                capability_key="appium:mjpegServerPort",
                start_port=8001,
                node_id=node_ids[idx],
            )
            await session.commit()
            return port

    ports = await asyncio.gather(*(one_reserver(i) for i in range(32)))
    assert len(set(ports)) == 32, f"Duplicate ports under contention: {ports}"
    assert sorted(ports) == list(range(8001, 8033))


@pytest.mark.db
@pytest.mark.asyncio
async def test_reserve_same_owner_idempotent_under_contention(
    db_session_maker: async_sessionmaker[AsyncSession],
) -> None:
    import asyncio

    from sqlalchemy.exc import IntegrityError

    host_id = uuidlib.uuid4()
    async with db_session_maker() as bootstrap:
        node_id = await _make_node(bootstrap, host_id)
        await bootstrap.commit()

    async def one_reserver() -> int | None:
        async with db_session_maker() as session:
            try:
                port = await svc.reserve(
                    session,
                    host_id=host_id,
                    capability_key="appium:mjpegServerPort",
                    start_port=8001,
                    node_id=node_id,
                )
                await session.commit()
                return port
            except IntegrityError:
                await session.rollback()
                return None

    results = await asyncio.gather(*(one_reserver() for _ in range(8)))
    successful = [p for p in results if p is not None]
    assert len(successful) == 1, f"Expected exactly one winner under same-owner contention: {results}"


async def _make_node(db_session: AsyncSession, host_id: uuidlib.UUID) -> uuidlib.UUID:
    from sqlalchemy import select

    from app.models.appium_node import AppiumDesiredState, AppiumNode
    from app.models.device import (
        ConnectionType,
        Device,
        DeviceOperationalState,
        DeviceType,
        HardwareHealthStatus,
        HardwareTelemetrySupportStatus,
    )
    from app.models.host import Host, HostStatus, OSType

    host = (await db_session.execute(select(Host).where(Host.id == host_id))).scalar_one_or_none()
    if host is None:
        host = Host(
            id=host_id,
            hostname=f"h-{host_id.hex[:6]}",
            ip="127.0.0.1",
            agent_port=5100,
            os_type=OSType.linux,
            status=HostStatus.online,
        )
        db_session.add(host)
        await db_session.flush()
    device = Device(
        host_id=host_id,
        pack_id="appium-uiautomator2",
        platform_id="android",
        identity_scheme="adb",
        identity_scope="host",
        identity_value=f"id-{uuidlib.uuid4().hex[:8]}",
        name="test",
        os_version="14",
        operational_state=DeviceOperationalState.offline,
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
        hardware_health_status=HardwareHealthStatus.unknown,
        hardware_telemetry_support_status=HardwareTelemetrySupportStatus.unknown,
    )
    db_session.add(device)
    await db_session.flush()
    node = AppiumNode(
        device_id=device.id,
        port=4723,
        grid_url="http://hub:4444",
        desired_state=AppiumDesiredState.stopped,
        desired_port=None,
        pid=None,
        active_connection_target=None,
    )
    db_session.add(node)
    await db_session.flush()
    return node.id
