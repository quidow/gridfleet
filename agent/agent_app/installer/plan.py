from __future__ import annotations

import getpass
import os
import platform
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING
from xml.sax.saxutils import escape

if TYPE_CHECKING:
    from collections.abc import Mapping


DEFAULT_SERVICE_PATH = "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"


def _xdg_data_home() -> Path:
    raw = os.environ.get("XDG_DATA_HOME")
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".local" / "share"


def _xdg_config_home() -> Path:
    raw = os.environ.get("XDG_CONFIG_HOME")
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".config"


@dataclass(frozen=True)
class InstallConfig:
    agent_dir: str = "/opt/gridfleet-agent"
    config_dir: str = "/etc/gridfleet-agent"
    bin_path: str | None = None
    user: str = field(default_factory=getpass.getuser)
    port: int = 5100
    manager_url: str = "http://localhost:8000"
    manager_auth_username: str | None = None
    manager_auth_password: str | None = None
    api_auth_username: str | None = None
    api_auth_password: str | None = None
    grid_hub_url: str = "http://localhost:4444"
    grid_publish_url: str = "tcp://localhost:4442"
    grid_subscribe_url: str = "tcp://localhost:4443"
    grid_node_port_start: int = 5555
    enable_web_terminal: bool = False
    terminal_token: str | None = None

    def __post_init__(self) -> None:
        has_username = bool(self.manager_auth_username)
        has_password = bool(self.manager_auth_password)
        if has_username != has_password:
            raise ValueError("AGENT_MANAGER_AUTH_USERNAME and AGENT_MANAGER_AUTH_PASSWORD must be set together.")
        has_api_username = bool(self.api_auth_username)
        has_api_password = bool(self.api_auth_password)
        if has_api_username != has_api_password:
            raise ValueError("AGENT_API_AUTH_USERNAME and AGENT_API_AUTH_PASSWORD must be set together.")
        if self.enable_web_terminal and not self.terminal_token:
            raise ValueError("AGENT_TERMINAL_TOKEN must be set when AGENT_ENABLE_WEB_TERMINAL=true.")

    @property
    def resolved_bin_path(self) -> str:
        if self.bin_path:
            return self.bin_path
        return f"{self.agent_dir}/venv/bin/gridfleet-agent"

    @property
    def venv_bin_dir(self) -> str:
        """Deprecated: use resolved_bin_path instead."""
        return f"{self.agent_dir}/venv/bin"

    @property
    def config_env_path(self) -> str:
        return f"{self.config_dir}/config.env"


def default_install_config(os_name: str) -> InstallConfig:
    """Return an InstallConfig with per-OS user-scope defaults.

    Linux uses XDG base directories ($XDG_DATA_HOME / $XDG_CONFIG_HOME, with
    ~/.local/share and ~/.config as fallbacks). Darwin uses Apple's
    Application Support convention under the operator's home directory.
    """
    if os_name == "Linux":
        return InstallConfig(
            agent_dir=str(_xdg_data_home() / "gridfleet-agent"),
            config_dir=str(_xdg_config_home() / "gridfleet-agent"),
        )
    if os_name == "Darwin":
        agent = Path.home() / "Library" / "Application Support" / "gridfleet-agent"
        return InstallConfig(
            agent_dir=str(agent),
            config_dir=str(agent / "config"),
        )
    raise RuntimeError(f"Unsupported OS: {os_name}")


@dataclass(frozen=True)
class ToolDiscovery:
    node_bin_dir: str | None = None
    android_home: str | None = None
    warnings: list[str] = field(default_factory=list)


