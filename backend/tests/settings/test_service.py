from __future__ import annotations

from tests.conftest import settings_service

# ── float validation tests (device_checks.ip_ping.timeout_sec: min=0.5, max=30.0) ──

_FLOAT_KEY = "device_checks.ip_ping.timeout_sec"


def test_validate_float_setting_accepts_valid_float() -> None:
    assert settings_service._validate_value(_FLOAT_KEY, 2.0) is None


def test_validate_float_setting_accepts_int_as_float() -> None:
    # An int (non-bool) is a valid float value
    assert settings_service._validate_value(_FLOAT_KEY, 5) is None


def test_validate_float_setting_rejects_non_numeric_string() -> None:
    error = settings_service._validate_value(_FLOAT_KEY, "fast")
    assert error is not None
    assert "Expected float" in error


def test_validate_float_setting_rejects_bool() -> None:
    error = settings_service._validate_value(_FLOAT_KEY, True)
    assert error is not None
    assert "Expected float" in error


def test_validate_float_setting_rejects_below_min() -> None:
    error = settings_service._validate_value(_FLOAT_KEY, 0.1)
    assert error is not None
    assert "below minimum" in error


def test_validate_float_setting_rejects_above_max() -> None:
    error = settings_service._validate_value(_FLOAT_KEY, 60.0)
    assert error is not None
    assert "exceeds maximum" in error


def test_validate_float_setting_rejects_nan() -> None:
    error = settings_service._validate_value(_FLOAT_KEY, float("nan"))
    assert error is not None
    assert "finite" in error


def test_validate_float_setting_rejects_positive_infinity() -> None:
    error = settings_service._validate_value(_FLOAT_KEY, float("inf"))
    assert error is not None
    assert "finite" in error


def test_validate_float_setting_rejects_negative_infinity() -> None:
    error = settings_service._validate_value(_FLOAT_KEY, float("-inf"))
    assert error is not None
    assert "finite" in error
