from __future__ import annotations

import hashlib
import os
import platform
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import httpx

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
class HealthCheckResult:
    ok: bool
    message: str


@dataclass(frozen=True)
class RegistrationCheckResult:
    ok: bool
    message: str


@dataclass(frozen=True)
class InstallResult:
    config_env: Path
    service_file: Path
    selenium_jar: Path
    started: bool
    health: HealthCheckResult | None = None
    registration: RegistrationCheckResult | None = None


def validate_dedicated_venv(
    config: InstallConfig, *, executable: Path | None = None, command_name: str = "install"
) -> None:
    expected = Path(config.venv_bin_dir) / "gridfleet-agent"
    actual = (executable or Path(sys.argv[0])).resolve()
    if actual != expected.resolve():
        raise RuntimeError(
            f"gridfleet-agent {command_name} must run from {expected}. "
            "Create /opt/gridfleet-agent/venv first, install gridfleet-agent there, "
            f"then run /opt/gridfleet-agent/venv/bin/gridfleet-agent {command_name}."
        )


def _selenium_url(config: InstallConfig) -> str:
    version = config.selenium_version
    return f"https://github.com/SeleniumHQ/selenium/releases/download/selenium-{version}/selenium-server-{version}.jar"


def _download_selenium(url: str, dest: Path) -> None:
    fd, tmp = tempfile.mkstemp(dir=str(dest.parent), suffix=".download")
    try:
        sha256 = hashlib.sha256()
        with urllib.request.urlopen(url, timeout=60) as response, os.fdopen(fd, "wb") as output:
            while True:
                chunk = response.read(65536)
                if not chunk:
                    break
                sha256.update(chunk)
                output.write(chunk)
        os.rename(tmp, str(dest))
    except BaseException:
        os.unlink(tmp)
        raise
    print(f"Downloaded {dest.name} sha256={sha256.hexdigest()}")


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

    config_env = Path(config.config_env_path)
    config_env.write_text(render_config_env(config, discovery))
    os.chmod(config_env, 0o600)
    if resolved_os == "Linux":
        service_file.write_text(render_systemd_unit(config))
    elif resolved_os == "Darwin":
        service_file.write_text(render_launchd_plist(config, discovery))
    else:
        raise RuntimeError(f"Unsupported OS: {resolved_os}")
    os.chmod(service_file, 0o644)

    return InstallResult(
        config_env=Path(config.config_env_path),
        service_file=service_file,
        selenium_jar=selenium_jar,
        started=False,
    )


def _run_command(command: list[str]) -> None:
    result = subprocess.run(command, check=False, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit code {result.returncode}"
        raise RuntimeError(f"{' '.join(command)} failed: {detail}")


def _start_service(os_name: str, service_file: Path, *, run_command: Callable[[list[str]], None]) -> None:
    if os_name == "Linux":
        run_command(["systemctl", "daemon-reload"])
        run_command(["systemctl", "enable", "gridfleet-agent"])
        run_command(["systemctl", "start", "gridfleet-agent"])
        return
    if os_name == "Darwin":
        run_command(["launchctl", "load", str(service_file)])
        return
    raise RuntimeError(f"Unsupported OS: {os_name}")


def poll_agent_health(
    url: str,
    *,
    timeout_sec: float = 30.0,
    interval_sec: float = 1.0,
    get: Callable[..., object] = httpx.get,
) -> HealthCheckResult:
    deadline = time.monotonic() + timeout_sec
    last_error = "no response"
    while time.monotonic() <= deadline:
        try:
            response = get(url, timeout=2.0)
            status_code = getattr(response, "status_code", None)
            if status_code == 200:
                return HealthCheckResult(ok=True, message="agent health check passed")
            last_error = f"unexpected status {status_code}"
        except Exception as exc:
            last_error = str(exc)
        time.sleep(interval_sec)
    return HealthCheckResult(ok=False, message=f"agent health check timed out: {last_error}")


def _manager_hosts_url(config: InstallConfig) -> str:
    return f"{config.manager_url.rstrip('/')}/api/hosts"


def _host_list_contains(hosts: object, hostname: str) -> bool:
    if not isinstance(hosts, list):
        return False
    return any(isinstance(host, dict) and host.get("hostname") == hostname for host in hosts)


def poll_manager_registration(
    config: InstallConfig,
    *,
    hostname: str | None = None,
    timeout_sec: float = 30.0,
    interval_sec: float = 1.0,
    get: Callable[..., object] = httpx.get,
) -> RegistrationCheckResult:
    resolved_hostname = hostname or socket.gethostname()
    url = _manager_hosts_url(config)
    auth = (
        (config.manager_auth_username, config.manager_auth_password)
        if config.manager_auth_username and config.manager_auth_password
        else None
    )
    deadline = time.monotonic() + timeout_sec
    last_error = f"{resolved_hostname} was not listed"
    while time.monotonic() <= deadline:
        try:
            response = get(url, timeout=2.0, auth=auth) if auth else get(url, timeout=2.0)
            status_code = getattr(response, "status_code", None)
            if status_code == 200:
                json_body = getattr(response, "json", None)
                hosts = json_body() if callable(json_body) else None
                if _host_list_contains(hosts, resolved_hostname):
                    return RegistrationCheckResult(
                        ok=True,
                        message=f"agent registered with manager as {resolved_hostname}",
                    )
                last_error = f"{resolved_hostname} was not listed"
            else:
                last_error = f"unexpected status {status_code}"
        except Exception as exc:
            last_error = str(exc)
        time.sleep(interval_sec)
    return RegistrationCheckResult(ok=False, message=f"agent registration pending: {last_error}")


def install_with_start(
    config: InstallConfig,
    discovery: ToolDiscovery,
    *,
    os_name: str | None = None,
    executable: Path | None = None,
    download: Callable[[str, Path], None] = _download_selenium,
    run_command: Callable[[list[str]], None] = _run_command,
    health_check: Callable[[str], HealthCheckResult] = poll_agent_health,
    registration_check: Callable[[InstallConfig], RegistrationCheckResult] = poll_manager_registration,
) -> InstallResult:
    resolved_os = os_name or platform.system()
    result = install_no_start(
        config,
        discovery,
        os_name=resolved_os,
        executable=executable,
        download=download,
    )
    _start_service(resolved_os, result.service_file, run_command=run_command)
    health = health_check(f"http://localhost:{config.port}/agent/health")
    registration = registration_check(config) if health.ok else None
    return InstallResult(
        config_env=result.config_env,
        service_file=result.service_file,
        selenium_jar=result.selenium_jar,
        started=True,
        health=health,
        registration=registration,
    )
