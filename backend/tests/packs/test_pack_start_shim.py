from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.devices.models import DeviceType
from app.packs.services.start_shim import (
    PackStartPayloadError,
    _ensure_session_discovery,
    build_pack_start_payload,
    resolve_pack_for_device,
)
from tests.packs.factories import seed_test_packs

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


def _has_session_discovery(features: list[str]) -> bool:
    return any(f == "session_discovery" or f.endswith(":session_discovery") for f in features)


def test_ensure_session_discovery_injects_when_missing() -> None:
    # C10: a pack manifest that omits session_discovery must still get it so grid
    # orphan reaping works.
    out = _ensure_session_discovery(["uiautomator2:chromedriver_autodownload"], pack_id="some-pack")
    assert _has_session_discovery(out)
    assert "uiautomator2:chromedriver_autodownload" in out


def test_ensure_session_discovery_leaves_existing_entry_untouched() -> None:
    existing = ["uiautomator2:session_discovery"]
    assert _ensure_session_discovery(existing, pack_id="some-pack") == existing
    bare = ["session_discovery"]
    assert _ensure_session_discovery(bare, pack_id="some-pack") == bare


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
    assert payload["appium_platform_name"] == "Android"


@pytest.mark.asyncio
async def test_pack_start_payload_carries_insecure_features_from_manifest(db_session: AsyncSession) -> None:
    await seed_test_packs(db_session)
    await db_session.commit()

    device = _make_device("android_mobile", DeviceType.real_device, pack_id="appium-uiautomator2")
    payload = await build_pack_start_payload(db_session, device=device)
    assert payload is not None
    assert "uiautomator2:chromedriver_autodownload" in payload["insecure_features"]


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


@pytest.mark.asyncio
async def test_pack_start_payload_returns_none_without_pack_identity(db_session: AsyncSession) -> None:
    device = _make_device("android_mobile", DeviceType.real_device, pack_id=None)
    assert await build_pack_start_payload(db_session, device=device) is None


@pytest.mark.asyncio
async def test_pack_start_payload_wraps_platform_and_stereotype_lookup_errors(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = _make_device("missing", DeviceType.real_device, pack_id="missing-pack")
    device.id = __import__("uuid").uuid4()
    monkeypatch.setattr("app.packs.services.start_shim.resolve_pack_platform", AsyncMock(side_effect=LookupError))
    with pytest.raises(PackStartPayloadError, match="not available"):
        await build_pack_start_payload(db_session, device=device)

    monkeypatch.setattr("app.packs.services.start_shim.resolve_pack_platform", AsyncMock(return_value=MagicMock()))
    monkeypatch.setattr("app.packs.services.start_shim.render_stereotype", AsyncMock(side_effect=LookupError))
    with pytest.raises(PackStartPayloadError, match="not available"):
        await build_pack_start_payload(db_session, device=device)
