import pytest

from app.settings.service import SettingsService
from tests.fakes import FakeSettingsReader


def test_typed_getters_return_narrowed_types() -> None:
    reader = FakeSettingsReader(
        {
            "general.session_viability_timeout_sec": 30,
            "appium_reconciler.interval_sec": 2.5,
            "general.node_fail_window_sec": True,
        }
    )
    assert reader.get_int("general.session_viability_timeout_sec") == 30
    assert reader.get_float("appium_reconciler.interval_sec") == 2.5
    assert reader.get_float("general.session_viability_timeout_sec") == 30.0  # int widens to float
    assert reader.get_bool("general.node_fail_window_sec") is True


def test_typed_getters_reject_mismatched_types() -> None:
    reader = FakeSettingsReader({"general.session_viability_timeout_sec": "30"})
    with pytest.raises(TypeError):
        reader.get_int("general.session_viability_timeout_sec")
    with pytest.raises(TypeError):
        reader.get_bool("general.session_viability_timeout_sec")


def test_settings_service_typed_getters_validate_cached_values() -> None:
    # Exercises the TypeError branches on the real implementation — required for
    # the 98% coverage gate, not just the fake. Cache-poking mirrors how the
    # conformance test constructs the service without DB initialization.
    service = SettingsService()
    service._cache["general.session_viability_timeout_sec"] = 30
    service._cache["general.node_fail_window_sec"] = True
    assert service.get_int("general.session_viability_timeout_sec") == 30
    assert service.get_float("general.session_viability_timeout_sec") == 30.0
    assert service.get_bool("general.node_fail_window_sec") is True
    with pytest.raises(TypeError):
        service.get_bool("general.session_viability_timeout_sec")
    with pytest.raises(TypeError):
        service.get_int("general.node_fail_window_sec")  # bool explicitly rejected as int
    with pytest.raises(TypeError):
        service.get_float("general.node_fail_window_sec")
