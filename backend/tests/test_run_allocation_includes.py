from typing import get_type_hints

import pytest

from app.schemas.run import ClaimResponse, ReservedDeviceInfo, UnavailableInclude


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
