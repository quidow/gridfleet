from __future__ import annotations

import platform
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from agent_app.installer.install import HealthCheckResult, _service_file_path, poll_agent_health

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from agent_app.installer.plan import InstallConfig


SECRET_KEYS = {"AGENT_MANAGER_AUTH_PASSWORD", "AGENT_TERMINAL_TOKEN"}


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


def collect_status(
    config: InstallConfig,
    *,
    os_name: str | None = None,
    env: Mapping[str, str] | None = None,
    run_command: Callable[[list[str]], str] = _run_status_command,
    health_check: Callable[[str], HealthCheckResult] = poll_agent_health,
) -> AgentStatus:
    resolved_os = os_name or platform.system()
    config_env = Path(config.config_env_path)
    config_error = None
    if env is not None:
        parsed_env = dict(env)
    else:
        parsed_env, config_error = _parse_config_env_with_error(config_env)
    service_file = _service_file_path(config, resolved_os)
    service_active, service_enabled = _service_state(resolved_os, run_command=run_command)

    if config_env.exists() or env is not None:
        raw_port = parsed_env.get("AGENT_AGENT_PORT", str(config.port))
        try:
            port = int(raw_port)
        except ValueError:
            port = config.port
        health = health_check(f"http://localhost:{port}/agent/health")
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
        env=parsed_env,
    )


def _format_env(env: Mapping[str, str]) -> list[str]:
    lines: list[str] = []
    for key in sorted(env):
        value = "<redacted>" if key in SECRET_KEYS else env[key]
        lines.append(f"  {key}={value}")
    return lines or ["  none"]


def format_status(status: AgentStatus) -> str:
    health_state = "ok" if status.health.ok else "failed"
    lines = [
        "GridFleet Agent status",
        "",
        f"Config file: {status.config_env} ({'present' if status.config_exists else 'missing'})",
        f"Config read: {'failed - ' + status.config_error if status.config_error else 'ok'}",
        f"Service file: {status.service_file} ({'present' if status.service_exists else 'missing'})",
        f"Service active: {status.service_active}",
        f"Service enabled: {status.service_enabled}",
        f"Local health: {health_state} - {status.health.message}",
        "",
        "Configured environment:",
        *_format_env(status.env),
    ]
    return "\n".join(lines)
