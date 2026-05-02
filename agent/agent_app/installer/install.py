from __future__ import annotations

import platform
import shutil
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from agent_app.installer.plan import (
    InstallConfig,
    ToolDiscovery,
    render_config_env,
    render_launchd_plist,
    render_systemd_unit,
)

if TYPE_CHECKING:
    from collections.abc import Callable


@dataclass(frozen=True)
class InstallResult:
    config_env: Path
    service_file: Path
    selenium_jar: Path
    started: bool


def validate_dedicated_venv(config: InstallConfig, *, executable: Path | None = None) -> None:
    expected = Path(config.venv_bin_dir) / "gridfleet-agent"
    actual = (executable or Path(sys.argv[0])).resolve()
    if actual != expected.resolve():
        raise RuntimeError(
            f"gridfleet-agent install must run from {expected}. "
            "Create /opt/gridfleet-agent/venv first, install gridfleet-agent there, "
            "then run /opt/gridfleet-agent/venv/bin/gridfleet-agent install."
        )


def _selenium_url(config: InstallConfig) -> str:
    version = config.selenium_version
    return f"https://github.com/SeleniumHQ/selenium/releases/download/selenium-{version}/selenium-server-{version}.jar"


def _download_selenium(url: str, dest: Path) -> None:
    with urllib.request.urlopen(url, timeout=60) as response, dest.open("wb") as output:
        shutil.copyfileobj(response, output)


def _service_file_path(config: InstallConfig, os_name: str) -> Path:
    if os_name == "Linux":
        if config.config_dir.startswith("/etc/"):
            root = Path(config.config_dir).parents[1]
            return root / "systemd/system/gridfleet-agent.service"
        return Path(config.config_dir).parent / "systemd/system/gridfleet-agent.service"
    if os_name == "Darwin":
        agent_path = Path(config.agent_dir)
        root = agent_path.parents[1] if len(agent_path.parents) > 1 else Path.home()
        return root / "Library/LaunchAgents/com.gridfleet.agent.plist"
    raise RuntimeError(f"Unsupported OS: {os_name}")


def install_no_start(
    config: InstallConfig,
    discovery: ToolDiscovery,
    *,
    os_name: str | None = None,
    executable: Path | None = None,
    download: Callable[[str, Path], None] = _download_selenium,
    start: bool = False,
) -> InstallResult:
    if start:
        raise NotImplementedError("service start is not implemented in this installer slice")

    validate_dedicated_venv(config, executable=executable)
    resolved_os = os_name or platform.system()
    agent_dir = Path(config.agent_dir)
    config_dir = Path(config.config_dir)
    runtime_dir = agent_dir / "runtimes"
    selenium_jar = Path(config.selenium_jar)
    service_file = _service_file_path(config, resolved_os)

    runtime_dir.mkdir(parents=True, exist_ok=True)
    config_dir.mkdir(parents=True, exist_ok=True)
    service_file.parent.mkdir(parents=True, exist_ok=True)

    if not selenium_jar.exists():
        download(_selenium_url(config), selenium_jar)

    Path(config.config_env_path).write_text(render_config_env(config, discovery))
    if resolved_os == "Linux":
        service_file.write_text(render_systemd_unit(config))
    elif resolved_os == "Darwin":
        service_file.write_text(render_launchd_plist(config, discovery))
    else:
        raise RuntimeError(f"Unsupported OS: {resolved_os}")

    return InstallResult(
        config_env=Path(config.config_env_path),
        service_file=service_file,
        selenium_jar=selenium_jar,
        started=False,
    )
