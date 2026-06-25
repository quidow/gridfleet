from __future__ import annotations

import os
import platform
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

import httpx2 as httpx

from agent_app.installer.install import (
    HealthCheckCallable,
    HealthCheckResult,
    _poll_http,
    _run_command,
    poll_agent_health,
)
from agent_app.installer.uv_runtime import UvRuntime, build_upgrade_command

if TYPE_CHECKING:
    from collections.abc import Callable

    from agent_app.installer.identity import OperatorIdentity
    from agent_app.installer.plan import InstallConfig


# ---------------------------------------------------------------------------
# Typed exception hierarchy
# ---------------------------------------------------------------------------


class UpdateError(RuntimeError):
    """Base for update lifecycle failures."""


class UpdateDrainError(UpdateError):
    """Active local nodes prevented the upgrade."""


class UvNotFoundError(UpdateError):
    """No usable uv binary discovered for the operator."""


class UpdateUpgradeError(UpdateError):
    """`uv tool upgrade` failed."""


class UpdateRestartError(UpdateError):
    """systemctl/launchctl restart failed."""


class UpdateHealthError(UpdateError):
    """Post-restart health poll failed."""


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DrainResult:
    ok: bool
    message: str


class DrainCheckCallable(Protocol):
    def __call__(self, url: str, *, auth: tuple[str, str] | None = None) -> DrainResult:
        raise NotImplementedError


@dataclass(frozen=True)
class UpdateResult:
    to_version: str | None
    restarted: bool
    drain: DrainResult
    health: HealthCheckResult


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _agent_package_spec(to_version: str | None) -> str:
    return f"gridfleet-agent=={to_version}" if to_version else "gridfleet-agent"


def _restart_command(os_name: str) -> list[str]:
    if os_name == "Linux":
        return ["systemctl", "--user", "restart", "gridfleet-agent"]
    if os_name == "Darwin":
        return ["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/com.gridfleet.agent"]
    raise RuntimeError(f"Unsupported OS: {os_name}")


def _health_url(config: InstallConfig) -> str:
    return f"http://localhost:{config.port}/agent/health"


def _parse_min_python(requires_python: str | None) -> tuple[int, int] | None:
    """Extract the (major, minor) floor from a requires-python spec like '>=3.14'.

    Returns None for anything we cannot read confidently (no >= clause, exotic
    operators), so callers fall back to the in-place upgrade.
    """
    if not requires_python:
        return None
    for raw_clause in requires_python.split(","):
        clause = raw_clause.strip()
        if clause.startswith(">="):
            parts = clause[2:].strip().split(".")
            if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
                return int(parts[0]), int(parts[1])
    return None


def _venv_python_version(venv_dir: Path) -> tuple[int, int] | None:
    """Read the venv interpreter's (major, minor) from pyvenv.cfg.

    uv writes ``version_info``; stdlib venv writes ``version``. None if unreadable.
    """
    try:
        text = (venv_dir / "pyvenv.cfg").read_text()
    except OSError:
        return None
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip() in ("version", "version_info"):
            parts = value.strip().split(".")
            if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
                return int(parts[0]), int(parts[1])
    return None


def fetch_required_python(
    to_version: str | None,
    *,
    get: Callable[..., httpx.Response] = httpx.get,
) -> tuple[int, int] | None:
    """Resolve the target gridfleet-agent release's required Python floor from PyPI.

    Returns None on any error so the caller falls back to the in-place upgrade.
    """
    suffix = f"{to_version}/json" if to_version else "json"
    try:
        resp = get(f"https://pypi.org/pypi/gridfleet-agent/{suffix}", timeout=30.0)
        if resp.status_code != 200:
            return None
        info = resp.json().get("info", {})
    except httpx.HTTPError, ValueError, KeyError, TypeError:
        return None
    return _parse_min_python(info.get("requires_python") if isinstance(info, dict) else None)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def format_update_dry_run(
    config: InstallConfig,
    *,
    operator: OperatorIdentity,
    uv_runtime: UvRuntime,
    to_version: str | None = None,
    os_name: str | None = None,
) -> str:
    resolved_os = os_name or platform.system()
    package_spec = _agent_package_spec(to_version)

    # Resolve uv path display
    if uv_runtime.bin_path is not None:
        uv_path_display = str(uv_runtime.bin_path)
    else:
        searched_str = ", ".join(uv_runtime.searched) if uv_runtime.searched else "(none)"
        uv_path_display = f"not found (searched: {searched_str})"

    # Resolve upgrade command
    try:
        upgrade_cmd = build_upgrade_command(
            uv_runtime,
            package_spec=package_spec,
            config=config,
        )
        uv_command = " ".join(upgrade_cmd)
    except RuntimeError as exc:
        uv_command = f"uv missing — {exc}"

    # Resolve restart command
    try:
        restart_command = " ".join(_restart_command(resolved_os))
    except RuntimeError as exc:
        restart_command = str(exc).replace("Unsupported OS", "unsupported OS", 1)

    return f"""GridFleet Agent update dry run

Operator: {operator.login} (uid={operator.uid}, home={operator.home})
uv binary: {uv_path_display}

Actions:
  - Wait for active local sessions to drain: {_health_url(config)}
  - Upgrade package: {uv_command}
  - Restart service: {restart_command}
  - Poll local health: {_health_url(config)}
"""


