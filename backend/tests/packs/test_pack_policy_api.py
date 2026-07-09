from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from httpx2 import AsyncClient

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("seeded_driver_packs")]


async def test_catalog_includes_runtime_policy(client: AsyncClient) -> None:
    response = await client.get("/api/driver-packs/catalog")

    assert response.status_code == 200
    pack = response.json()["packs"][0]
    assert pack["runtime_policy"] == {"strategy": "recommended"}


async def test_patch_runtime_policy_recommended(client: AsyncClient) -> None:
    response = await client.patch(
        "/api/driver-packs/appium-uiautomator2/policy",
        json={"runtime_policy": {"strategy": "recommended"}},
    )

    assert response.status_code == 200
    assert response.json()["runtime_policy"] == {"strategy": "recommended"}


async def test_policy_rejects_removed_strategies(client: AsyncClient) -> None:
    response = await client.patch(
        "/api/driver-packs/appium-uiautomator2/policy",
        json={"runtime_policy": {"strategy": "latest_patch"}},
    )

    assert response.status_code == 422
