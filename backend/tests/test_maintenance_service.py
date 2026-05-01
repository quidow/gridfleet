import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.device import DeviceAvailabilityStatus
from app.models.host import Host
from app.services.maintenance_service import enter_maintenance
from tests.helpers import create_device

pytestmark = pytest.mark.asyncio


async def test_enter_maintenance_rejects_reserved_device_by_default(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="reserved-target",
        availability_status=DeviceAvailabilityStatus.reserved,
    )
    await db_session.commit()

    with pytest.raises(ValueError) as exc:
        await enter_maintenance(db_session, device)

    assert "reserved" in str(exc.value).lower()
    await db_session.refresh(device)
    assert device.availability_status == DeviceAvailabilityStatus.reserved


async def test_enter_maintenance_allows_reserved_when_explicitly_overridden(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="forced-target",
        availability_status=DeviceAvailabilityStatus.reserved,
    )
    await db_session.commit()

    result = await enter_maintenance(db_session, device, allow_reserved=True, drain=True)

    assert result.availability_status == DeviceAvailabilityStatus.maintenance


async def test_enter_maintenance_succeeds_for_available_device(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="happy-target",
        availability_status=DeviceAvailabilityStatus.available,
    )
    await db_session.commit()

    result = await enter_maintenance(db_session, device, drain=True)

    assert result.availability_status == DeviceAvailabilityStatus.maintenance
