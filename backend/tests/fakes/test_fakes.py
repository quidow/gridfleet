"""Verify test fakes return expected values."""

from __future__ import annotations

from tests.fakes.settings import FakeSettingsReader


def test_fake_settings_reader_returns_defaults() -> None:
    reader = FakeSettingsReader()
    assert reader.get("any.key") == ""


def test_fake_settings_reader_returns_overrides() -> None:
    reader = FakeSettingsReader({"timeout": 30, "retries": 3})
    assert reader.get("timeout") == 30
    assert reader.get("retries") == 3
