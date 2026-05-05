import pytest

from app.schemas.run import UnavailableInclude


def test_unavailable_include_round_trip() -> None:
    item = UnavailableInclude(include="capabilities", reason="device_offline")
    dumped = item.model_dump()
    assert dumped == {"include": "capabilities", "reason": "device_offline"}
    rebuilt = UnavailableInclude.model_validate(dumped)
    assert rebuilt == item


def test_unavailable_include_requires_both_fields() -> None:
    with pytest.raises(ValueError):
        UnavailableInclude(include="config")  # type: ignore[call-arg]
