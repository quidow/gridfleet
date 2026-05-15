from __future__ import annotations

import contextlib
import getpass
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
class PathShimResult:
    path: Path
    created: bool
    warning: str | None = None


@dataclass(frozen=True)
class InstallResult:
    config_env: Path
    service_file: Path
    started: bool
    health: HealthCheckResult | None = None
    registration: RegistrationCheckResult | None = None
    linger_warning: str | None = None
    path_shim: PathShimResult | None = None


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
    expected = Path(config.resolved_bin_path)
    actual = (executable or Path(sys.argv[0])).resolve()
    if actual != expected.resolve():
        raise RuntimeError(
            f"gridfleet-agent {command_name} must run from {expected}. "
            f"Create {Path(config.agent_dir)}/venv first, install gridfleet-agent there, "
            f"then run {expected} {command_name}."
        )


_LEGACY_PATHS: tuple[Path, ...] = (
    Path("/opt/gridfleet-agent"),
    Path("/etc/gridfleet-agent/config.env"),
    Path("/etc/systemd/system/gridfleet-agent.service"),
)

_LEGACY_UNINSTALL_URL = "https://raw.githubusercontent.com/quidow/gridfleet/main/scripts/uninstall-legacy-agent.sh"


def detect_legacy_install() -> Path | None:
    """Return the first legacy artefact path that exists, or None."""
    for candidate in _LEGACY_PATHS:
        if candidate.exists():
            return candidate
    return None


class LegacyInstallDetectedError(RuntimeError):
    """Raised when /opt or /etc artefacts from a pre-user-scope install remain."""

    def __init__(self, path: Path) -> None:
        message = (
            f"Legacy root-scope install detected at {path}. "
            f"Run `curl -LsSf {_LEGACY_UNINSTALL_URL} | sudo sh` once to remove the root-owned files, "
            "then re-run this installer without sudo."
        )
        super().__init__(message)
        self.path = path


def path_shim_location(operator: OperatorIdentity) -> Path:
    return operator.home / ".local" / "bin" / "gridfleet-agent"


def ensure_path_shim(config: InstallConfig, operator: OperatorIdentity) -> PathShimResult:
    link = path_shim_location(operator)
    target = Path(config.resolved_bin_path)
    link.parent.mkdir(parents=True, exist_ok=True)
    if link.is_symlink():
        current_target = Path(os.readlink(link))
        if current_target == target:
            return PathShimResult(path=link, created=True)
        link.unlink()
    elif link.exists():
        return PathShimResult(
            path=link,
            created=False,
            warning=(
                f"{link} already exists and is not a symlink; leaving it untouched. "
                "Remove it manually if you want `gridfleet-agent` on PATH."
            ),
        )
    link.symlink_to(target)
    return PathShimResult(path=link, created=True)


def remove_path_shim(config: InstallConfig, operator: OperatorIdentity) -> bool:
    link = path_shim_location(operator)
    if not link.is_symlink():
        return False
    target = os.readlink(link)
    agent_prefix = str(Path(config.agent_dir)) + os.sep
    if target == config.resolved_bin_path or target.startswith(agent_prefix):
        link.unlink()
        return True
    return False


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

    legacy = detect_legacy_install()
    if legacy is not None:
        raise LegacyInstallDetectedError(legacy)

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

    shim = ensure_path_shim(config, operator)

    return InstallResult(
        config_env=Path(config.config_env_path),
        service_file=service_file,
        started=False,
        path_shim=shim,
    )


def _run_command(command: list[str], *, timeout: float | None = 30) -> None:
    try:
        result = subprocess.run(command, check=False, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"{' '.join(command)} timed out after {timeout}s") from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit code {result.returncode}"
        raise RuntimeError(f"{' '.join(command)} failed: {detail}")


def check_linger(*, run_command: Callable[[list[str]], str] | None = None) -> str | None:
    """Probe `loginctl show-user --property=Linger` for the current user.

    Returns a warning string if linger is off or unknown, None if linger is on.
    Never raises — this is best-effort; missing loginctl on non-systemd boxes is fine.
    """
    user = getpass.getuser()
    cmd = ["loginctl", "show-user", user, "--property=Linger"]
    try:
        if run_command is None:
            result = subprocess.run(cmd, check=False, capture_output=True, text=True, timeout=5)
            output = result.stdout.strip()
        else:
            output = run_command(cmd).strip()
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return None  # loginctl missing — not a systemd host.
    if output == "Linger=yes":
        return None
    return f"WARNING: user-instance linger is off. For headless hosts, run: sudo loginctl enable-linger {user}"


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
    linger_check: Callable[[], str | None] = check_linger,
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

    linger_warning = linger_check() if resolved_os == "Linux" else None

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
        linger_warning=linger_warning,
        path_shim=result.path_shim,
    )
