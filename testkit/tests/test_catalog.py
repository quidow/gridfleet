from __future__ import annotations

import pytest

from gridfleet_testkit.catalog import (
    _catalog_payload,
    _enabled_platform_matches,
    _required_platform_string,
    _resolve_pack_platform,
)

CATALOG = {
    "packs": [
        {
            "id": "appium-uiautomator2",
            "state": "enabled",
            "platforms": [
                {
                    "id": "android_mobile",
                    "appium_platform_name": "Android",
                    "automation_name": "UiAutomator2",
                },
                {
                    "id": "firetv_real",
                    "appium_platform_name": "Android",
                    "automation_name": "UiAutomator2",
                },
            ],
        }
    ]
}

AMBIGUOUS_CATALOG = {
    "packs": [
        {
            "id": "appium-uiautomator2",
            "state": "enabled",
            "platforms": [
                {"id": "android_mobile", "appium_platform_name": "Android", "automation_name": "UiAutomator2"}
            ],
        },
        {
            "id": "local/custom",
            "state": "enabled",
            "platforms": [{"id": "android_mobile", "appium_platform_name": "Android", "automation_name": "Custom"}],
        },
    ]
}


# --- _catalog_payload ---


def test_catalog_payload_accepts_dict():
    result = _catalog_payload(CATALOG)
    assert result == CATALOG


def test_catalog_payload_accepts_list():
    packs_list = CATALOG["packs"]
    result = _catalog_payload(packs_list)
    assert result == {"packs": packs_list}


def test_catalog_payload_accepts_callable():
    result = _catalog_payload(lambda: CATALOG)
    assert result == CATALOG


def test_catalog_payload_calls_get_driver_pack_catalog():
    class FakeClient:
        def get_driver_pack_catalog(self):
            return CATALOG

    result = _catalog_payload(FakeClient())
    assert result == CATALOG


def test_catalog_payload_rejects_invalid_type():
    with pytest.raises(ValueError, match="invalid payload"):
        _catalog_payload(42)


# --- _enabled_platform_matches ---


def test_enabled_platform_matches_finds_match():
    matches = _enabled_platform_matches(CATALOG, "android_mobile")
    assert len(matches) == 1
    pack, platform = matches[0]
    assert pack.get("id") == "appium-uiautomator2"
    assert platform.get("id") == "android_mobile"


def test_enabled_platform_matches_finds_second_platform():
    matches = _enabled_platform_matches(CATALOG, "firetv_real")
    assert len(matches) == 1
    _, platform = matches[0]
    assert platform.get("id") == "firetv_real"


def test_enabled_platform_matches_ignores_disabled_packs():
    catalog = {
        "packs": [
            {"id": "disabled-pack", "state": "disabled", "platforms": [{"id": "android_mobile"}]},
        ]
    }
    matches = _enabled_platform_matches(catalog, "android_mobile")
    assert matches == []


def test_enabled_platform_matches_finds_multiple():
    matches = _enabled_platform_matches(AMBIGUOUS_CATALOG, "android_mobile")
    assert len(matches) == 2


def test_enabled_platform_matches_rejects_missing_packs_key():
    with pytest.raises(ValueError, match="packs list"):
        _enabled_platform_matches({"not_packs": []}, "android_mobile")


# --- _required_platform_string ---


def test_required_platform_string_returns_value():
    platform = {"appium_platform_name": "Android", "automation_name": "UiAutomator2", "id": "android_mobile"}
    assert _required_platform_string(platform, "appium_platform_name") == "Android"


def test_required_platform_string_raises_on_missing_key():
    with pytest.raises(ValueError, match="missing automation_name"):
        _required_platform_string({"id": "android_mobile"}, "automation_name")


def test_required_platform_string_raises_on_empty_value():
    with pytest.raises(ValueError, match="missing id"):
        _required_platform_string({"id": ""}, "id")


# --- _resolve_pack_platform ---


def test_resolve_pack_platform_with_pack_and_platform_id():
    pack_id, platform = _resolve_pack_platform(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        catalog_client=CATALOG,
    )
    assert pack_id == "appium-uiautomator2"
    assert platform.get("appium_platform_name") == "Android"


def test_resolve_pack_platform_unambiguous_platform_id():
    pack_id, platform = _resolve_pack_platform(
        pack_id=None,
        platform_id="firetv_real",
        catalog_client=CATALOG,
    )
    assert pack_id == "appium-uiautomator2"
    assert platform.get("id") == "firetv_real"


def test_resolve_pack_platform_raises_on_missing_platform_id():
    with pytest.raises(ValueError, match="Appium options require pack_id"):
        _resolve_pack_platform(pack_id=None, platform_id=None, catalog_client=CATALOG)


def test_resolve_pack_platform_raises_on_ambiguous_platform_id():
    with pytest.raises(ValueError, match="Multiple enabled driver packs"):
        _resolve_pack_platform(pack_id=None, platform_id="android_mobile", catalog_client=AMBIGUOUS_CATALOG)


def test_resolve_pack_platform_raises_on_not_found():
    with pytest.raises(ValueError, match="was not found"):
        _resolve_pack_platform(pack_id=None, platform_id="nonexistent", catalog_client=CATALOG)


def test_resolve_pack_platform_raises_when_pack_not_found():
    with pytest.raises(ValueError, match="was not found"):
        _resolve_pack_platform(
            pack_id="nonexistent-pack",
            platform_id="android_mobile",
            catalog_client=CATALOG,
        )


def test_resolve_pack_platform_reads_env_vars(monkeypatch):
    monkeypatch.setenv("GRIDFLEET_TESTKIT_PACK_ID", "appium-uiautomator2")
    monkeypatch.setenv("GRIDFLEET_TESTKIT_PLATFORM_ID", "android_mobile")

    pack_id, platform = _resolve_pack_platform(
        pack_id=None,
        platform_id=None,
        catalog_client=CATALOG,
    )
    assert pack_id == "appium-uiautomator2"
    assert platform.get("id") == "android_mobile"