def _first_existing_executable(candidates: list[str]) -> str | None:
    for candidate in candidates:
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def _find_node_bin_dir(env: Mapping[str, str], home: Path) -> str | None:
    nvm_bin = env.get("NVM_BIN", "")
    if nvm_bin:
        executable = _first_existing_executable([f"{nvm_bin}/node"])
        if executable:
            return str(Path(executable).parent)

    nvm_root = home / ".nvm/versions/node"
    if nvm_root.is_dir():
        candidates = sorted(nvm_root.glob("v*/bin/node"), key=_node_version_key, reverse=True)
        executable = _first_existing_executable([str(candidate) for candidate in candidates])
        if executable:
            return str(Path(executable).parent)

    for candidate in (
        env.get("FNM_DIR", "") + "/aliases/default/bin/node" if env.get("FNM_DIR") else "",
        str(home / ".local/share/fnm/aliases/default/bin/node"),
        str(home / "Library/Application Support/fnm/aliases/default/bin/node"),
    ):
        executable = _first_existing_executable([candidate])
        if executable:
            return str(Path(executable).parent)

    node = shutil.which("node")
    if node:
        return str(Path(node).parent)

    return None


def _node_version_key(node_path: Path) -> tuple[int, ...]:
    version = node_path.parent.parent.name.removeprefix("v")
    parts: list[int] = []
    for segment in version.split("."):
        if not segment.isdecimal():
            break
        parts.append(int(segment))
    return tuple(parts)


def _find_android_home(env: Mapping[str, str], home: Path) -> str | None:
    for candidate in (
        env.get("ANDROID_HOME", ""),
        env.get("ANDROID_SDK_ROOT", ""),
        str(home / "Library/Android/sdk"),
        str(home / "Android/Sdk"),
        "/opt/android-sdk",
        "/usr/local/android-sdk",
    ):
        if candidate and os.path.isdir(f"{candidate}/platform-tools"):
            return candidate
    return None


def _operator_home(env: Mapping[str, str]) -> Path:
    sudo_user = env.get("SUDO_USER")
    if sudo_user:
        try:
            return Path(f"~{sudo_user}").expanduser()
        except RuntimeError:
            pass
    return Path.home()


def discover_tools(
    *,
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
    os_name: str | None = None,
) -> ToolDiscovery:
    resolved_env = os.environ if env is None else env
    resolved_home = home or _operator_home(resolved_env)
    warnings: list[str] = []

    node_bin_dir = _find_node_bin_dir(resolved_env, resolved_home)
    android_home = _find_android_home(resolved_env, resolved_home)

    if node_bin_dir is None:
        warnings.append("Node.js not found. Appium commands may not be available to the service.")
    if android_home is None:
        warnings.append("Android SDK platform-tools not found. ADB-based devices may not be available.")

    return ToolDiscovery(
        node_bin_dir=node_bin_dir,
        android_home=android_home,
        warnings=warnings,
    )


def build_service_path(discovery: ToolDiscovery) -> str:
    prefixes: list[str] = []
    if discovery.node_bin_dir:
        prefixes.append(discovery.node_bin_dir)
    if discovery.android_home:
        prefixes.append(f"{discovery.android_home}/platform-tools")
    return ":".join([*prefixes, DEFAULT_SERVICE_PATH])


def render_config_env(config: InstallConfig, discovery: ToolDiscovery, *, redact_secrets: bool = False) -> str:
    lines = [
        f"AGENT_MANAGER_URL={config.manager_url}",
        f"AGENT_AGENT_PORT={config.port}",
        f"AGENT_GRID_HUB_URL={config.grid_hub_url}",
        f"AGENT_GRID_PUBLISH_URL={config.grid_publish_url}",
        f"AGENT_GRID_SUBSCRIBE_URL={config.grid_subscribe_url}",
        f"AGENT_GRID_NODE_PORT_START={config.grid_node_port_start}",
        f"PATH={build_service_path(discovery)}",
    ]
    if discovery.android_home:
        lines.extend(
            [
                f"ANDROID_HOME={discovery.android_home}",
                f"ANDROID_SDK_ROOT={discovery.android_home}",
            ]
        )
    if config.manager_auth_username:
        manager_password = "<redacted>" if redact_secrets else config.manager_auth_password
        lines.extend(
            [
                f"AGENT_MANAGER_AUTH_USERNAME={config.manager_auth_username}",
                f"AGENT_MANAGER_AUTH_PASSWORD={manager_password}",
            ]
        )
    if config.api_auth_username:
        api_password = "<redacted>" if redact_secrets else config.api_auth_password
        lines.extend(
            [
                f"AGENT_API_AUTH_USERNAME={config.api_auth_username}",
                f"AGENT_API_AUTH_PASSWORD={api_password}",
            ]
        )
    if config.enable_web_terminal:
        terminal_token = "<redacted>" if redact_secrets else config.terminal_token
        lines.append("AGENT_ENABLE_WEB_TERMINAL=true")
        lines.append(f"AGENT_TERMINAL_TOKEN={terminal_token}")
    return "\n".join(lines) + "\n"


