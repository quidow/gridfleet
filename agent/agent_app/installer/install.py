from __future__ import annotations

import contextlib
import os
import platform
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

import httpx

from agent_app.installer.plan import (
    InstallConfig,
    ToolDiscovery,
    _xdg_config_home,
    render_config_env,
    render_launchd_plist,
    render_systemd_unit,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from agent_app.installer.identity import OperatorIdentity


@dataclass(frozen=True)
class HealthCheckResult:
    ok: bool
    message: str
    details: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class RegistrationCheckResult:
    ok: bool
    message: str


class HealthCheckCallable(Protocol):
    def __call__(self, url: str, *, auth: tuple[str, str] | None = None) -> HealthCheckResult:
        raise NotImplementedError


@dataclass(frozen=True)
class InstallResult:
    config_env: Path
    service_file: Path
    started: bool
    health: HealthCheckResult | None = None
    registration: RegistrationCheckResult | None = None


def resolve_bin_path(*, executable: Path | None = None) -> str:
    raw = executable or Path(sys.argv[0])
    if not raw.is_absolute():
        which_result = shutil.which(str(raw))
        if which_result:
            raw = Path(which_result)
    return str(raw.resolve())


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


def _resolve_uid(uid: int | None = None) -> int:
    if uid is not None:
        return uid
    sudo_uid = os.environ.get("SUDO_UID")
    if sudo_uid and sudo_uid.isdecimal():
        return int(sudo_uid)
    return os.getuid()


def _service_file_path(config: InstallConfig, os_name: str, operator: OperatorIdentity | None = None) -> Path:
    del operator  # SUDO_USER fallbacks gone; operator is the calling user.
    if os_name == "Linux":
        return _xdg_config_home() / "systemd/user/gridfleet-agent.service"
    if os_name == "Darwin":
        return Path.home() / "Library/LaunchAgents/com.gridfleet.agent.plist"
    raise RuntimeError(f"Unsupported OS: {os_name}")


def install_no_start(
    config: InstallConfig,
    discovery: ToolDiscovery,
    *,
    operator: OperatorIdentity,
    os_name: str | None = None,
    executable: Path | None = None,
    download: Callable[[str, Path], None] | None = None,
    start: bool = False,
) -> InstallResult:
    del download
    if start:
        raise NotImplementedError("service start is not implemented in this installer slice")

    if not config.bin_path:
        resolved = resolve_bin_path(executable=executable)
        config = InstallConfig(
            **{f.name: getattr(config, f.name) for f in fields(config) if f.name != "bin_path"},
            bin_path=resolved,
        )
    resolved_os = os_name or platform.system()
    agent_dir = Path(config.agent_dir)
    config_dir = Path(config.config_dir)
    runtime_dir = agent_dir / "runtimes"
    service_file = _service_file_path(config, resolved_os, operator)

    runtime_dir.mkdir(parents=True, exist_ok=True)
    config_dir.mkdir(parents=True, exist_ok=True)
    service_file.parent.mkdir(parents=True, exist_ok=True)

    config_env = Path(config.config_env_path)
    config_env.write_text(render_config_env(config, discovery))
    os.chmod(config_env, 0o600)
    if resolved_os == "Linux":
        service_file.write_text(render_systemd_unit(config))
    elif resolved_os == "Darwin":
        (Path.home() / "Library/Logs/gridfleet-agent").mkdir(parents=True, exist_ok=True)
        service_file.write_text(render_launchd_plist(config, discovery))
    else:
        raise RuntimeError(f"Unsupported OS: {resolved_os}")
    os.chmod(service_file, 0o600)

    return InstallResult(
        config_env=Path(config.config_env_path),
        service_file=service_file,
        started=False,
    )


def _run_command(command: list[str], *, timeout: float | None = 30) -> None:
    try:
        result = subprocess.run(command, check=False, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"{' '.join(command)} timed out after {timeout}s") from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit code {result.returncode}"
        raise RuntimeError(f"{' '.join(command)} failed: {detail}")


def _start_service(
    os_name: str,
    service_file: Path,
    *,
    run_command: Callable[[list[str]], None],
) -> None:
    if os_name == "Linux":
        run_command(["systemctl", "--user", "daemon-reload"])
        run_command(["systemctl", "--user", "enable", "gridfleet-agent"])
        run_command(["systemctl", "--user", "start", "gridfleet-agent"])
        return
    if os_name == "Darwin":
        uid = os.getuid()
        domain_target = f"gui/{uid}"
        with contextlib.suppress(RuntimeError):
            run_command(["launchctl", "bootout", f"{domain_target}/com.gridfleet.agent"])
        run_command(["launchctl", "bootstrap", domain_target, str(service_file)])
        return
    raise RuntimeError(f"Unsupported OS: {os_name}")


def poll_agent_health(
    url: str,
    *,
    timeout_sec: float = 30.0,
    interval_sec: float = 1.0,
    get: Callable[..., object] = httpx.get,
    auth: tuple[str, str] | None = None,
) -> HealthCheckResult:
    deadline = time.monotonic() + timeout_sec
    last_error = "no response"
    while time.monotonic() <= deadline:
        try:
            response = get(url, timeout=2.0, auth=auth) if auth else get(url, timeout=2.0)
            status_code = getattr(response, "status_code", None)
            if status_code == 200:
                json_body = getattr(response, "json", None)
                details = json_body() if callable(json_body) else {}
                return HealthCheckResult(
                    ok=True,
                    message="agent health check passed",
                    details=details if isinstance(details, dict) else {},
                )
            last_error = "agent rejected credentials" if status_code == 401 else f"unexpected status {status_code}"
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
                if status_code == 401:
                    last_error = (
                        "manager requires machine auth; rerun install with "
                        "--manager-auth-username and --manager-auth-password matching "
                        "GRIDFLEET_MACHINE_AUTH_USERNAME and GRIDFLEET_MACHINE_AUTH_PASSWORD"
                    )
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
    operator: OperatorIdentity,
    os_name: str | None = None,
    executable: Path | None = None,
    download: Callable[[str, Path], None] | None = None,
    run_command: Callable[[list[str]], None] = _run_command,
    health_check: HealthCheckCallable = poll_agent_health,
    registration_check: Callable[[InstallConfig], RegistrationCheckResult] = poll_manager_registration,
) -> InstallResult:
    resolved_os = os_name or platform.system()
    result = install_no_start(
        config,
        discovery,
        operator=operator,
        os_name=resolved_os,
        executable=executable,
        download=download,
    )
    _start_service(resolved_os, result.service_file, run_command=run_command)

    api_auth = (
        (config.api_auth_username, config.api_auth_password)
        if config.api_auth_username and config.api_auth_password
        else None
    )

    health_url = f"http://localhost:{config.port}/agent/health"
    health = health_check(health_url, auth=api_auth) if api_auth else health_check(health_url)
    registration = registration_check(config) if health.ok else None
    return InstallResult(
        config_env=result.config_env,
        service_file=result.service_file,
        started=True,
        health=health,
        registration=registration,
    )
