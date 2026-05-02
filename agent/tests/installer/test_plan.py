from __future__ import annotations

from pathlib import Path

import pytest

from agent_app.installer.plan import (
    InstallConfig,
    ToolDiscovery,
    build_service_path,
    format_dry_run,
    load_installed_config,
    render_config_env,
    render_launchd_plist,
    render_systemd_unit,
)


def test_config_rejects_partial_manager_auth() -> None:
    with pytest.raises(ValueError, match="AGENT_MANAGER_AUTH_USERNAME and AGENT_MANAGER_AUTH_PASSWORD"):
        InstallConfig(manager_auth_username="machine")


def test_config_rejects_terminal_without_token() -> None:
    with pytest.raises(ValueError, match="AGENT_TERMINAL_TOKEN"):
        InstallConfig(enable_web_terminal=True)


def test_render_config_env_includes_detected_paths_and_optional_auth() -> None:
    config = InstallConfig(
        manager_url="https://manager.example.com",
        manager_auth_username="machine",
        manager_auth_password="secret",
        enable_web_terminal=True,
        terminal_token="terminal-token",
    )
    discovery = ToolDiscovery(
        java_bin="/usr/bin/java",
        java_version='openjdk version "21"',
        node_bin_dir="/opt/node/bin",
        android_home="/opt/android-sdk",
        warnings=[],
    )

    rendered = render_config_env(config, discovery)

    assert "AGENT_MANAGER_URL=https://manager.example.com" in rendered
    assert "AGENT_SELENIUM_SERVER_JAR=/opt/gridfleet-agent/selenium-server.jar" in rendered
    assert "ANDROID_HOME=/opt/android-sdk" in rendered
    assert "ANDROID_SDK_ROOT=/opt/android-sdk" in rendered
    assert "AGENT_MANAGER_AUTH_USERNAME=machine" in rendered
    assert "AGENT_MANAGER_AUTH_PASSWORD=secret" in rendered
    assert "AGENT_ENABLE_WEB_TERMINAL=true" in rendered
    assert "AGENT_TERMINAL_TOKEN=terminal-token" in rendered
    assert "PATH=/usr/bin:/opt/node/bin:" in rendered


def test_load_installed_config_reads_persisted_agent_env(tmp_path: Path) -> None:
    defaults = InstallConfig(agent_dir=str(tmp_path / "agent"), config_dir=str(tmp_path / "config"))
    config_env = Path(defaults.config_env_path)
    config_env.parent.mkdir(parents=True)
    config_env.write_text(
        "\n".join(
            [
                "AGENT_MANAGER_URL=https://manager.example.com",
                "AGENT_AGENT_PORT=5300",
                "AGENT_GRID_HUB_URL=http://grid:4444",
                "AGENT_GRID_PUBLISH_URL=tcp://grid:4442",
                "AGENT_GRID_SUBSCRIBE_URL=tcp://grid:4443",
                "AGENT_GRID_NODE_PORT_START=6100",
                "AGENT_MANAGER_AUTH_USERNAME=machine",
                "AGENT_MANAGER_AUTH_PASSWORD=secret",
                "AGENT_ENABLE_WEB_TERMINAL=true",
                "AGENT_TERMINAL_TOKEN=terminal-token",
                "",
            ]
        )
    )

    config = load_installed_config(defaults)

    assert config.manager_url == "https://manager.example.com"
    assert config.port == 5300
    assert config.grid_hub_url == "http://grid:4444"
    assert config.grid_publish_url == "tcp://grid:4442"
    assert config.grid_subscribe_url == "tcp://grid:4443"
    assert config.grid_node_port_start == 6100
    assert config.manager_auth_username == "machine"
    assert config.manager_auth_password == "secret"
    assert config.enable_web_terminal is True
    assert config.terminal_token == "terminal-token"


def test_render_systemd_unit_uses_console_entry_point() -> None:
    rendered = render_systemd_unit(InstallConfig(user="gridfleet", port=5200))

    assert "User=gridfleet" in rendered
    assert "EnvironmentFile=/etc/gridfleet-agent/config.env" in rendered
    assert "ExecStart=/opt/gridfleet-agent/venv/bin/gridfleet-agent serve --host 0.0.0.0 --port 5200" in rendered


