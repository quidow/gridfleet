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


def test_host_docker_internal_is_resolvable_by_manager_and_grid() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    expected_host = "host.docker.internal:host-gateway"

    for compose_file in ("docker-compose.yml", "docker-compose.prod.yml"):
        compose = yaml.safe_load((repo_root / "docker" / compose_file).read_text())

        for service_name in ("backend", "selenium-hub"):
            service = compose["services"][service_name]

            assert expected_host in service.get("extra_hosts", [])
