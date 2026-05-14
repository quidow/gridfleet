import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.devices.models import ConnectionType, Device, DeviceType
from app.devices.schemas.device import DeviceVerificationCreate, DeviceVerificationUpdate
from app.devices.services import write as device_write
from app.devices.services.write import prepare_device_create_payload_async, prepare_device_update_payload_async
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


@pytest.mark.asyncio
async def test_update_payload_logs_repr_safe_pack_platform_ids(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def missing_pack_platform(*_args: object, **_kwargs: object) -> object:
        raise LookupError

    debug_calls: list[tuple[str, tuple[object, ...]]] = []

    def capture_debug(message: str, *args: object, **_kwargs: object) -> None:
        debug_calls.append((message, args))

    monkeypatch.setattr(device_write, "resolve_pack_platform", missing_pack_platform)
    monkeypatch.setattr(device_write.logger, "debug", capture_debug)

    device = Device(
        host_id=uuid.uuid4(),
        name="Living Room",
        pack_id="appium-xcuitest",
        platform_id="tvos",
        identity_scheme="apple_udid",
        identity_scope="global",
        identity_value="udid-1",
        connection_target="udid-1",
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.network,
        ip_address="192.168.1.10",
        os_version="26.4",
    )

    await prepare_device_update_payload_async(
        db_session,
        device,
        DeviceVerificationUpdate(
            host_id=device.host_id,
            pack_id="bad\npack",
            platform_id="bad\nplatform",
        ),
    )

    assert debug_calls == [
        (
            "Pack platform not resolvable for pack=%s platform=%s",
            (repr("bad\npack"), repr("bad\nplatform")),
        )
    ]
