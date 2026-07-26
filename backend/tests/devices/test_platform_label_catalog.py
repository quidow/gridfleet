from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from app.devices.services.platform_label import load_platform_label_map, platform_labels_from_catalog
from app.packs.models import PackState
from app.packs.services.catalog_view import PackPlatformView, PackReleaseView, PackView, load_pack_catalog

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

ANDROID_PACK = "appium-uiautomator2"
ROKU_PACK = "appium-roku-dlenroc"


def _platform(platform_id: str, display_name: str) -> PackPlatformView:
    return PackPlatformView(
        manifest_platform_id=platform_id,
        display_name=display_name,
        automation_name="UiAutomator2",
        appium_platform_name="Android",
        data={},
    )


def _pack(*releases: PackReleaseView, current_release: str | None) -> dict[str, PackView]:
    return {"pack": PackView(id="pack", state=PackState.enabled, current_release=current_release, releases=releases)}


def test_labels_come_from_the_pinned_release() -> None:
    packs = _pack(
        PackReleaseView(release="1.0.0", platforms=(_platform("android", "Pinned Android"),)),
        PackReleaseView(release="2.0.0", platforms=(_platform("android", "Latest Android"),)),
        current_release="1.0.0",
    )

    assert platform_labels_from_catalog(packs) == {("pack", "android"): "Pinned Android"}


def test_labels_fall_back_to_the_latest_release_when_none_is_pinned() -> None:
    packs = _pack(
        PackReleaseView(release="1.0.0", platforms=(_platform("android", "Pinned Android"),)),
        PackReleaseView(release="2.0.0", platforms=(_platform("android", "Latest Android"),)),
        current_release=None,
    )

    assert platform_labels_from_catalog(packs) == {("pack", "android"): "Latest Android"}


def test_a_pack_with_no_releases_contributes_no_labels() -> None:
    assert platform_labels_from_catalog(_pack(current_release=None)) == {}


@pytest.mark.db
@pytest.mark.usefixtures("seeded_driver_packs")
async def test_catalog_labels_match_the_query_backed_map(db_session: AsyncSession) -> None:
    """Parity, pair for pair, with the three-statement map this replaces.

    ``load_platform_label_map`` keeps five other callers with no catalog in hand,
    so the two must not drift: the derived map is only a legal substitute at the
    device-list call site while it answers every pair identically, including the
    misses.
    """
    pairs = [
        (ANDROID_PACK, "android_mobile"),
        (ANDROID_PACK, "android_tv"),
        (ANDROID_PACK, "no_such_platform"),
        (ROKU_PACK, "roku_network"),
        ("ghost-pack", "android_mobile"),
    ]
    catalog = await load_pack_catalog(db_session, [ANDROID_PACK, ROKU_PACK, "ghost-pack"])
    expected = await load_platform_label_map(db_session, pairs)

    derived = platform_labels_from_catalog(catalog)

    assert {pair: derived.get(pair) for pair in pairs} == expected
    assert expected[(ANDROID_PACK, "android_mobile")] == "Android"
