"""Verify test fakes satisfy their protocols."""

from __future__ import annotations

from app.core.protocols import SettingsReader
from tests.fakes.settings import FakeSettingsReader


def test_fake_settings_reader_returns_defaults() -> None:
    reader = FakeSettingsReader()
    assert reader.get("any.key") == ""


def test_fake_settings_reader_returns_overrides() -> None:
    reader = FakeSettingsReader({"timeout": 30, "retries": 3})
    assert reader.get("timeout") == 30
    assert reader.get("retries") == 3


def test_fake_settings_reader_satisfies_protocol() -> None:
    reader = FakeSettingsReader()
    assert isinstance(reader, SettingsReader)
