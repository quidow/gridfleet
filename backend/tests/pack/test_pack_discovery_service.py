import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.host import Host
from app.services import pack_discovery_service
from app.services.pack_discovery_service import (
    PackDiscoveredCandidate,
    discover_pack_candidates,
    refresh_device_properties,
)
from tests.helpers import create_device_record
from tests.pack.factories import seed_test_packs


class _FakeAgentClient:
    async def get_pack_devices(self, host: str, port: int) -> dict:  # type: ignore[type-arg]
        return {
            "candidates": [
                {
                    "pack_id": "appium-uiautomator2",
                    "platform_id": "android_mobile",
                    "identity_scheme": "android_serial",
                    "identity_scope": "host",
                    "identity_value": "ABCD1234",
                    "suggested_name": "Pixel 6",
                    "detected_properties": {"os_version": "14"},
                    "runnable": True,
                    "missing_requirements": [],
                }
            ],
        }


@pytest.mark.asyncio
async def test_discover_pack_candidates_returns_typed_rows() -> None:
    result = await discover_pack_candidates(_FakeAgentClient(), host="h.local", port=5100)
    assert len(result.candidates) == 1
    c = result.candidates[0]
    assert isinstance(c, PackDiscoveredCandidate)
    assert c.pack_id == "appium-uiautomator2"
    assert c.platform_id == "android_mobile"
    assert c.identity_scheme == "android_serial"
    assert c.identity_scope == "host"
    assert c.identity_value == "ABCD1234"
    assert c.runnable is True
    assert not hasattr(c, "platform")
    assert not hasattr(c, "identity_kind")


@pytest.mark.asyncio
async def test_list_intake_candidates_uses_pack_devices_endpoint(db_session: AsyncSession, db_host: Host) -> None:
    await seed_test_packs(db_session)
    await db_session.commit()

    async def fake_get_pack_devices(host: str, port: int) -> dict[str, object]:
        return {
            "candidates": [
                {
                    "pack_id": "appium-uiautomator2",
                    "platform_id": "android_mobile",
                    "identity_scheme": "android_serial",
                    "identity_scope": "host",
                    "identity_value": "emulator-5554",
                    "suggested_name": "Pixel 6",
                    "detected_properties": {"model": "Pixel 6", "os_version": "14"},
                    "runnable": True,
                }
            ],
        }

    candidates = await pack_discovery_service.list_intake_candidates(
        db_session, db_host, agent_get_pack_devices=fake_get_pack_devices
    )
    assert len(candidates) == 1
    assert candidates[0].pack_id == "appium-uiautomator2"
    assert candidates[0].platform_id == "android_mobile"
    assert candidates[0].already_registered is False


@pytest.mark.asyncio
async def test_refresh_device_properties_updates_pack_device_rows(db_session: AsyncSession, db_host: Host) -> None:
    device = await create_device_record(
        db_session,
        host_id=db_host.id,
        identity_value="stable-serial",
        connection_target="old-target",
        connection_type="network",
        ip_address="10.0.0.24",
        name="Refresh Device",
        os_version="14",
        availability_status="available",
    )

    async def fake_get_properties(host: str, port: int, connection_target: str, pack_id: str) -> dict[str, object]:
        assert host == db_host.ip
        assert port == db_host.agent_port
        assert connection_target == "old-target"
        assert pack_id == device.pack_id
        return {
            "detected_properties": {
                "os_version": "15",
                "connection_target": "new-target",
                "connection_type": "network",
                "ip_address": "10.0.0.25",
            }
        }

    await refresh_device_properties(
        db_session,
        device,
        agent_get_pack_device_properties=fake_get_properties,
    )

    await db_session.refresh(device)
    assert device.os_version == "15"
    assert device.connection_target == "old-target"
    assert str(device.connection_type) == "network"
    assert device.ip_address == "10.0.0.24"


@pytest.mark.asyncio
async def test_refresh_device_properties_preserves_registered_identity_and_descriptors(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    device = await create_device_record(
        db_session,
        host_id=db_host.id,
        identity_value="G070VM2011740KW1",
        connection_target="192.168.1.254:5555",
        name="AFTMM",
        manufacturer="Amazon",
        model="AFTMM",
        os_version="7.1.2",
        availability_status="available",
    )

    async def fake_get_properties(host: str, port: int, connection_target: str, pack_id: str) -> dict[str, object]:
        return {
            "detected_properties": {
                "os_version": "6.0",
                "manufacturer": "Amazon",
                "model": "Fire TV Stick 4K",
                "model_number": "AFTMM",
                "software_versions": {
                    "fire_os": "Fire OS 6.7.1.1",
                    "fire_os_compat": "6.0",
                    "android": "7.1.2",
                    "build": "NS6711",
                },
            }
        }

    await refresh_device_properties(
        db_session,
        device,
        agent_get_pack_device_properties=fake_get_properties,
    )

    await db_session.refresh(device)
    assert device.name == "AFTMM"
    assert device.os_version == "6.0"
    assert device.manufacturer == "Amazon"
    assert device.model == "AFTMM"
    assert device.model_number is None
    assert device.software_versions == {
        "fire_os": "Fire OS 6.7.1.1",
        "fire_os_compat": "6.0",
        "android": "7.1.2",
        "build": "NS6711",
    }
