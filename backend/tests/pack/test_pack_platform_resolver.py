import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import PackDisabledError, PackUnavailableError, PlatformRemovedError
from app.models.driver_pack import DriverPack
from app.services.pack_platform_resolver import (
    PackPlatformNotFound,
    ResolvedPackPlatform,
    assert_runnable,
    resolve_pack_platform,
)
from tests.pack.factories import seed_test_packs


@pytest.mark.asyncio
async def test_resolve_pack_platform_returns_latest_enabled_release(db_session: AsyncSession) -> None:
    await seed_test_packs(db_session)
    await db_session.flush()

    resolved = await resolve_pack_platform(
        db_session,
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
    )

    assert isinstance(resolved, ResolvedPackPlatform)
    assert resolved.pack_id == "appium-uiautomator2"
    assert resolved.release == "2026.04.0"
    assert resolved.platform_id == "android_mobile"
    assert resolved.identity_scheme == "android_serial"
    assert resolved.identity_scope == "host"
    assert resolved.display_name == "Android"


@pytest.mark.asyncio
async def test_resolve_pack_platform_raises_for_missing_platform(db_session: AsyncSession) -> None:
    await seed_test_packs(db_session)
    await db_session.flush()

    with pytest.raises(PackPlatformNotFound):
        await resolve_pack_platform(
            db_session,
            pack_id="appium-uiautomator2",
            platform_id="not_in_manifest",
        )


@pytest.mark.asyncio
async def test_assert_runnable_returns_resolved_platform_when_enabled(db_session: AsyncSession) -> None:
    await seed_test_packs(db_session)
    await db_session.flush()
    resolved = await assert_runnable(
        db_session,
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
    )
    assert resolved.pack_id == "appium-uiautomator2"
    assert resolved.platform_id == "android_mobile"


@pytest.mark.asyncio
async def test_assert_runnable_raises_pack_unavailable_when_pack_missing(db_session: AsyncSession) -> None:
    await seed_test_packs(db_session)
    await db_session.flush()
    with pytest.raises(PackUnavailableError):
        await assert_runnable(
            db_session,
            pack_id="appium-roku",
            platform_id="roku_network",
        )


@pytest.mark.asyncio
async def test_assert_runnable_raises_pack_disabled_distinct_from_missing(db_session: AsyncSession) -> None:
    await seed_test_packs(db_session)
    pack = await db_session.scalar(select(DriverPack).where(DriverPack.id == "appium-uiautomator2"))
    pack.state = "disabled"
    await db_session.flush()
    with pytest.raises(PackDisabledError):
        await assert_runnable(
            db_session,
            pack_id="appium-uiautomator2",
            platform_id="android_mobile",
        )


@pytest.mark.asyncio
async def test_assert_runnable_raises_platform_removed_when_pack_enabled_but_platform_absent(
    db_session: AsyncSession,
) -> None:
    await seed_test_packs(db_session)
    await db_session.flush()
    with pytest.raises(PlatformRemovedError):
        await assert_runnable(
            db_session,
            pack_id="appium-uiautomator2",
            platform_id="never_existed",
        )
