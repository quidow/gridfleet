import pytest

from app.core.leader import settings_provider


def test_get_raises_before_provider_is_registered() -> None:
    settings_provider.reset_for_tests()

    with pytest.raises(RuntimeError, match="Leader settings provider not registered"):
        settings_provider.get("general.leader_keepalive_enabled")


def test_register_routes_get_to_provider() -> None:
    settings_provider.reset_for_tests()
    settings_provider.register_settings_provider(lambda key: {"general.leader_keepalive_enabled": True}[key])

    assert settings_provider.get("general.leader_keepalive_enabled") is True


def test_reset_clears_registered_provider() -> None:
    settings_provider.register_settings_provider(lambda _key: True)
    settings_provider.reset_for_tests()

    with pytest.raises(RuntimeError, match="Leader settings provider not registered"):
        settings_provider.get("general.leader_keepalive_enabled")
