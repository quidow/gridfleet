from __future__ import annotations

import platform
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from agent_app.installer.install import LegacyInstallDetectedError, _service_file_path, detect_legacy_install

if TYPE_CHECKING:
    from collections.abc import Callable

    from agent_app.installer.identity import OperatorIdentity
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


def _stop_service(
    os_name: str,
    service_file: Path,
    *,
    run_command: Callable[..., None],
    operator: OperatorIdentity,
) -> None:
    if os_name == "Linux":
        run_command(["systemctl", "--user", "stop", "gridfleet-agent"], check=False)
        run_command(["systemctl", "--user", "disable", "gridfleet-agent"], check=False)
        return
    if os_name == "Darwin":
        # Use domain-target form so bootout still removes the registered service when the
        # plist file is missing (e.g. half-uninstalled host where the plist was rm'd manually).
        run_command(["launchctl", "bootout", f"gui/{operator.uid}/com.gridfleet.agent"], check=False)
        return
    raise RuntimeError(f"Unsupported OS: {os_name}")


def uninstall(
    config: InstallConfig,
    *,
    operator: OperatorIdentity,
    os_name: str | None = None,
    run_command: Callable[..., None] = _run_command,
    remove_agent_dir: bool = True,
    remove_config_dir: bool = True,
) -> UninstallResult:
    resolved_os = os_name or platform.system()
    legacy = detect_legacy_install()
    if legacy is not None:
        raise LegacyInstallDetectedError(legacy)
    service_file = _service_file_path(config, resolved_os, operator)
    agent_dir = Path(config.agent_dir)
    config_dir = Path(config.config_dir)

    _stop_service(resolved_os, service_file, run_command=run_command, operator=operator)

    removed_service_file = False
    if service_file.exists():
        service_file.unlink()
        removed_service_file = True

    if resolved_os == "Linux":
        run_command(["systemctl", "--user", "daemon-reload"], check=True)

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
