"""Tests for POST /api/hosts/{host_id}/driver-packs/{pack_id}/doctor."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import ANY, AsyncMock, patch

import pytest
from sqlalchemy import select

from app.hosts.models import Host, HostStatus, OSType
from app.packs.models import HostPackDoctorResult
from app.packs.models.pack import DriverPack

if TYPE_CHECKING:
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession


async def _create_online_host(db_session: AsyncSession) -> Host:
    host = Host(
        hostname="doctor-test-host",
        ip="192.168.1.200",
        os_type=OSType.linux,
        agent_port=5100,
        status=HostStatus.online,
    )
    db_session.add(host)
    await db_session.flush()
    return host


async def _create_offline_host(db_session: AsyncSession) -> Host:
    host = Host(
        hostname="doctor-offline-host",
        ip="192.168.1.201",
        os_type=OSType.linux,
        agent_port=5100,
        status=HostStatus.offline,
    )
    db_session.add(host)
    await db_session.flush()
    return host


@pytest.mark.db
async def test_trigger_doctor_returns_404_for_unknown_host(client: AsyncClient) -> None:
    resp = await client.post("/api/hosts/00000000-0000-0000-0000-000000000000/driver-packs/appium-uiautomator2/doctor")
    assert resp.status_code == 404


@pytest.mark.db
async def test_trigger_doctor_returns_409_for_offline_host(client: AsyncClient, db_session: AsyncSession) -> None:
    host = await _create_offline_host(db_session)
    resp = await client.post(f"/api/hosts/{host.id}/driver-packs/appium-uiautomator2/doctor")
    assert resp.status_code == 409


@pytest.mark.db
async def test_trigger_doctor_proxies_to_agent_and_persists(client: AsyncClient, db_session: AsyncSession) -> None:
    host = await _create_online_host(db_session)
    db_session.add(DriverPack(id="appium-uiautomator2", origin="uploaded", display_name="UiAutomator2", maintainer=""))
    await db_session.flush()
    agent_checks: list[dict[str, Any]] = [
        {"check_id": "adb", "ok": True, "message": "adb found"},
        {"check_id": "java", "ok": False, "message": "java not found"},
    ]
    with patch(
        "app.hosts.router.agent_operations.pack_doctor",
        new_callable=AsyncMock,
        return_value=agent_checks,
    ) as mock_doctor:
        resp = await client.post(f"/api/hosts/{host.id}/driver-packs/appium-uiautomator2/doctor")

    assert resp.status_code == 200
    mock_doctor.assert_awaited_once_with(
        host.ip, host.agent_port, "appium-uiautomator2", settings=ANY, circuit_breaker=ANY, pool=ANY
    )

    body = resp.json()
    assert len(body) == 2
    assert body[0]["check_id"] == "adb"
    assert body[0]["ok"] is True
    assert body[1]["check_id"] == "java"
    assert body[1]["ok"] is False

    rows = (
        (
            await db_session.execute(
                select(HostPackDoctorResult).where(
                    HostPackDoctorResult.host_id == host.id,
                    HostPackDoctorResult.pack_id == "appium-uiautomator2",
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 2
    by_check = {r.check_id: r for r in rows}
    assert by_check["adb"].ok is True
    assert by_check["java"].ok is False
