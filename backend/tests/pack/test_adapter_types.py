from types import SimpleNamespace

import pytest

from app.pack.adapter import (
    DiscoveryCandidate,
    DoctorCheckResult,
    DriverPackAdapter,
    HealthCheckResult,
    LifecycleActionResult,
    SessionOutcome,
    SessionSpec,
)


def test_discovery_candidate_roundtrip() -> None:
    c = DiscoveryCandidate(
        identity_scheme="android_serial",
        identity_value="emulator-5554",
        suggested_name="Pixel 6",
        detected_properties={"os_version": "14"},
        runnable=True,
        missing_requirements=[],
        field_errors=[],
        feature_status=[],
    )
    assert c.identity_value == "emulator-5554"
    assert c.runnable is True


def test_health_check_result_ok() -> None:
    r = HealthCheckResult(check_id="adb_connected", ok=True, detail="")
    assert r.ok


def test_lifecycle_action_result_shape() -> None:
    r = LifecycleActionResult(ok=True, state="online", detail="")
    assert r.state == "online"


def test_doctor_check_result_shape() -> None:
    r = DoctorCheckResult(check_id="x", ok=False, message="tool missing")
    assert not r.ok


async def test_driver_pack_adapter_protocol_default_methods_raise() -> None:
    adapter = object()
    with pytest.raises(NotImplementedError):
        await DriverPackAdapter.discover(adapter, SimpleNamespace(host_id="host", platform_id="android"))
    with pytest.raises(NotImplementedError):
        await DriverPackAdapter.doctor(adapter, SimpleNamespace(host_id="host"))
    with pytest.raises(NotImplementedError):
        await DriverPackAdapter.health_check(
            adapter,
            SimpleNamespace(
                device_identity_value="device",
                allow_boot=False,
                platform_id="android",
                device_type="real_device",
                connection_type="usb",
            ),
        )
    with pytest.raises(NotImplementedError):
        await DriverPackAdapter.feature_action(
            adapter,
            "camera",
            "reset",
            {},
            SimpleNamespace(host_id="host", device_identity_value="d"),
        )
    with pytest.raises(NotImplementedError):
        await DriverPackAdapter.lifecycle_action(
            adapter,
            "state",
            {},
            SimpleNamespace(host_id="host", device_identity_value="d"),
        )
    with pytest.raises(NotImplementedError):
        await DriverPackAdapter.pre_session(
            adapter,
            SessionSpec(pack_id="pack", platform_id="android", device_identity_value="device"),
        )
    with pytest.raises(NotImplementedError):
        await DriverPackAdapter.post_session(
            adapter,
            SessionSpec(pack_id="pack", platform_id="android", device_identity_value="device"),
            SessionOutcome(ok=True, detail="ok"),
        )
    with pytest.raises(NotImplementedError):
        await DriverPackAdapter.sidecar_lifecycle(adapter, "video", "status")
    with pytest.raises(NotImplementedError):
        await DriverPackAdapter.normalize_device(
            adapter,
            SimpleNamespace(host_id="host", platform_id="android", raw_input={}),
        )
    with pytest.raises(NotImplementedError):
        await DriverPackAdapter.telemetry(adapter, SimpleNamespace(device_identity_value="d", connection_target="d"))
