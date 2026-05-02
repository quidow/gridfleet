from __future__ import annotations

import os
import platform
from dataclasses import dataclass
from typing import TYPE_CHECKING

from agent_app.installer.install import HealthCheckResult, _run_command, poll_agent_health, validate_dedicated_venv

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from agent_app.installer.plan import InstallConfig


@dataclass(frozen=True)
class UpdateResult:
    to_version: str | None
    restarted: bool
    health: HealthCheckResult


def _agent_package_spec(to_version: str | None) -> str:
    return f"gridfleet-agent=={to_version}" if to_version else "gridfleet-agent"


def _pip_upgrade_command(config: InstallConfig, to_version: str | None) -> list[str]:
    return [
        f"{config.agent_dir}/venv/bin/python",
        "-m",
        "pip",
        "install",
        "--upgrade",
        _agent_package_spec(to_version),
    ]


def _restart_command(os_name: str, *, uid: int | None = None) -> list[str]:
    if os_name == "Linux":
        return ["systemctl", "restart", "gridfleet-agent"]
    if os_name == "Darwin":
        resolved_uid = os.getuid() if uid is None else uid
        return ["launchctl", "kickstart", "-k", f"gui/{resolved_uid}/com.gridfleet.agent"]
    raise RuntimeError(f"Unsupported OS: {os_name}")


def _health_url(config: InstallConfig) -> str:
    return f"http://localhost:{config.port}/agent/health"


def format_update_dry_run(
    config: InstallConfig,
    *,
    to_version: str | None = None,
    os_name: str | None = None,
    uid: int | None = None,
) -> str:
    resolved_os = os_name or platform.system()
    pip_command = " ".join(_pip_upgrade_command(config, to_version))
    restart_command = " ".join(_restart_command(resolved_os, uid=uid))

    return f"""GridFleet Agent update dry run

Actions:
  - Upgrade package: {pip_command}
  - Restart service: {restart_command}
  - Poll local health: {_health_url(config)}
"""


def update_agent(
    config: InstallConfig,
    *,
    to_version: str | None = None,
    os_name: str | None = None,
    executable: Path | None = None,
    run_command: Callable[[list[str]], None] = _run_command,
    health_check: Callable[[str], HealthCheckResult] = poll_agent_health,
    uid: int | None = None,
) -> UpdateResult:
    validate_dedicated_venv(config, executable=executable, command_name="update")
    resolved_os = os_name or platform.system()

    run_command(_pip_upgrade_command(config, to_version))
    run_command(_restart_command(resolved_os, uid=uid))
    health = health_check(_health_url(config))

    return UpdateResult(to_version=to_version, restarted=True, health=health)
