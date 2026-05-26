"""Verify SettingsService satisfies the SettingsReader protocol."""

from __future__ import annotations

from app.core.protocols import SettingsReader
from app.settings.service import SettingsService


def test_settings_service_satisfies_reader_protocol() -> None:
    service = SettingsService()
    assert isinstance(service, SettingsReader)
