from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from tests.test_devices_api import _create_device

if TYPE_CHECKING:
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession


@pytest.mark.db
@pytest.mark.asyncio
async def test_device_search_matches_model_and_identity_tokens(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    await _create_device(
        db_session,
        default_host_id,
        name="Lab Pixel",
        identity_value="usb-pixel-8-pro",
        connection_target="usb-pixel-8-pro",
    )
    await _create_device(
        db_session,
        default_host_id,
        name="Kitchen Roku",
        identity_value="roku-living-room",
        connection_target="roku-living-room",
    )
    await db_session.commit()

    response = await client.get("/api/devices", params={"search": "pixel pro"})

    assert response.status_code == 200
    names = [item["name"] for item in response.json()]
    assert names == ["Lab Pixel"]
