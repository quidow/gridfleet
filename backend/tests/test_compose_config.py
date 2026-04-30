from __future__ import annotations

from pathlib import Path

import yaml

from app.services.settings_registry import SETTINGS_REGISTRY


def test_selenium_hub_image_matches_managed_node_jar_version() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    expected_version = SETTINGS_REGISTRY["grid.selenium_jar_version"].default

    for compose_file in ("docker-compose.yml", "docker-compose.prod.yml"):
        compose = yaml.safe_load((repo_root / "docker" / compose_file).read_text())
        image = compose["services"]["selenium-hub"]["image"]

        assert image == f"selenium/hub:{expected_version}"
