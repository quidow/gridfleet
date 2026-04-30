from unittest.mock import MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.device import DeviceType
from app.services.pack_start_shim import build_pack_start_payload, resolve_pack_for_device
from tests.pack.factories import seed_test_packs


def _make_device(platform_id: str, device_type: DeviceType, pack_id: str | None = None) -> MagicMock:
    """Create a minimal Device-like mock with pack identity attributes."""
    d: MagicMock = MagicMock()
    d.platform_id = platform_id
    d.device_type = device_type
    d.pack_id = pack_id
    return d


def test_resolve_pack_for_android_real_device() -> None:
    assert resolve_pack_for_device(
        _make_device("android_mobile", DeviceType.real_device, pack_id="appium-uiautomator2")
    ) == (
        "appium-uiautomator2",
        "android_mobile",
    )


def test_resolve_pack_for_android_emulator() -> None:
    assert resolve_pack_for_device(
        _make_device("android_mobile", DeviceType.emulator, pack_id="appium-uiautomator2")
    ) == (
        "appium-uiautomator2",
        "android_mobile",
    )


def test_resolve_pack_returns_pack_from_device_columns() -> None:
    # When pack_id and platform_id are set, resolve_pack_for_device returns them directly
    d = _make_device("ios", DeviceType.real_device, pack_id="appium-xcuitest")
    assert resolve_pack_for_device(d) == ("appium-xcuitest", "ios")


@pytest.mark.asyncio
async def test_build_pack_start_payload_includes_rendered_stereotype(db_session: AsyncSession) -> None:
    await seed_test_packs(db_session)
    await db_session.commit()

    device = _make_device("android_mobile", DeviceType.real_device, pack_id="appium-uiautomator2")
    payload = await build_pack_start_payload(db_session, device=device)
    assert payload is not None
    assert payload["pack_id"] == "appium-uiautomator2"
    assert payload["platform_id"] == "android_mobile"
    assert payload["stereotype_caps"]["platformName"] == "Android"
    assert payload["stereotype_caps"]["appium:automationName"] == "UiAutomator2"


@pytest.mark.asyncio
async def test_pack_start_payload_carries_insecure_features_from_manifest(db_session: AsyncSession) -> None:
    await seed_test_packs(db_session)
    await db_session.commit()

    device = _make_device("android_mobile", DeviceType.real_device, pack_id="appium-uiautomator2")
    payload = await build_pack_start_payload(db_session, device=device)
    assert payload is not None
    assert "uiautomator2:chromedriver_autodownload" in payload["insecure_features"]


@pytest.mark.asyncio
async def test_pack_start_payload_carries_grid_slots_from_manifest(db_session: AsyncSession) -> None:
    await seed_test_packs(db_session)
    await db_session.commit()

    device = _make_device("android_mobile", DeviceType.real_device, pack_id="appium-uiautomator2")
    payload = await build_pack_start_payload(db_session, device=device)
    assert payload is not None
    assert payload["grid_slots"] == ["native", "chrome"]


@pytest.mark.asyncio
async def test_pack_start_payload_grid_slots_native_only_for_xcuitest(db_session: AsyncSession) -> None:
    await seed_test_packs(db_session)
    await db_session.commit()

    device = _make_device("ios", DeviceType.real_device, pack_id="appium-xcuitest")
    payload = await build_pack_start_payload(db_session, device=device)
    assert payload is not None
    assert payload["grid_slots"] == ["native"]


@pytest.mark.asyncio
async def test_pack_start_payload_includes_launch_metadata_keys(db_session: AsyncSession) -> None:
    """build_pack_start_payload includes adapter-owned launch metadata."""
    await seed_test_packs(db_session)
    await db_session.commit()

    device = _make_device("android_mobile", DeviceType.real_device, pack_id="appium-uiautomator2")
    payload = await build_pack_start_payload(db_session, device=device)
    assert payload is not None
    assert "lifecycle_actions" in payload
    assert "connection_behavior" in payload
    # For real devices there are no lifecycle actions in the manifest.
    assert isinstance(payload["lifecycle_actions"], list)
    assert isinstance(payload["connection_behavior"], dict)


@pytest.mark.asyncio
async def test_pack_start_payload_applies_device_type_override_for_emulator(db_session: AsyncSession) -> None:
    await seed_test_packs(db_session)
    await db_session.commit()

    device = _make_device("android_mobile", DeviceType.emulator, pack_id="appium-uiautomator2")
    payload = await build_pack_start_payload(db_session, device=device)

    assert payload is not None
    action_ids = {action["id"] for action in payload["lifecycle_actions"]}
    assert "boot" in action_ids
    assert payload["connection_behavior"]["default_connection_type"] == "virtual"


@pytest.mark.asyncio
async def test_pack_start_payload_launch_metadata_for_xcuitest(db_session: AsyncSession) -> None:
    """XCUITest platform should also expose launch metadata keys."""
    await seed_test_packs(db_session)
    await db_session.commit()

    device = _make_device("ios", DeviceType.real_device, pack_id="appium-xcuitest")
    payload = await build_pack_start_payload(db_session, device=device)
    assert payload is not None
    assert "lifecycle_actions" in payload
    assert "connection_behavior" in payload
