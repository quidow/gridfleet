from typing import TYPE_CHECKING

import pytest
from sqlalchemy import select

from app.hosts.models import Host, HostStatus, OSType
from app.packs.models import HostPackDoctorResult, HostPackInstallation, InstallStatus
from app.packs.services.status import PackStatusService
from tests.packs.factories import seed_test_packs

if TYPE_CHECKING:
    from httpx2 import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession

_status_svc = PackStatusService()


def _make_payload(host_id: str) -> dict:
    return {
        "host_id": host_id,
        "runtimes": [
            {
                "runtime_id": "abc123",
                "appium_server": {"package": "appium", "version": "2.11.5"},
                "appium_driver": [{"package": "appium-uiautomator2-driver", "version": "3.6.0"}],
                "appium_home": "/var/lib/gridfleet-agent/runtimes/abc123",
                "status": "installed",
                "blocked_reason": None,
            }
        ],
        "packs": [
            {
                "pack_id": "appium-uiautomator2",
                "pack_release": "2026.04.0",
                "runtime_id": "abc123",
                "status": "installed",
                "resolved_install_spec": {"appium": "2.11.5", "uiautomator2": "3.6.0"},
                "installer_log_excerpt": "ok",
                "resolver_version": "1",
                "blocked_reason": None,
            }
        ],
        "doctor": [{"pack_id": "appium-uiautomator2", "check_id": "adb_present", "ok": True, "message": ""}],
    }


@pytest.mark.asyncio
async def test_status_upsert_creates_runtime_and_installation(client: AsyncClient, db_session: AsyncSession) -> None:
    await seed_test_packs(db_session)
    host = Host(
        hostname="h2.local",
        ip="10.0.0.2",
        os_type=OSType.linux,
        agent_port=5100,
        status=HostStatus.online,
    )
    db_session.add(host)
    await db_session.commit()
    host_id = str(host.id)

    payload = _make_payload(host_id)

    resp = await client.post("/agent/driver-packs/status", json=payload)
    assert resp.status_code == 204

    installs = (await db_session.execute(select(HostPackInstallation))).scalars().all()
    assert len(installs) == 1
    assert installs[0].status == "installed"
    assert installs[0].runtime_id == "abc123"
    # Runtime report is folded onto the pack row keyed by runtime_id.
    assert installs[0].runtime_status == "installed"
    assert installs[0].appium_server_version == "2.11.5"

    doctor = (await db_session.execute(select(HostPackDoctorResult))).scalars().all()
    assert len(doctor) == 1
    assert doctor[0].ok is True


@pytest.mark.asyncio
async def test_agent_status_persists_blocked_reason(
    client: AsyncClient, db_session: AsyncSession, default_host_id: str
) -> None:
    await seed_test_packs(db_session)
    payload = {
        "host_id": default_host_id,
        "runtimes": [],
        "packs": [
            {
                "pack_id": "appium-xcuitest",
                "pack_release": "2026.04.0",
                "runtime_id": None,
                "status": "blocked",
                "blocked_reason": "adapter_unavailable",
            }
        ],
        "doctor": [],
    }

    response = await client.post("/agent/driver-packs/status", json=payload)

    assert response.status_code == 204
    row = (
        await db_session.execute(select(HostPackInstallation).where(HostPackInstallation.pack_id == "appium-xcuitest"))
    ).scalar_one()
    assert row.status == "blocked"
    assert row.blocked_reason == "adapter_unavailable"


@pytest.mark.asyncio
async def test_status_upsert_is_idempotent(client: AsyncClient, db_session: AsyncSession) -> None:
    await seed_test_packs(db_session)
    host = Host(
        hostname="h3.local",
        ip="10.0.0.3",
        os_type=OSType.linux,
        agent_port=5100,
        status=HostStatus.online,
    )
    db_session.add(host)
    await db_session.commit()
    host_id = str(host.id)

    payload = _make_payload(host_id)

    # POST twice
    resp1 = await client.post("/agent/driver-packs/status", json=payload)
    assert resp1.status_code == 204
    resp2 = await client.post("/agent/driver-packs/status", json=payload)
    assert resp2.status_code == 204

    # Exactly 1 row each — upsert, not append
    installs = (await db_session.execute(select(HostPackInstallation))).scalars().all()
    assert len(installs) == 1
    assert installs[0].appium_server_version == "2.11.5"

    # Doctor rows: cleared + reinserted each time → still exactly 1
    doctor = (await db_session.execute(select(HostPackDoctorResult))).scalars().all()
    assert len(doctor) == 1
    assert doctor[0].ok is True


@pytest.mark.asyncio
async def test_apply_status_maps_runtime_onto_pack_row(db_session: AsyncSession, db_host: Host) -> None:
    await seed_test_packs(db_session)
    payload = {
        "host_id": str(db_host.id),
        "runtimes": [
            {
                "runtime_id": "rt-1",
                "appium_server": {"package": "appium", "version": "3.0.0"},
                "appium_driver": [{"package": "appium-uiautomator2-driver", "version": "3.6.0"}],
                "appium_home": "/tmp/rt-1",
                "status": "installed",
                "blocked_reason": None,
            }
        ],
        "packs": [
            {
                "pack_id": "appium-uiautomator2",
                "pack_release": "2026.04.0",
                "runtime_id": "rt-1",
                "status": "installed",
            }
        ],
        "doctor": [],
    }

    await _status_svc.apply_status(db_session, payload)

    row = (await db_session.execute(select(HostPackInstallation))).scalar_one()
    assert row.runtime_status == InstallStatus.installed
    assert row.appium_server_package == "appium"
    assert row.appium_server_version == "3.0.0"
    assert (row.driver_specs or [])[0]["version"] == "3.6.0"
    assert row.appium_home == "/tmp/rt-1"


@pytest.mark.asyncio
async def test_apply_status_coerces_unknown_status_to_blocked(db_session: AsyncSession, db_host: Host) -> None:
    await seed_test_packs(db_session)
    payload = {
        "host_id": str(db_host.id),
        "runtimes": [],
        "packs": [
            {
                "pack_id": "appium-uiautomator2",
                "pack_release": "2026.04.0",
                "runtime_id": None,
                "status": "exploded",
            }
        ],
        "doctor": [],
    }

    await _status_svc.apply_status(db_session, payload)

    row = (await db_session.execute(select(HostPackInstallation))).scalar_one()
    assert row.status == InstallStatus.blocked
    assert "unknown status" in (row.blocked_reason or "")
