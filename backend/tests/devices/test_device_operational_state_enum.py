from app.devices.models import DeviceOperationalState


def test_verifying_is_a_valid_operational_state() -> None:
    assert DeviceOperationalState("verifying") is DeviceOperationalState.verifying
    assert DeviceOperationalState.verifying.value == "verifying"
