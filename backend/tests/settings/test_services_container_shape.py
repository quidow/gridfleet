"""Shape guards on per-domain service containers."""

from __future__ import annotations

import dataclasses

from app.settings.services_container import SettingsServices


def test_settings_services_has_no_reader_field() -> None:
    field_names = {f.name for f in dataclasses.fields(SettingsServices)}
    assert "reader" not in field_names
    assert "service" in field_names
