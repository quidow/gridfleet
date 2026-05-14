from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from app.config import settings as process_settings

if TYPE_CHECKING:
    from collections.abc import Iterator

    from httpx import AsyncClient

pytestmark = pytest.mark.asyncio


@pytest.fixture
def auth_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(process_settings, "auth_enabled", True)
    monkeypatch.setattr(process_settings, "auth_username", "operator")
    monkeypatch.setattr(process_settings, "auth_password", "operator-secret")
    monkeypatch.setattr(process_settings, "auth_session_secret", "session-secret-for-tests-pad-to-32-bytes")
    monkeypatch.setattr(process_settings, "auth_session_ttl_sec", 28_800)
    monkeypatch.setattr(process_settings, "auth_cookie_secure", False)
    monkeypatch.setattr(process_settings, "machine_auth_username", "machine")
    monkeypatch.setattr(process_settings, "machine_auth_password", "machine-secret")
    yield


async def test_anonymous_cannot_toggle_pack_state(client: AsyncClient, auth_settings: None) -> None:
    res = await client.patch("/api/driver-packs/appium-uiautomator2", json={"state": "disabled"})
    assert res.status_code in (401, 403)


async def test_anonymous_cannot_update_runtime_policy(client: AsyncClient, auth_settings: None) -> None:
    res = await client.patch(
        "/api/driver-packs/appium-uiautomator2/policy",
        json={"runtime_policy": {"strategy": "latest_patch"}},
    )
    assert res.status_code in (401, 403)
