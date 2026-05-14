from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from agent_app.config import agent_settings
from agent_app.main import _build_adapter_loader
from agent_app.pack.adapter_registry import AdapterRegistry
from agent_app.pack.manifest import AppiumInstallable, DesiredPack, DesiredPlatform
from agent_app.pack.runtime import RuntimeEnv

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


@pytest.fixture(autouse=True)
def clear_manager_auth(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(agent_settings, "manager_auth_username", None)
    monkeypatch.setattr(agent_settings, "manager_auth_password", None)
    yield


@pytest.mark.asyncio
async def test_adapter_tarball_download_uses_manager_basic_auth(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(agent_settings, "manager_auth_username", "machine")
    monkeypatch.setattr(agent_settings, "manager_auth_password", "machine-secret")
    payload = b"adapter-tarball"
    seen_requests: list[httpx.Request] = []
    original_async_client = httpx.AsyncClient

    async def handler(request: httpx.Request) -> httpx.Response:
        seen_requests.append(request)
        assert request.headers["authorization"].startswith("Basic ")
        return httpx.Response(200, content=payload, request=request)

    def client_factory(**kwargs: object) -> httpx.AsyncClient:
        return original_async_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client_factory)
    loader = _build_adapter_loader("http://manager.local", AdapterRegistry())
    with patch("agent_app.lifespan.load_adapter", new_callable=AsyncMock) as load_adapter:
        await loader(_desired_pack(hashlib.sha256(payload).hexdigest()), _runtime_env(tmp_path))

    assert len(seen_requests) == 1
    assert seen_requests[0].url.path == "/api/driver-packs/appium-roku-dlenroc/releases/2026.04.0/tarball"
    load_adapter.assert_awaited_once()


def _desired_pack(tarball_sha256: str) -> DesiredPack:
    return DesiredPack(
        id="appium-roku-dlenroc",
        release="2026.04.0",
        appium_server=AppiumInstallable("npm", "appium", "3.3.1", "3.3.1", []),
        appium_driver=AppiumInstallable("github", "@dlenroc/appium-roku-driver", "0.13.3", "0.13.3", []),
        platforms=[
            DesiredPlatform(
                id="roku_network",
                automation_name="Roku",
                device_types=["real_device"],
                connection_types=["network"],
                grid_slots=["native"],
                identity_scheme="roku_serial",
                identity_scope="global",
                stereotype={},
                appium_platform_name="roku",
            )
        ],
        tarball_sha256=tarball_sha256,
    )


def _runtime_env(tmp_path: Path) -> RuntimeEnv:
    return RuntimeEnv(
        runtime_id="runtime-1",
        appium_home=str(tmp_path),
        appium_bin="/tmp/appium",
        server_package="appium",
        server_version="3.3.1",
        driver_versions={"@dlenroc/appium-roku-driver": "0.13.3"},
        plugin_statuses=[],
    )
