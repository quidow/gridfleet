from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from app.config import settings as process_settings
from app.services.pack_template_service import TemplateDescriptor

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from httpx import AsyncClient

pytestmark = pytest.mark.asyncio


@pytest.fixture
def auth_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, str]]:
    values = {
        "auth_username": "operator",
        "auth_password": "operator-secret",
        "auth_session_secret": "session-secret-for-tests",
        "machine_auth_username": "machine",
        "machine_auth_password": "machine-secret",
    }
    monkeypatch.setattr(process_settings, "auth_enabled", True)
    monkeypatch.setattr(process_settings, "auth_username", values["auth_username"])
    monkeypatch.setattr(process_settings, "auth_password", values["auth_password"])
    monkeypatch.setattr(process_settings, "auth_session_secret", values["auth_session_secret"])
    monkeypatch.setattr(process_settings, "auth_session_ttl_sec", 28_800)
    monkeypatch.setattr(process_settings, "auth_cookie_secure", False)
    monkeypatch.setattr(process_settings, "machine_auth_username", values["machine_auth_username"])
    monkeypatch.setattr(process_settings, "machine_auth_password", values["machine_auth_password"])
    yield values


async def test_list_templates_contains_shipped_entries(client: AsyncClient) -> None:
    response = await client.get("/api/driver-packs/templates")
    assert response.status_code == 200
    templates = response.json()["templates"]
    ids = {template["template_id"] for template in templates}
    assert "appium-uiautomator2-android-real" in ids
    assert "appium-xcuitest-ios-real" in ids
    assert all("source_pack_id" in template for template in templates)


async def test_list_templates_anonymous_rejected_when_auth_enabled(
    client: AsyncClient,
    auth_settings: dict[str, str],
) -> None:
    response = await client.get("/api/driver-packs/templates")
    assert response.status_code in (401, 403)


async def test_create_from_template_returns_201(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(process_settings, "driver_pack_storage_dir", tmp_path)
    response = await client.post(
        "/api/driver-packs/from-template/appium-uiautomator2-android-real",
        json={"pack_id": "vendor/my-android-pack", "release": "1.0.0"},
    )
    assert response.status_code == 201
    body = response.json()
    assert body["id"] == "vendor/my-android-pack"
    assert body["state"] == "enabled"
    assert "origin" not in body


async def test_create_from_template_with_display_name_override(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(process_settings, "driver_pack_storage_dir", tmp_path)
    response = await client.post(
        "/api/driver-packs/from-template/appium-uiautomator2-android-real",
        json={"pack_id": "vendor/my-android-named", "release": "1.0.0", "display_name": "Custom Name"},
    )
    assert response.status_code == 201
    assert response.json()["display_name"] == "Custom Name"


async def test_create_from_template_unknown_template_returns_404(client: AsyncClient) -> None:
    response = await client.post(
        "/api/driver-packs/from-template/no-such-template",
        json={"pack_id": "vendor/some-pack", "release": "1.0.0"},
    )
    assert response.status_code == 404


async def test_create_from_template_collision_returns_409(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(process_settings, "driver_pack_storage_dir", tmp_path)
    payload = {"pack_id": "vendor/collision-pack", "release": "1.0.0"}
    first = await client.post("/api/driver-packs/from-template/appium-uiautomator2-android-real", json=payload)
    assert first.status_code == 201
    second = await client.post("/api/driver-packs/from-template/appium-uiautomator2-android-real", json=payload)
    assert second.status_code == 409


async def test_create_from_template_anonymous_rejected_when_auth_enabled(
    client: AsyncClient,
    auth_settings: dict[str, str],
) -> None:
    response = await client.post(
        "/api/driver-packs/from-template/appium-uiautomator2-android-real",
        json={"pack_id": "vendor/auth-test-pack", "release": "1.0.0"},
    )
    assert response.status_code in (401, 403)


async def test_template_route_uses_descriptor_source_pack_id(client: AsyncClient) -> None:
    fake_descriptor = TemplateDescriptor(
        id="appium-uiautomator2-android-real",
        display_name="Test",
        target_driver_summary="Android Real",
        source_pack_id="appium-uiautomator2",
        prerequisite_host_tools=["adb"],
    )

    with patch("app.routers.driver_pack_templates.list_templates") as mock_list:
        mock_list.return_value = [fake_descriptor]
        response = await client.get("/api/driver-packs/templates")
    assert response.status_code == 200
    assert response.json()["templates"][0]["source_pack_id"] == "appium-uiautomator2"
