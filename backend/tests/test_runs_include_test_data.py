import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.devices.models import DeviceOperationalState
from tests.helpers import create_device_record
from tests.pack.factories import seed_test_packs

pytestmark = pytest.mark.db


@pytest_asyncio.fixture(autouse=True)
async def _seed(db_session: AsyncSession) -> None:
    await seed_test_packs(db_session)
    await db_session.commit()


async def _create_run_with_include(client: AsyncClient, *, include: str | None = None) -> dict:
    params = {"include": include} if include else None
    resp = await client.post(
        "/api/runs",
        params=params,
        json={
            "name": "test-data-run",
            "requirements": [
                {
                    "pack_id": "appium-uiautomator2",
                    "platform_id": "android_mobile",
                    "count": 1,
                }
            ],
        },
    )
    assert resp.status_code == 201
    return resp.json()


async def test_reserve_with_include_test_data_returns_inline(
    client: AsyncClient, db_session: AsyncSession, default_host_id: str
) -> None:
    await create_device_record(
        db_session,
        host_id=default_host_id,
        identity_value="udid-include-1",
        name="dev-include-1",
        operational_state=DeviceOperationalState.available,
        test_data={"feature_flag": "x"},
    )
    await db_session.commit()

    run = await _create_run_with_include(client, include="test_data")
    devices = run["devices"]
    assert devices[0]["test_data"] == {"feature_flag": "x"}


async def test_reserve_without_include_omits_test_data(
    client: AsyncClient, db_session: AsyncSession, default_host_id: str
) -> None:
    await create_device_record(
        db_session,
        host_id=default_host_id,
        identity_value="udid-include-3",
        name="dev-include-3",
        operational_state=DeviceOperationalState.available,
        test_data={"k": "v"},
    )
    await db_session.commit()

    run = await _create_run_with_include(client)
    devices = run["devices"]
    assert "test_data" not in devices[0] or devices[0]["test_data"] is None
