from __future__ import annotations

import getpass
import os
import platform
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING
from xml.sax.saxutils import escape

if TYPE_CHECKING:
    from collections.abc import Mapping


DEFAULT_SERVICE_PATH = "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"


@dataclass(frozen=True)
class InstallConfig:
    agent_dir: str = "/opt/gridfleet-agent"
    config_dir: str = "/etc/gridfleet-agent"
    user: str = field(default_factory=getpass.getuser)
    port: int = 5100
    manager_url: str = "http://localhost:8000"
    manager_auth_username: str | None = None
    manager_auth_password: str | None = None
    grid_hub_url: str = "http://localhost:4444"
    grid_publish_url: str = "tcp://localhost:4442"
    grid_subscribe_url: str = "tcp://localhost:4443"
    grid_node_port_start: int = 5555
    selenium_version: str = "4.41.0"
    enable_web_terminal: bool = False
    terminal_token: str | None = None

    def __post_init__(self) -> None:
        has_username = bool(self.manager_auth_username)
        has_password = bool(self.manager_auth_password)
        if has_username != has_password:
            raise ValueError("AGENT_MANAGER_AUTH_USERNAME and AGENT_MANAGER_AUTH_PASSWORD must be set together.")
        if self.enable_web_terminal and not self.terminal_token:
            raise ValueError("AGENT_TERMINAL_TOKEN must be set when AGENT_ENABLE_WEB_TERMINAL=true.")

    @property
    def selenium_jar(self) -> str:
        return f"{self.agent_dir}/selenium-server.jar"

    @property
    def venv_bin_dir(self) -> str:
        return f"{self.agent_dir}/venv/bin"

    @property
    def config_env_path(self) -> str:
        return f"{self.config_dir}/config.env"


@dataclass(frozen=True)
class ToolDiscovery:
    java_bin: str | None = None
    java_version: str | None = None
    node_bin_dir: str | None = None
    android_home: str | None = None
    warnings: list[str] = field(default_factory=list)


def _first_existing_executable(candidates: list[str]) -> str | None:
    for candidate in candidates:
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def _java_version(java_bin: str) -> str | None:
    try:
        result = subprocess.run(
            [java_bin, "-version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    output = result.stderr.strip() or result.stdout.strip()
    return output.splitlines()[0] if output else None


def _find_java(env: Mapping[str, str], home: Path, os_name: str) -> tuple[str | None, str | None]:
    path_java = shutil.which("java")
    java_home = env.get("JAVA_HOME", "")
    candidates = [
        path_java or "",
        f"{java_home}/bin/java" if java_home else "",
        str(home / ".sdkman/candidates/java/current/bin/java"),
        "/usr/local/bin/java",
        "/usr/bin/java",
    ]
    java_bin = _first_existing_executable(candidates)

    if java_bin is None and os_name == "Darwin" and os.path.exists("/usr/libexec/java_home"):
        try:
            result = subprocess.run(
                ["/usr/libexec/java_home"],
                check=False,
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            result = None
        if result is not None and result.returncode == 0:
            java_home_path = result.stdout.strip()
            java_bin = _first_existing_executable([f"{java_home_path}/bin/java"])

    return java_bin, _java_version(java_bin) if java_bin else None


def _find_node_bin_dir(env: Mapping[str, str], home: Path) -> str | None:
    node = shutil.which("node")
    if node:
        return str(Path(node).parent)

    nvm_root = home / ".nvm/versions/node"
    if nvm_root.is_dir():
        candidates = sorted(nvm_root.glob("v*/bin/node"), reverse=True)
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

    return None


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


def discover_tools(
    *,
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
    os_name: str | None = None,
) -> ToolDiscovery:
    resolved_env = env or os.environ
    resolved_home = home or Path.home()
    resolved_os = os_name or platform.system()
    warnings: list[str] = []

    java_bin, java_version = _find_java(resolved_env, resolved_home, resolved_os)
    node_bin_dir = _find_node_bin_dir(resolved_env, resolved_home)
    android_home = _find_android_home(resolved_env, resolved_home)

    if java_bin is None:
        warnings.append("Java not found. Grid relay node will not start.")
    if node_bin_dir is None:
        warnings.append("Node.js not found. Appium commands may not be available to the service.")
    if android_home is None:
        warnings.append("Android SDK platform-tools not found. ADB-based devices may not be available.")

    return ToolDiscovery(
        java_bin=java_bin,
        java_version=java_version,
        node_bin_dir=node_bin_dir,
        android_home=android_home,
        warnings=warnings,
    )


def build_service_path(discovery: ToolDiscovery) -> str:
    prefixes: list[str] = []
    if discovery.java_bin:
        prefixes.append(str(Path(discovery.java_bin).parent))
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
        f"AGENT_SELENIUM_SERVER_JAR={config.selenium_jar}",
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
    if config.enable_web_terminal:
        terminal_token = "<redacted>" if redact_secrets else config.terminal_token
        lines.append("AGENT_ENABLE_WEB_TERMINAL=true")
        lines.append(f"AGENT_TERMINAL_TOKEN={terminal_token}")
    return "\n".join(lines) + "\n"


def render_systemd_unit(config: InstallConfig) -> str:
    return f"""[Unit]
Description=GridFleet Agent
After=network.target

[Service]
Type=simple
User={config.user}
WorkingDirectory={config.agent_dir}
EnvironmentFile={config.config_env_path}
ExecStart={config.venv_bin_dir}/gridfleet-agent serve --host 0.0.0.0 --port {config.port}
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
        <string>{config.venv_bin_dir}/gridfleet-agent</string>
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
        "AGENT_SELENIUM_SERVER_JAR": config.selenium_jar,
        "AGENT_GRID_NODE_PORT_START": str(config.grid_node_port_start),
    }
    if discovery.android_home:
        entries["ANDROID_HOME"] = discovery.android_home
        entries["ANDROID_SDK_ROOT"] = discovery.android_home
    if config.manager_auth_username:
        entries["AGENT_MANAGER_AUTH_USERNAME"] = config.manager_auth_username
        entries["AGENT_MANAGER_AUTH_PASSWORD"] = config.manager_auth_password or ""
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
    java_line = (
        f"{discovery.java_bin} ({discovery.java_version or 'version unknown'})" if discovery.java_bin else "missing"
    )
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
  Selenium version: {config.selenium_version}
  Selenium JAR: {config.selenium_jar}
  Web terminal: {"enabled" if config.enable_web_terminal else "disabled"}

Detected tools:
  Java: {java_line}
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
        user=config.user,
        port=config.port,
        manager_url=config.manager_url,
        manager_auth_username=config.manager_auth_username,
        manager_auth_password="<redacted>" if config.manager_auth_password else None,
        grid_hub_url=config.grid_hub_url,
        grid_publish_url=config.grid_publish_url,
        grid_subscribe_url=config.grid_subscribe_url,
        grid_node_port_start=config.grid_node_port_start,
        selenium_version=config.selenium_version,
        enable_web_terminal=config.enable_web_terminal,
        terminal_token="<redacted>" if config.terminal_token else None,
    )
