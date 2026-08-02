import uuid
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import func, select

from app.events.models import SystemEvent
from app.hosts.models import Host, HostStatus, OSType
from app.packs.models import DriverPack, HostPackInstallation, PackState
from tests.packs.factories import seed_test_packs

if TYPE_CHECKING:
    from httpx2 import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@pytest.mark.asyncio
async def test_catalog_lists_pack(client: AsyncClient, db_session: AsyncSession) -> None:
    await seed_test_packs(db_session)
    await db_session.commit()

    resp = await client.get("/api/driver-packs/catalog")
    assert resp.status_code == 200
    body = resp.json()
    assert any(p["id"] == "appium-uiautomator2" for p in body["packs"])
    uiautomator2 = next(p for p in body["packs"] if p["id"] == "appium-uiautomator2")
    assert {p["id"] for p in uiautomator2["platforms"]} == {
        "android_mobile",
        "android_tv",
        "firetv_real",
    }


@pytest.mark.asyncio
async def test_catalog_read_does_not_end_the_callers_transaction(
    client: AsyncClient,
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """A GET route shares the request session; committing it publishes the caller's writes.

    The marker row is the observable: it is flushed but deliberately not
    committed, so any commit the read performs on the request session makes it
    visible to every other session in the database.
    """
    await seed_test_packs(db_session)
    await db_session.commit()
    db_session.add(DriverPack(id="catalog/uncommitted-marker", display_name="Uncommitted marker"))
    await db_session.flush()

    resp = await client.get("/api/driver-packs/catalog")

    assert resp.status_code == 200
    assert db_session.in_transaction(), "the catalog read ended the request session's transaction"
    async with db_session_maker() as peer:
        leaked = await peer.get(DriverPack, "catalog/uncommitted-marker")
    assert leaked is None, "the catalog read committed the caller's pending write"


@pytest.mark.asyncio
async def test_catalog_read_leaves_a_zero_work_draining_pack_draining(
    client: AsyncClient,
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Drain completion belongs to the release hook and the janitor backstop."""
    await seed_test_packs(db_session, state=PackState.draining)
    await db_session.commit()
    async with db_session_maker() as peer:
        events_before = await peer.scalar(select(func.count()).select_from(SystemEvent))

    resp = await client.get("/api/driver-packs/catalog")

    assert resp.status_code == 200
    async with db_session_maker() as peer:
        states = set((await peer.execute(select(DriverPack.state))).scalars().all())
        events_after = await peer.scalar(select(func.count()).select_from(SystemEvent))
    assert states == {PackState.draining}, f"the catalog read completed a drain it must not touch: {states}"
    assert events_after == events_before, "the catalog read recorded an event"


@pytest.mark.asyncio
async def test_get_single_pack(client: AsyncClient, db_session: AsyncSession) -> None:
    await seed_test_packs(db_session)
    await db_session.commit()

    resp = await client.get("/api/driver-packs/appium-uiautomator2")
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == "appium-uiautomator2"
    assert "platforms" in data
    assert "state" in data
    assert "runtime_policy" in data
    assert {p["id"] for p in data["platforms"]} == {
        "android_mobile",
        "android_tv",
        "firetv_real",
    }
    assert isinstance(data.get("insecure_features"), list)


@pytest.mark.asyncio
async def test_get_single_pack_exposes_manifest_details(client: AsyncClient, db_session: AsyncSession) -> None:
    await seed_test_packs(db_session)
    await db_session.commit()

    resp = await client.get("/api/driver-packs/appium-xcuitest")

    assert resp.status_code == 200
    data = resp.json()
    assert data["maintainer"] == "gridfleet-team"
    assert data["license"] == "Apache-2.0"
    assert data["appium_server"] == {
        "source": "npm",
        "package": "appium",
        "version": ">=2.5,<3",
        "recommended": "2.19.0",
        "known_bad": [],
        "github_repo": None,
    }
    assert data["appium_driver"] == {
        "source": "npm",
        "package": "appium-xcuitest-driver",
        "version": ">=7,<10",
        "recommended": "9.3.1",
        "known_bad": ["9.0.0"],
        "github_repo": None,
    }
    assert data["appium_env"] == [
        {
            "id": "tvos_devicectl_preference",
            "applies_when": {
                "platform_ids": ["tvos"],
                "device_types": ["real_device"],
                "min_os_version": None,
                "device_config": {"prefer_devicectl": True},
            },
            "env": {"APPIUM_XCUITEST_PREFER_DEVICECTL": "1"},
        }
    ]


@pytest.mark.asyncio
async def test_get_single_pack_not_found(client: AsyncClient) -> None:
    resp = await client.get("/api/driver-packs/nonexistent-pack")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_pack_hosts_unknown_pack_is_404(client: AsyncClient) -> None:
    resp = await client.get("/api/driver-packs/nonexistent-pack/hosts")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_delete_unknown_pack_is_404(client: AsyncClient) -> None:
    resp = await client.delete("/api/driver-packs/nonexistent-pack")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_update_pack_rejects_unknown_state(client: AsyncClient, db_session: AsyncSession) -> None:
    await seed_test_packs(db_session)
    await db_session.flush()

    resp = await client.patch("/api/driver-packs/appium-uiautomator2", json={"state": "not-a-state"})
    assert resp.status_code == 400

    # The pack is untouched: an invalid target state is rejected before any transition.
    unchanged = await client.get("/api/driver-packs/appium-uiautomator2")
    assert unchanged.json()["state"] == PackState.enabled.value


@pytest.mark.asyncio
async def test_catalog_exposes_display_metadata(client: AsyncClient, db_session: AsyncSession) -> None:
    await seed_test_packs(db_session)
    await db_session.flush()
    resp = await client.get("/api/driver-packs/catalog")
    assert resp.status_code == 200
    pack = next(p for p in resp.json()["packs"] if p["id"] == "appium-uiautomator2")
    platform = next(p for p in pack["platforms"] if p["id"] == "android_mobile")
    assert platform["display_metadata"] == {"icon_kind": "mobile"}


@pytest.mark.asyncio
async def test_catalog_exposes_identity_and_lifecycle_actions(client: AsyncClient, db_session: AsyncSession) -> None:
    await seed_test_packs(db_session)
    await db_session.flush()

    response = await client.get("/api/driver-packs/catalog")

    assert response.status_code == 200
    pack = next(p for p in response.json()["packs"] if p["id"] == "appium-uiautomator2")
    platform = next(p for p in pack["platforms"] if p["id"] == "android_mobile")
    assert platform["identity_scheme"] == "android_serial"
    assert platform["identity_scope"] == "host"
    assert platform["device_types"] == ["real_device", "emulator"]
    assert platform["connection_types"] == ["usb", "network", "virtual"]
    assert platform["lifecycle_actions"] == [
        {"id": "reconnect"},
        {"id": "release_forwarded_ports"},
    ]
    assert platform["device_type_overrides"]["emulator"]["lifecycle_actions"] == [
        {"id": "release_forwarded_ports"},
    ]
    assert platform["health_checks"] == [
        {"id": "adb_connected", "label": "ADB Connected", "applies_when": None},
        {"id": "adb_responsive", "label": "ADB Responsive", "applies_when": None},
        {"id": "boot_completed", "label": "Boot Completed", "applies_when": None},
        {"id": "ping", "label": "IP Reachable", "applies_when": None},
    ]


@pytest.mark.asyncio
async def test_catalog_exposes_default_capabilities(client: AsyncClient, db_session: AsyncSession) -> None:
    await seed_test_packs(db_session)
    await db_session.flush()
    resp = await client.get("/api/driver-packs/catalog")
    pack = next(p for p in resp.json()["packs"] if p["id"] == "appium-xcuitest")
    ios = next(p for p in pack["platforms"] if p["id"] == "ios")
    tvos = next(p for p in pack["platforms"] if p["id"] == "tvos")
    assert "devicectl_tunnel" in {check["id"] for check in ios["health_checks"]}
    assert "devicectl_tunnel" not in {check["id"] for check in tvos["health_checks"]}
    assert tvos["default_capabilities"] == {}
    real_override = tvos["device_type_overrides"]["real_device"]
    assert real_override["default_capabilities"]["appium:platformVersion"] == "{device.os_version}"
    assert "appium:wdaBaseUrl" not in real_override["default_capabilities"]
    assert "appium:updatedWDABundleId" not in real_override["default_capabilities"]
    wda_field = next(field for field in real_override["device_fields_schema"] if field["id"] == "wda_base_url")
    assert wda_field.get("required_for_session_when") == {"prefer_devicectl": True}
    assert wda_field["capability_name"] == "appium:wdaBaseUrl"
    bundle_field = next(
        field for field in real_override["device_fields_schema"] if field["id"] == "updated_wda_bundle_id"
    )
    assert bundle_field["required_for_session"] is True
    cb = real_override["connection_behavior"]
    assert cb["default_device_type"] == "real_device"
    assert cb["default_connection_type"] == "usb"
    assert cb["requires_ip_address"] is False
    assert cb["requires_connection_target"] is True


@pytest.mark.asyncio
async def test_catalog_exposes_insecure_features(client: AsyncClient, db_session: AsyncSession) -> None:
    await seed_test_packs(db_session)
    await db_session.flush()
    resp = await client.get("/api/driver-packs/catalog")
    pack = next(p for p in resp.json()["packs"] if p["id"] == "appium-uiautomator2")
    assert "uiautomator2:chromedriver_autodownload" in pack["insecure_features"]


@pytest.mark.asyncio
async def test_catalog_exposes_observed_runtime_versions(
    client: AsyncClient, db_session: AsyncSession, default_host_id: str
) -> None:
    await seed_test_packs(db_session)
    host_id = uuid.UUID(default_host_id)
    db_session.add(
        HostPackInstallation(
            host_id=host_id,
            pack_id="appium-uiautomator2",
            pack_release="2026.04.0",
            runtime_id="runtime-android",
            status="installed",
            resolved_install_spec={"appium_driver_version": "3.6.0"},
            appium_server_package="appium",
            appium_server_version="2.19.0",
            driver_specs=[{"package": "appium-uiautomator2-driver", "version": "4.2.0"}],
            runtime_status="installed",
        )
    )
    await db_session.commit()

    resp = await client.get("/api/driver-packs/catalog")

    assert resp.status_code == 200
    pack = next(p for p in resp.json()["packs"] if p["id"] == "appium-uiautomator2")
    assert pack["runtime_summary"] == {
        "installed_hosts": 1,
        "blocked_hosts": 0,
        "actual_appium_server_versions": ["2.19.0"],
        "actual_appium_driver_versions": ["4.2.0"],
        "driver_drift_hosts": 1,
    }


@pytest.mark.asyncio
async def test_host_driver_pack_status_endpoint(client: AsyncClient, db_session: AsyncSession) -> None:
    await seed_test_packs(db_session)
    host = Host(
        hostname="catalog-mac-status.local",
        ip="10.0.0.97",
        os_type=OSType.macos,
        agent_port=5100,
        status=HostStatus.online,
    )
    db_session.add(host)
    await db_session.flush()
    db_session.add(
        HostPackInstallation(
            host_id=host.id,
            pack_id="appium-xcuitest",
            pack_release="2026.04.0",
            runtime_id=None,
            status="blocked",
            blocked_reason="adapter_unavailable",
        )
    )
    await db_session.commit()

    response = await client.get(f"/api/hosts/{host.id}/driver-packs")

    assert response.status_code == 200
    data = response.json()
    assert data["host_id"] == str(host.id)
    assert data["packs"][0]["pack_id"] == "appium-xcuitest"
    assert data["packs"][0]["status"] == "blocked"
    assert data["packs"][0]["blocked_reason"] == "adapter_unavailable"
