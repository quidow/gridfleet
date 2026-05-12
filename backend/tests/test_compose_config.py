from __future__ import annotations

from pathlib import Path

import yaml


def test_selenium_hub_image_is_pinned() -> None:
    repo_root = Path(__file__).resolve().parents[2]

    for compose_file in ("docker-compose.yml", "docker-compose.prod.yml"):
        compose = yaml.safe_load((repo_root / "docker" / compose_file).read_text())
        image = compose["services"]["selenium-hub"]["image"]

        assert image == "selenium/hub:4.41.0"


def test_host_docker_internal_is_resolvable_by_manager_and_grid() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    expected_host = "host.docker.internal:host-gateway"

    for compose_file in ("docker-compose.yml", "docker-compose.prod.yml"):
        compose = yaml.safe_load((repo_root / "docker" / compose_file).read_text())

        for service_name in ("backend", "selenium-hub"):
            service = compose["services"][service_name]

            assert expected_host in service.get("extra_hosts", [])
