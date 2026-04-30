from app.pack.adapter import (
    DiscoveryCandidate,
    DoctorCheckResult,
    HealthCheckResult,
    LifecycleActionResult,
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