def _drain_blockers(payload: object) -> tuple[int, int] | None:
    """Count drain blockers as (nodes with an active session, nodes with unknown session state).

    Running nodes are persistent per-connected-device processes, so node
    existence alone must not block an upgrade — only in-flight sessions do.
    Nodes that do not report ``has_active_session`` (older serving agents)
    are counted as unknown and treated as blockers
    rather than risk killing an in-flight session.
    """
    if not isinstance(payload, dict):
        return None
    appium_processes = payload.get("appium_processes")
    if not isinstance(appium_processes, dict):
        return None
    running_nodes = appium_processes.get("running_nodes")
    if not isinstance(running_nodes, list):
        return None
    active = 0
    unknown = 0
    for node in running_nodes:
        if not isinstance(node, dict) or "has_active_session" not in node:
            unknown += 1
        elif node["has_active_session"] is True:
            active += 1
    return active, unknown


def wait_for_update_drain(
    url: str,
    *,
    timeout_sec: float = 120.0,
    interval_sec: float = 2.0,
    get: Callable[..., object] = httpx.get,
    auth: tuple[str, str] | None = None,
) -> DrainResult:
    def interpret(status_code: object, payload: object) -> DrainResult | str:
        if status_code != 200:
            return f"unexpected status {status_code}"
        blockers = _drain_blockers(payload)
        if blockers is None:
            return "health payload did not include appium_processes.running_nodes"
        active, unknown = blockers
        if active == 0 and unknown == 0:
            return DrainResult(ok=True, message="no active local sessions")
        parts = []
        if active:
            suffix = "session" if active == 1 else "sessions"
            parts.append(f"{active} active local {suffix}")
        if unknown:
            suffix = "node" if unknown == 1 else "nodes"
            parts.append(f"{unknown} local {suffix} with unknown session state")
        return ", ".join(parts)

    outcome = _poll_http(
        url,
        timeout_sec=timeout_sec,
        interval_sec=interval_sec,
        get=get,
        auth=auth,
        initial_error="no response",
        interpret=interpret,
    )
    if isinstance(outcome, str):
        return DrainResult(ok=False, message=f"update drain timed out: {outcome}")
    return outcome


def update_agent(
    config: InstallConfig,
    *,
    operator: OperatorIdentity,
    uv_runtime: UvRuntime,
    to_version: str | None = None,
    os_name: str | None = None,
    run_command: Callable[[list[str]], None] = _run_command,
    drain_check: DrainCheckCallable = wait_for_update_drain,
    health_check: HealthCheckCallable = poll_agent_health,
    required_python: Callable[[str | None], tuple[int, int] | None] = fetch_required_python,
    venv_python_version: Callable[[Path], tuple[int, int] | None] = _venv_python_version,
) -> UpdateResult:
    resolved_os = os_name or platform.system()
    health_url = _health_url(config)

    api_auth = (
        (config.api_auth_username, config.api_auth_password)
        if config.api_auth_username and config.api_auth_password
        else None
    )

    drain = drain_check(health_url, auth=api_auth) if api_auth else drain_check(health_url)
    if not drain.ok:
        raise UpdateDrainError(drain.message)

    package_spec = _agent_package_spec(to_version)
    try:
        upgrade_cmd = build_upgrade_command(
            uv_runtime,
            package_spec=package_spec,
            config=config,
        )
    except RuntimeError as exc:
        raise UvNotFoundError(str(exc)) from exc

    # A breaking release can raise the required Python above the venv's interpreter
    # (e.g. 3.12 -> 3.14). `uv pip install --upgrade` cannot cross that boundary —
    # it silently keeps the old version — so recreate the venv on the new Python
    # first. Skipped whenever the floor or current version is unknown (no regression).
    venv_dir = Path(config.agent_dir) / "venv"
    current = venv_python_version(venv_dir)
    required = required_python(to_version) if current is not None else None
    if current is not None and required is not None and current < required:
        recreate_cmd = [
            str(uv_runtime.bin_path),
            "venv",
            "--clear",
            "--python",
            f"{required[0]}.{required[1]}",
            str(venv_dir),
        ]
        try:
            run_command(recreate_cmd)
        except (RuntimeError, OSError) as exc:
            raise UpdateUpgradeError(str(exc)) from exc

    try:
        run_command(upgrade_cmd)
    except (RuntimeError, OSError) as exc:
        raise UpdateUpgradeError(str(exc)) from exc

    try:
        run_command(_restart_command(resolved_os))
    except (RuntimeError, OSError) as exc:
        raise UpdateRestartError(str(exc)) from exc

    try:
        health = health_check(health_url, auth=api_auth) if api_auth else health_check(health_url)
    except Exception as exc:  # symmetric with the other wrappers
        raise UpdateHealthError(str(exc)) from exc
    if not health.ok:
        raise UpdateHealthError(health.message)

    return UpdateResult(to_version=to_version, restarted=True, drain=drain, health=health)
