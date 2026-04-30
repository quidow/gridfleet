import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.device import ConnectionType, DeviceType
from app.schemas.device import DeviceVerificationCreate
from app.services.device_write import prepare_device_create_payload_async
from tests.pack.factories import seed_test_packs


@pytest.mark.asyncio
async def test_tvos_network_real_device_does_not_require_separate_ip(
    db_session: AsyncSession,
) -> None:
    await seed_test_packs(db_session)

    payload = await prepare_device_create_payload_async(
        db_session,
        DeviceVerificationCreate(
            host_id=uuid.uuid4(),
            name="Living Room",
            pack_id="appium-xcuitest",
            platform_id="tvos",
            identity_scheme="apple_udid",
            identity_scope="global",
            identity_value="a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0",
            connection_target="a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0",
            device_type=DeviceType.real_device,
            connection_type=ConnectionType.network,
            os_version="26.4",
            device_config={"wda_base_url": "http://192.168.1.5"},
        ),
    )

    assert payload["connection_type"] is ConnectionType.network
    assert payload["ip_address"] is None
    assert payload["connection_target"] == "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0"
