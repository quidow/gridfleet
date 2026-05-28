"""Verify Protocol definitions are structurally sound."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.core.protocols import SettingsReader
    from app.core.type_defs import SettingValue


class _FakeSettingsReader:
    def get(self, key: str) -> SettingValue:
        return ""


def test_fake_settings_reader_satisfies_protocol() -> None:
    reader: SettingsReader = _FakeSettingsReader()
    assert reader.get("x") == ""
