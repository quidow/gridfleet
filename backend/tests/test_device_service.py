import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.device import DeviceOperationalState
from app.models.host import Host
from app.schemas.device import DeviceVerificationCreate
from app.services import device_service

pytestmark = pytest.mark.usefixtures("seeded_driver_packs")


@pytest.mark.db
@pytest.mark.asyncio
async def test_create_device_persists_initial_operational_state(db_session: AsyncSession, db_host: Host) -> None:
    data = DeviceVerificationCreate(
        identity_value="initial-state-verify",
        connection_target="initial-state-verify",
        name="Initial State Verify",
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        os_version="14",
        host_id=db_host.id,
    )

    device = await device_service.create_device(
        db_session,
        data,
        initial_operational_state=DeviceOperationalState.verifying,
    )

    assert device.operational_state is DeviceOperationalState.verifying
    assert device.verified_at is None
