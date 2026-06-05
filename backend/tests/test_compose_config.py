from __future__ import annotations

from pathlib import Path

import yaml


def test_router_builds_from_router_dockerfile() -> None:
    repo_root = Path(__file__).resolve().parents[2]

    for compose_file in ("docker-compose.yml", "docker-compose.prod.yml"):
        compose = yaml.safe_load((repo_root / "docker" / compose_file).read_text())
        build = compose["services"]["router"]["build"]

        assert build["dockerfile"] == "router/Dockerfile"


def test_host_docker_internal_is_resolvable_by_manager_and_router() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    expected_host = "host.docker.internal:host-gateway"

    for compose_file in ("docker-compose.yml", "docker-compose.prod.yml"):
        compose = yaml.safe_load((repo_root / "docker" / compose_file).read_text())

        for service_name in ("backend", "router"):
            service = compose["services"][service_name]

            assert expected_host in service.get("extra_hosts", [])
