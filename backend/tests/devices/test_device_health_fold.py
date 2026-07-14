from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from app.devices.models import Device
from tests.helpers import seed_host_with_devices

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.usefixtures("seeded_driver_packs")


async def test_device_has_device_health_fold_receipt_columns(db_session: AsyncSession) -> None:
    _host, devices = await seed_host_with_devices(db_session, count=1, identity_prefix="fold-cols")
    device = devices[0]
    # Defaults match the AppiumNode receipt columns.
    assert device.device_checks_fold_applied_revision == 0
    assert device.device_checks_fold_boot_id is None
    assert device.device_checks_fold_section_sequence is None
    reloaded = await db_session.get(Device, device.id)
    assert reloaded is not None
    assert hasattr(reloaded, "device_checks_fold_applied_revision")