def test_render_launchd_plist_uses_console_entry_point() -> None:
    rendered = render_launchd_plist(
        InstallConfig(
            port=5200,
            manager_auth_username="machine",
            manager_auth_password="secret",
            enable_web_terminal=True,
            terminal_token="terminal-token",
        ),
        ToolDiscovery(node_bin_dir="/opt/node/bin", android_home="/opt/android-sdk"),
    )

    assert "<string>com.gridfleet.agent</string>" in rendered
    assert "<string>/opt/gridfleet-agent/venv/bin/gridfleet-agent</string>" in rendered
    assert "<string>serve</string>" in rendered
    assert "<string>5200</string>" in rendered
    assert "<key>PATH</key>" in rendered
    assert "<key>AGENT_GRID_HUB_URL</key>" in rendered
    assert "<key>AGENT_SELENIUM_SERVER_JAR</key>" in rendered
    assert "<key>ANDROID_HOME</key>" in rendered
    assert "<key>AGENT_MANAGER_AUTH_USERNAME</key>" in rendered
    assert "<key>AGENT_MANAGER_AUTH_PASSWORD</key>" in rendered
    assert "<key>AGENT_ENABLE_WEB_TERMINAL</key>" in rendered
    assert "<key>AGENT_TERMINAL_TOKEN</key>" in rendered
    assert "/opt/node/bin" in rendered


def test_dry_run_output_redacts_secrets() -> None:
    config = InstallConfig(
        manager_auth_username="machine",
        manager_auth_password="secret",
        enable_web_terminal=True,
        terminal_token="terminal-token",
    )

    output = format_dry_run(config, ToolDiscovery(), os_name="Linux")

    assert "secret" not in output
    assert "terminal-token" not in output
    assert "AGENT_MANAGER_AUTH_PASSWORD=<redacted>" in output
    assert "AGENT_TERMINAL_TOKEN=<redacted>" in output


def test_dry_run_output_redacts_launchd_service_secrets() -> None:
    config = InstallConfig(
        manager_auth_username="machine",
        manager_auth_password="secret",
        enable_web_terminal=True,
        terminal_token="terminal-token",
    )

    output = format_dry_run(config, ToolDiscovery(), os_name="Darwin")

    assert "secret" not in output
    assert "terminal-token" not in output
    assert "<key>AGENT_MANAGER_AUTH_PASSWORD</key>" in output
    assert "<string>&lt;redacted&gt;</string>" in output


def test_dry_run_output_names_generated_artifacts_and_warnings() -> None:
    config = InstallConfig(manager_url="https://manager.example.com")
    discovery = ToolDiscovery(warnings=["Java not found. Grid relay node will not start."])

    output = format_dry_run(config, discovery, os_name="Linux")

    assert "GridFleet Agent install dry run" in output
    assert "Manager URL: https://manager.example.com" in output
    assert "Config file: /etc/gridfleet-agent/config.env" in output
    assert "Service file: /etc/systemd/system/gridfleet-agent.service" in output
    assert "Java not found. Grid relay node will not start." in output
    assert "AGENT_MANAGER_URL=https://manager.example.com" in output
    assert "ExecStart=/opt/gridfleet-agent/venv/bin/gridfleet-agent serve" in output


def test_build_service_path_prepends_discovered_tool_dirs() -> None:
    discovery = ToolDiscovery(java_bin="/usr/lib/jvm/bin/java", node_bin_dir="/opt/node/bin", android_home="/opt/sdk")

    assert build_service_path(discovery).startswith("/usr/lib/jvm/bin:/opt/node/bin:/opt/sdk/platform-tools:")


def test_config_resolved_bin_path_defaults_to_venv() -> None:
    config = InstallConfig()
    assert config.resolved_bin_path == "/opt/gridfleet-agent/venv/bin/gridfleet-agent"


def test_config_resolved_bin_path_uses_explicit_override() -> None:
    config = InstallConfig(bin_path="/home/user/.local/bin/gridfleet-agent")
    assert config.resolved_bin_path == "/home/user/.local/bin/gridfleet-agent"


def test_render_systemd_unit_uses_custom_bin_path() -> None:
    rendered = render_systemd_unit(
        InstallConfig(
            user="gridfleet",
            port=5200,
            bin_path="/home/user/.local/bin/gridfleet-agent",
        )
    )
    assert "ExecStart=/home/user/.local/bin/gridfleet-agent serve --host 0.0.0.0 --port 5200" in rendered


def test_render_launchd_plist_uses_custom_bin_path() -> None:
    rendered = render_launchd_plist(
        InstallConfig(port=5200, bin_path="/home/user/.local/bin/gridfleet-agent"),
        ToolDiscovery(),
    )
    assert "<string>/home/user/.local/bin/gridfleet-agent</string>" in rendered
