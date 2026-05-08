from __future__ import annotations

import platform
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from agent_app.installer.install import HealthCheckCallable, HealthCheckResult, _service_file_path, poll_agent_health

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from agent_app.installer.identity import OperatorIdentity
    from agent_app.installer.plan import InstallConfig
    from agent_app.installer.uv_runtime import UvRuntime


SECRET_KEYS = {"AGENT_MANAGER_AUTH_PASSWORD", "AGENT_TERMINAL_TOKEN", "AGENT_API_AUTH_PASSWORD"}


@dataclass(frozen=True)
class AgentStatus:
    config_env: Path
    config_exists: bool
    config_error: str | None
    service_file: Path
    service_exists: bool
    service_active: str
    service_enabled: str
    health: HealthCheckResult
    operator: OperatorIdentity
    uv: UvRuntime
    env: dict[str, str] = field(default_factory=dict)


def _parse_config_env_with_error(path: Path) -> tuple[dict[str, str], str | None]:
    if not path.exists():
        return {}, None
    values: dict[str, str] = {}
    try:
        raw_lines = path.read_text().splitlines()
    except OSError as exc:
        return {}, str(exc)
    for raw_line in raw_lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key] = value
    return values, None


def parse_config_env(path: Path) -> dict[str, str]:
    values, _error = _parse_config_env_with_error(path)
    return values


def _run_status_command(command: list[str]) -> str:
    try:
        result = subprocess.run(command, check=False, capture_output=True, text=True, timeout=10)
    except FileNotFoundError as exc:
        return f"{command[0]} unavailable: {exc}"
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"{command[0]} failed: {exc}"
    if result.returncode != 0:
        return result.stderr.strip() or result.stdout.strip() or f"exit code {result.returncode}"
    return result.stdout.strip()


def _service_state(os_name: str, *, run_command: Callable[[list[str]], str]) -> tuple[str, str]:
    if os_name == "Linux":
        return (
            run_command(["systemctl", "is-active", "gridfleet-agent"]).strip(),
            run_command(["systemctl", "is-enabled", "gridfleet-agent"]).strip(),
        )
    if os_name == "Darwin":
        state = run_command(["launchctl", "list", "com.gridfleet.agent"]).strip()
        return (state or "not loaded", "launchd")
    return (f"unsupported OS: {os_name}", "unknown")


def _status_health_check(url: str, *, auth: tuple[str, str] | None = None) -> HealthCheckResult:
    return poll_agent_health(url, timeout_sec=2.0, interval_sec=2.0, auth=auth)


def collect_status(
    config: InstallConfig,
    *,
    operator: OperatorIdentity,
    uv_runtime: UvRuntime,
    os_name: str | None = None,
    env: Mapping[str, str] | None = None,
    run_command: Callable[[list[str]], str] = _run_status_command,
    health_check: HealthCheckCallable = _status_health_check,
) -> AgentStatus:
    resolved_os = os_name or platform.system()
    config_env = Path(config.config_env_path)
    config_error = None
    if env is not None:
        parsed_env = dict(env)
    else:
        parsed_env, config_error = _parse_config_env_with_error(config_env)
    service_file = _service_file_path(config, resolved_os, operator)
    service_active, service_enabled = _service_state(resolved_os, run_command=run_command)

    api_auth_username = parsed_env.get("AGENT_API_AUTH_USERNAME") or config.api_auth_username
    api_auth_password = parsed_env.get("AGENT_API_AUTH_PASSWORD") or config.api_auth_password
    api_auth = (api_auth_username, api_auth_password) if api_auth_username and api_auth_password else None

    if config_error:
        health = HealthCheckResult(ok=False, message="config.env unreadable; health check skipped")
    elif config_env.exists() or env is not None:
        raw_port = parsed_env.get("AGENT_AGENT_PORT", str(config.port))
        try:
            port = int(raw_port)
        except ValueError:
            port = config.port
        url = f"http://localhost:{port}/agent/health"
        health = health_check(url, auth=api_auth) if api_auth else health_check(url)
    else:
        health = HealthCheckResult(ok=False, message="config.env missing; health check skipped")

    return AgentStatus(
        config_env=config_env,
        config_exists=config_env.exists() or env is not None,
        config_error=config_error,
        service_file=service_file,
        service_exists=service_file.exists(),
        service_active=service_active,
        service_enabled=service_enabled,
        health=health,
        operator=operator,
        uv=uv_runtime,
        env=parsed_env,
    )


def _format_env(env: Mapping[str, str]) -> list[str]:
    lines: list[str] = []
    for key in sorted(env):
        value = "<redacted>" if key in SECRET_KEYS else env[key]
        lines.append(f"  {key}={value}")
    return lines or ["  none"]


def _format_version_guidance(details: Mapping[str, object]) -> list[str]:
    raw = details.get("version_guidance")
    if not isinstance(raw, dict):
        return ["Agent version guidance: unavailable"]
    status = raw.get("agent_version_status")
    recommended = raw.get("recommended_agent_version")
    required = raw.get("required_agent_version")
    guidance_line = (
        f"Agent version guidance: installed version is {status}"
        if isinstance(status, str)
        else "Agent version guidance: unavailable"
    )
    lines = [guidance_line]
    if isinstance(recommended, str) and recommended:
        lines.append(f"Recommended agent version: {recommended}")
    if isinstance(required, str) and required:
        lines.append(f"Minimum supported agent version: {required}")
    return lines


def _format_uv(uv: UvRuntime) -> str:
    if uv.bin_path is not None:
        return f"uv path: {uv.bin_path}"
    searched_str = ", ".join(repr(s) for s in uv.searched)
    return f"uv path: not found; searched: [{searched_str}]"


def format_status(status: AgentStatus) -> str:
    health_state = "ok" if status.health.ok else "failed"
    if status.config_error:
        config_read = f"failed - {status.config_error}"
    elif status.config_exists:
        config_read = "ok"
    else:
        config_read = "skipped - config.env missing"
    op = status.operator
    lines = [
        "GridFleet Agent status",
        "",
        f"Config file: {status.config_env} ({'present' if status.config_exists else 'missing'})",
        f"Config read: {config_read}",
        f"Service file: {status.service_file} ({'present' if status.service_exists else 'missing'})",
        f"Service active: {status.service_active}",
        f"Service enabled: {status.service_enabled}",
        f"Local health: {health_state} - {status.health.message}",
        *_format_version_guidance(status.health.details),
        "",
        f"Operator: {op.login} (uid {op.uid}, home {op.home})",
        _format_uv(status.uv),
        "",
        "Configured environment:",
        *_format_env(status.env),
    ]
    return "\n".join(lines)