def _parse_config_env_values(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    try:
        raw_lines = path.read_text().splitlines()
    except OSError:
        return {}
    for raw_line in raw_lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key] = value
    return values


def _env_int(values: Mapping[str, str], key: str, default: int) -> int:
    try:
        return int(values.get(key, str(default)))
    except ValueError:
        return default


def load_installed_config(defaults: InstallConfig | None = None) -> InstallConfig:
    base = defaults or InstallConfig()
    values = _parse_config_env_values(Path(base.config_env_path))
    return InstallConfig(
        agent_dir=base.agent_dir,
        config_dir=base.config_dir,
        user=base.user,
        port=_env_int(values, "AGENT_AGENT_PORT", base.port),
        manager_url=values.get("AGENT_MANAGER_URL", base.manager_url),
        manager_auth_username=values.get("AGENT_MANAGER_AUTH_USERNAME", base.manager_auth_username),
        manager_auth_password=values.get("AGENT_MANAGER_AUTH_PASSWORD", base.manager_auth_password),
        api_auth_username=values.get("AGENT_API_AUTH_USERNAME", base.api_auth_username),
        api_auth_password=values.get("AGENT_API_AUTH_PASSWORD", base.api_auth_password),
        grid_hub_url=values.get("AGENT_GRID_HUB_URL", base.grid_hub_url),
        grid_publish_url=values.get("AGENT_GRID_PUBLISH_URL", base.grid_publish_url),
        grid_subscribe_url=values.get("AGENT_GRID_SUBSCRIBE_URL", base.grid_subscribe_url),
        grid_node_port_start=_env_int(values, "AGENT_GRID_NODE_PORT_START", base.grid_node_port_start),
        enable_web_terminal=values.get("AGENT_ENABLE_WEB_TERMINAL", str(base.enable_web_terminal)).lower() == "true",
        terminal_token=values.get("AGENT_TERMINAL_TOKEN", base.terminal_token),
    )


def render_systemd_unit(config: InstallConfig) -> str:
    return f"""[Unit]
Description=GridFleet Agent
After=network.target

[Service]
Type=simple
User={config.user}
WorkingDirectory={config.agent_dir}
EnvironmentFile={config.config_env_path}
ExecStart={config.resolved_bin_path} serve --host 0.0.0.0 --port {config.port}
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
"""


