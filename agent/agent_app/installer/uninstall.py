from __future__ import annotations

import platform
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from agent_app.installer.install import _service_file_path

if TYPE_CHECKING:
    from collections.abc import Callable

    from agent_app.installer.plan import InstallConfig


@dataclass(frozen=True)
class UninstallResult:
    service_file: Path
    removed_service_file: bool
    removed_agent_dir: bool
    removed_config_dir: bool


def _run_command(command: list[str], *, check: bool = True) -> None:
    result = subprocess.run(command, check=False, capture_output=True, text=True, timeout=30)
    if check and result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit code {result.returncode}"
        raise RuntimeError(f"{' '.join(command)} failed: {detail}")


def _stop_service(os_name: str, service_file: Path, *, run_command: Callable[..., None]) -> None:
    if os_name == "Linux":
        run_command(["systemctl", "stop", "gridfleet-agent"], check=False)
        run_command(["systemctl", "disable", "gridfleet-agent"], check=False)
        return
    if os_name == "Darwin":
        run_command(["launchctl", "unload", str(service_file)], check=False)
        return
    raise RuntimeError(f"Unsupported OS: {os_name}")


def uninstall(
    config: InstallConfig,
    *,
    os_name: str | None = None,
    run_command: Callable[..., None] = _run_command,
    remove_agent_dir: bool = True,
    remove_config_dir: bool = True,
) -> UninstallResult:
    resolved_os = os_name or platform.system()
    service_file = _service_file_path(config, resolved_os)
    agent_dir = Path(config.agent_dir)
    config_dir = Path(config.config_dir)

    _stop_service(resolved_os, service_file, run_command=run_command)

    removed_service_file = False
    if service_file.exists():
        service_file.unlink()
        removed_service_file = True

    if resolved_os == "Linux":
        run_command(["systemctl", "daemon-reload"], check=True)

    removed_agent_dir = False
    if remove_agent_dir and agent_dir.exists():
        shutil.rmtree(agent_dir)
        removed_agent_dir = True

    removed_config_dir = False
    if remove_config_dir and config_dir.exists():
        shutil.rmtree(config_dir)
        removed_config_dir = True

    return UninstallResult(
        service_file=service_file,
        removed_service_file=removed_service_file,
        removed_agent_dir=removed_agent_dir,
        removed_config_dir=removed_config_dir,
    )
