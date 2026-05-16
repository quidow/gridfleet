import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.packs.services.capability import (
    render_default_capabilities,
    render_device_field_capabilities,
    render_stereotype,
    resolve_workaround_env,
)
from app.packs.services.platform_resolver import resolve_pack_platform
from tests.pack.factories import seed_test_packs


@pytest.mark.asyncio
async def test_render_stereotype_for_uiautomator2_real(db_session: AsyncSession) -> None:
    await seed_test_packs(db_session)
    await db_session.commit()

    caps = await render_stereotype(db_session, pack_id="appium-uiautomator2", platform_id="android_mobile")
    assert caps["platformName"] == "Android"
    assert caps["appium:automationName"] == "UiAutomator2"


@pytest.mark.asyncio
async def test_render_stereotype_missing_platform_raises(db_session: AsyncSession) -> None:
    await seed_test_packs(db_session)
    await db_session.commit()

    with pytest.raises(LookupError):
        await render_stereotype(db_session, pack_id="appium-uiautomator2", platform_id="does_not_exist")


@pytest.mark.asyncio
async def test_render_stereotype_interpolates_device_context(db_session: AsyncSession) -> None:
    await seed_test_packs(db_session)
    await db_session.commit()

    caps = await render_stereotype(
        db_session,
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        device_context={"os_version": "14"},
    )
    assert caps["appium:os_version"] == "14"


@pytest.mark.asyncio
async def test_render_stereotype_drops_keys_with_missing_context(db_session: AsyncSession) -> None:
    await seed_test_packs(db_session)
    await db_session.commit()

    caps = await render_stereotype(
        db_session,
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
    )
    # Template references device.os_version which is not in the (empty) context;
    # the key is dropped, matching render_default_capabilities behaviour.
    assert "appium:os_version" not in caps
    # Renderer still emits the hard-coded platformName from the platform model.
    assert caps["platformName"] == "Android"


@pytest.mark.asyncio
async def test_tvos_real_device_wda_fields_render_to_capabilities(db_session: AsyncSession) -> None:
    await seed_test_packs(db_session)
    await db_session.flush()
    resolved = await resolve_pack_platform(
        db_session,
        pack_id="appium-xcuitest",
        platform_id="tvos",
        device_type="real_device",
    )
    default_caps = render_default_capabilities(
        resolved,
        device_context={
            "ip_address": "10.0.0.42",
            "connection_target": "tv-1",
            "identity_value": "ABC123",
            "os_version": "18.1",
        },
    )
    field_caps = render_device_field_capabilities(
        resolved,
        {
            "wda_base_url": "http://10.0.0.42",
            "use_preinstalled_wda": True,
            "updated_wda_bundle_id": "com.test.WebDriverAgentRunner",
        },
    )
    assert default_caps["appium:platformVersion"] == "18.1"
    assert "appium:wdaBaseUrl" not in default_caps
    assert "appium:updatedWDABundleId" not in default_caps
    assert field_caps["appium:wdaBaseUrl"] == "http://10.0.0.42"
    assert field_caps["appium:usePreinstalledWDA"] is True
    assert field_caps["appium:updatedWDABundleId"] == "com.test.WebDriverAgentRunner"


@pytest.mark.asyncio
async def test_tvos_wda_base_url_is_required_for_real_device_sessions(
    db_session: AsyncSession,
) -> None:
    await seed_test_packs(db_session)
    await db_session.flush()
    resolved = await resolve_pack_platform(
        db_session,
        pack_id="appium-xcuitest",
        platform_id="tvos",
        device_type="real_device",
    )
    wda_field = next(field for field in resolved.device_fields_schema if field["id"] == "wda_base_url")
    assert wda_field["required_for_session"] is True
    assert wda_field["capability_name"] == "appium:wdaBaseUrl"


@pytest.mark.asyncio
async def test_ios_real_device_wda_fields_render_to_capabilities(db_session: AsyncSession) -> None:
    await seed_test_packs(db_session)
    await db_session.flush()
    resolved = await resolve_pack_platform(
        db_session,
        pack_id="appium-xcuitest",
        platform_id="ios",
        device_type="real_device",
    )
    field_ids = {field["id"] for field in resolved.device_fields_schema}
    assert {
        "use_preinstalled_wda",
        "updated_wda_bundle_id",
        "updated_wda_bundle_id_suffix",
        "prebuilt_wda_path",
        "wda_launch_timeout",
        "xcode_org_id",
        "xcode_signing_id",
        "show_xcode_log",
    }.issubset(field_ids)

    prebuilt_wda_path = (
        "/Users/me/Library/Developer/Xcode/DerivedData/WebDriverAgent/Build/Products/"
        "Debug-iphoneos/WebDriverAgentRunner-Runner.app"
    )
    rendered = render_device_field_capabilities(
        resolved,
        {
            "use_preinstalled_wda": True,
            "updated_wda_bundle_id": "com.example.WebDriverAgentRunner",
            "updated_wda_bundle_id_suffix": "",
            "prebuilt_wda_path": prebuilt_wda_path,
            "wda_launch_timeout": 180000,
            "xcode_org_id": "TEAM12345",
            "xcode_signing_id": "Apple Development",
            "show_xcode_log": True,
        },
    )
    assert rendered == {
        "appium:usePreinstalledWDA": True,
        "appium:updatedWDABundleId": "com.example.WebDriverAgentRunner",
        "appium:updatedWDABundleIdSuffix": "",
        "appium:prebuiltWDAPath": prebuilt_wda_path,
        "appium:wdaLaunchTimeout": 180000,
        "appium:xcodeOrgId": "TEAM12345",
        "appium:xcodeSigningId": "Apple Development",
        "appium:showXcodeLog": True,
    }


@pytest.mark.asyncio
async def test_resolve_workaround_env_for_tvos_includes_devicectl_pref(
    db_session: AsyncSession,
) -> None:
    await seed_test_packs(db_session)
    await db_session.flush()
    env = await resolve_workaround_env(
        db_session,
        pack_id="appium-xcuitest",
        platform_id="tvos",
        device_type="real_device",
        os_version="18.0",
    )
    assert env == {"APPIUM_XCUITEST_PREFER_DEVICECTL": "1"}


@pytest.mark.asyncio
async def test_resolve_workaround_env_skips_when_device_type_mismatch(
    db_session: AsyncSession,
) -> None:
    await seed_test_packs(db_session)
    await db_session.flush()
    env = await resolve_workaround_env(
        db_session,
        pack_id="appium-xcuitest",
        platform_id="tvos",
        device_type="simulator",
        os_version="18.0",
    )
    assert env == {}
