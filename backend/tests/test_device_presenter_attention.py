from sqlalchemy.ext.asyncio import AsyncSession

from app.models.device import ConnectionType, Device, DeviceAvailabilityStatus, DeviceType
from app.models.host import Host
from app.services import device_presenter


async def test_serialize_device_includes_needs_attention(db_session: AsyncSession, db_host: Host) -> None:
    device = Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value="ATTN-DEV-1",
        connection_target="ATTN-DEV-1",
        name="Attention Test",
        os_version="14",
        host_id=db_host.id,
        availability_status=DeviceAvailabilityStatus.offline,
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )
    db_session.add(device)
    await db_session.commit()
    await db_session.refresh(device)

    payload = await device_presenter.serialize_device(db_session, device)
    assert payload["needs_attention"] is True


async def test_serialize_device_includes_extended_device_info(db_session: AsyncSession, db_host: Host) -> None:
    device = Device(
        pack_id="appium-uiautomator2",
        platform_id="firetv_real",
        identity_scheme="android_serial",
        identity_scope="global",
        identity_value="G070VM1234567890",
        connection_target="192.168.1.99:5555",
        name="Fire TV Stick 4K",
        os_version="6.0",
        host_id=db_host.id,
        availability_status=DeviceAvailabilityStatus.available,
        manufacturer="Amazon",
        model="Fire TV Stick 4K",
        model_number="AFTMM",
        software_versions={"fire_os": "6.0", "android": "7.1.2", "build": "NS6271/2495"},
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.network,
        ip_address="192.168.1.99",
    )
    db_session.add(device)
    await db_session.commit()
    await db_session.refresh(device)

    payload = await device_presenter.serialize_device(db_session, device)

    assert payload["model"] == "Fire TV Stick 4K"
    assert payload["model_number"] == "AFTMM"
    assert payload["software_versions"] == {
        "fire_os": "6.0",
        "android": "7.1.2",
        "build": "NS6271/2495",
    }
