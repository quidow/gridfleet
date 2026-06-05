from __future__ import annotations

from datetime import UTC, datetime

from app.devices.models import Device
from app.devices.services import state_write_guard
from app.devices.services.state_derivation import device_in_service


def _device(*, verified: bool = True, maintenance: bool = False, review: bool = False) -> Device:
    with state_write_guard.bypass():
        device = Device(
            name="d",
            verified_at=datetime.now(UTC) if verified else None,
            review_required=review,
            lifecycle_policy_state={"maintenance_reason": "operator"} if maintenance else {},
        )
    return device


def test_in_service_when_verified_no_maintenance_no_review() -> None:
    assert device_in_service(_device()) is True


def test_unverified_device_is_withdrawn() -> None:
    assert device_in_service(_device(verified=False)) is False


def test_maintenance_device_is_withdrawn() -> None:
    assert device_in_service(_device(maintenance=True)) is False


def test_review_required_device_is_withdrawn() -> None:
    assert device_in_service(_device(review=True)) is False
