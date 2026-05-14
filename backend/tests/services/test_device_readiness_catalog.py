from unittest.mock import MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.devices.services.readiness import assess_device_async
from app.packs.models import DriverPack, DriverPackPlatform, DriverPackRelease
from tests.pack.factories import seed_test_packs


def _make_device(
    *,
    pack_id: str | None = None,
    platform_id: str | None = None,
    ip_address: str | None = None,
    device_config: dict | None = None,
    connection_type: str | None = None,
    device_type: str | None = None,
    verified_at: object = None,
) -> MagicMock:
    device = MagicMock()
    device.pack_id = pack_id
    device.platform_id = platform_id
    device.ip_address = ip_address
    device.device_config = device_config
    device.connection_type = connection_type
    device.device_type = device_type
    device.verified_at = verified_at
    return device


async def _seed_roku_pack(session: AsyncSession) -> None:
    pack = DriverPack(
        id="appium-roku-dlenroc",
        origin="uploaded",
        display_name="Roku (dlenroc)",
        maintainer="community",
        license="MIT",
        state="disabled",
    )
    session.add(pack)
    await session.flush()

    release = DriverPackRelease(
        pack_id="appium-roku-dlenroc",
        release="2026.04.0",
        manifest_json={},
    )
    session.add(release)
    await session.flush()

    session.add(
        DriverPackPlatform(
            pack_release_id=release.id,
            manifest_platform_id="roku_network",
            display_name="Roku (network)",
            automation_name="Roku",
            appium_platform_name="roku",
            device_types=["real_device"],
            connection_types=["network"],
            grid_slots=["native"],
            data={
                "device_fields_schema": [
                    {
                        "id": "roku_password",
                        "label": "Developer password",
                        "type": "string",
                        "required_for_session": True,
                        "sensitive": True,
                        "capability_name": "appium:password",
                    },
                ],
                "identity": {"scheme": "roku_serial", "scope": "global"},
            },
        )
    )
    await session.flush()


@pytest.mark.asyncio
async def test_roku_missing_password_setup_required(db_session: AsyncSession) -> None:
    await _seed_roku_pack(db_session)
    device = _make_device(
        pack_id="appium-roku-dlenroc",
        platform_id="roku_network",
        ip_address="192.168.1.100",
        device_config={},
    )
    result = await assess_device_async(db_session, device)
    assert result.readiness_state == "setup_required"
    assert "roku_password" in result.missing_setup_fields


@pytest.mark.asyncio
async def test_roku_with_password_verification_required(db_session: AsyncSession) -> None:
    await _seed_roku_pack(db_session)
    device = _make_device(
        pack_id="appium-roku-dlenroc",
        platform_id="roku_network",
        ip_address="192.168.1.100",
        device_config={"roku_password": "secret"},
    )
    result = await assess_device_async(db_session, device)
    assert result.readiness_state == "verification_required"


@pytest.mark.asyncio
async def test_unknown_pack_setup_required(db_session: AsyncSession) -> None:
    device = _make_device(
        pack_id="nonexistent-pack",
        platform_id="unknown_platform",
    )
    result = await assess_device_async(db_session, device)
    assert result.readiness_state == "setup_required"
    assert "driver_pack" in result.missing_setup_fields


@pytest.mark.asyncio
async def test_no_pack_fallback_to_payload(db_session: AsyncSession) -> None:
    device = _make_device(
        platform_id="android_mobile",
        connection_type="usb",
    )
    result = await assess_device_async(db_session, device)
    assert result.readiness_state in {"setup_required", "verification_required"}


@pytest.mark.asyncio
async def test_tvos_real_device_requires_wda_base_url(db_session: AsyncSession) -> None:
    await seed_test_packs(db_session)
    await db_session.flush()
    device = _make_device(
        pack_id="appium-xcuitest",
        platform_id="tvos",
        ip_address="10.0.0.42",
        device_type="real_device",
        device_config={"use_preinstalled_wda": True},
    )
    result = await assess_device_async(db_session, device)
    assert result.readiness_state == "setup_required"
    assert result.missing_setup_fields == ["wda_base_url", "updated_wda_bundle_id"]