def render_launchd_plist(config: InstallConfig, discovery: ToolDiscovery) -> str:
    env_entries = _launchd_env_entries(config, discovery)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.gridfleet.agent</string>
    <key>ProgramArguments</key>
    <array>
        <string>{config.resolved_bin_path}</string>
        <string>serve</string>
        <string>--host</string>
        <string>0.0.0.0</string>
        <string>--port</string>
        <string>{config.port}</string>
    </array>
    <key>WorkingDirectory</key>
    <string>{config.agent_dir}</string>
    <key>EnvironmentVariables</key>
    <dict>
{env_entries}
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/tmp/gridfleet-agent.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/gridfleet-agent.err</string>
</dict>
</plist>
"""


def _launchd_env_entries(config: InstallConfig, discovery: ToolDiscovery) -> str:
    entries = {
        "PATH": build_service_path(discovery),
        "AGENT_MANAGER_URL": config.manager_url,
        "AGENT_AGENT_PORT": str(config.port),
        "AGENT_GRID_HUB_URL": config.grid_hub_url,
        "AGENT_GRID_PUBLISH_URL": config.grid_publish_url,
        "AGENT_GRID_SUBSCRIBE_URL": config.grid_subscribe_url,
        "AGENT_GRID_NODE_PORT_START": str(config.grid_node_port_start),
    }
    if discovery.android_home:
        entries["ANDROID_HOME"] = discovery.android_home
        entries["ANDROID_SDK_ROOT"] = discovery.android_home
    if config.manager_auth_username:
        entries["AGENT_MANAGER_AUTH_USERNAME"] = config.manager_auth_username
        entries["AGENT_MANAGER_AUTH_PASSWORD"] = config.manager_auth_password or ""
    if config.api_auth_username:
        entries["AGENT_API_AUTH_USERNAME"] = config.api_auth_username
        entries["AGENT_API_AUTH_PASSWORD"] = config.api_auth_password or ""
    if config.enable_web_terminal:
        entries["AGENT_ENABLE_WEB_TERMINAL"] = "true"
        entries["AGENT_TERMINAL_TOKEN"] = config.terminal_token or ""

    lines: list[str] = []
    for key, value in entries.items():
        lines.append(f"        <key>{escape(key)}</key>")
        lines.append(f"        <string>{escape(value)}</string>")
    return "\n".join(lines)


def format_dry_run(config: InstallConfig, discovery: ToolDiscovery, *, os_name: str | None = None) -> str:
    resolved_os = os_name or platform.system()
    display_config = _redacted_config(config)
    service_path = (
        "/etc/systemd/system/gridfleet-agent.service"
        if resolved_os == "Linux"
        else "~/Library/LaunchAgents/com.gridfleet.agent.plist"
    )
    service_body = (
        render_systemd_unit(display_config)
        if resolved_os == "Linux"
        else render_launchd_plist(display_config, discovery)
    )
    warnings = "\n".join(f"  - {warning}" for warning in discovery.warnings) or "  - none"
    node_line = discovery.node_bin_dir or "missing"
    android_line = discovery.android_home or "missing"

    return f"""GridFleet Agent install dry run

Install paths:
  Agent dir: {config.agent_dir}
  Runtime dir: {config.agent_dir}/runtimes
  Config file: {config.config_env_path}
  Service file: {service_path}

Settings:
  User: {config.user}
  Agent port: {config.port}
  Manager URL: {config.manager_url}
  Grid hub URL: {config.grid_hub_url}
  Grid publish URL: {config.grid_publish_url}
  Grid subscribe URL: {config.grid_subscribe_url}
  Web terminal: {"enabled" if config.enable_web_terminal else "disabled"}

Detected tools:
  Node bin dir: {node_line}
  Android SDK: {android_line}

Warnings:
{warnings}

Generated config.env:
{render_config_env(config, discovery, redact_secrets=True)}
Generated service definition:
{service_body}
"""


def _redacted_config(config: InstallConfig) -> InstallConfig:
    return InstallConfig(
        agent_dir=config.agent_dir,
        config_dir=config.config_dir,
        bin_path=config.bin_path,
        user=config.user,
        port=config.port,
        manager_url=config.manager_url,
        manager_auth_username=config.manager_auth_username,
        manager_auth_password="<redacted>" if config.manager_auth_password else None,
        api_auth_username=config.api_auth_username,
        api_auth_password="<redacted>" if config.api_auth_password else None,
        grid_hub_url=config.grid_hub_url,
        grid_publish_url=config.grid_publish_url,
        grid_subscribe_url=config.grid_subscribe_url,
        grid_node_port_start=config.grid_node_port_start,
        enable_web_terminal=config.enable_web_terminal,
        terminal_token="<redacted>" if config.terminal_token else None,
    )
