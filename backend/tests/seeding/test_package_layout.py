"""Smoke test that the seeding package is importable and layered."""

import importlib


def test_seeding_package_importable() -> None:
    importlib.import_module("app.seeding")
    importlib.import_module("app.seeding.factories")
    importlib.import_module("app.seeding.scenarios")
