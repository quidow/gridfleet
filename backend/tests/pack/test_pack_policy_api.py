import pytest
from httpx import AsyncClient

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("seeded_driver_packs")]


async def test_catalog_includes_runtime_policy(client: AsyncClient) -> None:
    response = await client.get("/api/driver-packs/catalog")

    assert response.status_code == 200
    pack = response.json()["packs"][0]
    assert pack["runtime_policy"] == {
        "strategy": "recommended",
        "appium_server_version": None,
        "appium_driver_version": None,
    }


async def test_patch_runtime_policy_exact(client: AsyncClient) -> None:
    response = await client.patch(
        "/api/driver-packs/appium-uiautomator2/policy",
        json={
            "runtime_policy": {
                "strategy": "exact",
                "appium_server_version": "2.11.5",
                "appium_driver_version": "3.6.0",
            }
        },
    )

    assert response.status_code == 200
    assert response.json()["runtime_policy"]["strategy"] == "exact"
    assert response.json()["runtime_policy"]["appium_server_version"] == "2.11.5"
    assert response.json()["runtime_policy"]["appium_driver_version"] == "3.6.0"


async def test_patch_runtime_policy_rejects_incomplete_exact(client: AsyncClient) -> None:
    response = await client.patch(
        "/api/driver-packs/appium-uiautomator2/policy",
        json={"runtime_policy": {"strategy": "exact", "appium_server_version": "2.11.5"}},
    )

    assert response.status_code == 422
