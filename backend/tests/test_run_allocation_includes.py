from typing import get_type_hints

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.device import Device
from app.schemas.run import ClaimResponse, ReservedDeviceInfo, UnavailableInclude
from app.services.run_service import _build_device_info
from tests.helpers import create_device

pytestmark = pytest.mark.usefixtures("seeded_driver_packs")


def test_unavailable_include_round_trip() -> None:
    item = UnavailableInclude(include="capabilities", reason="device_offline")
    dumped = item.model_dump()
    assert dumped == {"include": "capabilities", "reason": "device_offline"}
    rebuilt = UnavailableInclude.model_validate(dumped)
    assert rebuilt == item


def test_unavailable_include_requires_both_fields() -> None:
    with pytest.raises(ValueError):
        UnavailableInclude(include="config")  # type: ignore[call-arg]


def test_reserved_device_info_has_tier1_and_tier2_fields() -> None:
    hints = get_type_hints(ReservedDeviceInfo)
    for field in (
        "name",
        "device_type",
        "connection_type",
        "manufacturer",
        "model",
        "config",
        "live_capabilities",
        "unavailable_includes",
    ):
        assert field in hints, f"{field} missing from ReservedDeviceInfo"


def test_claim_response_has_tier1_and_tier2_fields() -> None:
    hints = get_type_hints(ClaimResponse)
    for field in (
        "name",
        "device_type",
        "connection_type",
        "manufacturer",
        "model",
        "config",
        "live_capabilities",
        "unavailable_includes",
    ):
        assert field in hints, f"{field} missing from ClaimResponse"


def test_reserved_device_info_construction_without_tier1_still_valid() -> None:
    info = ReservedDeviceInfo(
        device_id="d",
        identity_value="i",
        pack_id="p",
        platform_id="pl",
        os_version="1",
    )
    assert info.name is None
    assert info.device_type is None


@pytest.mark.db
@pytest.mark.asyncio
async def test_build_device_info_populates_tier1_fields(db_session: AsyncSession, default_host_id: str) -> None:
    created = await create_device(
        db_session,
        host_id=default_host_id,
        name="emu-pixel7-1",
        device_type="emulator",
        connection_type="virtual",
        manufacturer="Google",
        model="Pixel 7",
    )
    # Reload with host eagerly loaded, matching production query pattern.
    result = await db_session.execute(select(Device).where(Device.id == created.id).options(selectinload(Device.host)))
    device = result.scalar_one()
    info = _build_device_info(device, platform_label="Android 14")
    assert info.name == "emu-pixel7-1"
    assert info.device_type == "emulator"
    assert info.connection_type == "virtual"
    assert info.manufacturer == "Google"
    assert info.model == "Pixel 7"
