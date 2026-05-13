from __future__ import annotations

from pathlib import Path

import pytest

from agent_app.installer.plan import (
    InstallConfig,
    ToolDiscovery,
    _find_node_bin_dir,
    build_service_path,
    discover_tools,
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
        node_bin_dir="/opt/node/bin",
        android_home="/opt/android-sdk",
        warnings=[],
    )

    rendered = render_config_env(config, discovery)

    assert "AGENT_MANAGER_URL=https://manager.example.com" in rendered
    assert "AGENT_SELENIUM_SERVER_JAR" not in rendered
    assert "ANDROID_HOME=/opt/android-sdk" in rendered
    assert "ANDROID_SDK_ROOT=/opt/android-sdk" in rendered
    assert "AGENT_MANAGER_AUTH_USERNAME=machine" in rendered
    assert "AGENT_MANAGER_AUTH_PASSWORD=secret" in rendered
    assert "AGENT_ENABLE_WEB_TERMINAL=true" in rendered
    assert "AGENT_TERMINAL_TOKEN=terminal-token" in rendered
    assert "PATH=/opt/node/bin:/opt/android-sdk/platform-tools:" in rendered


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
    assert "<key>AGENT_SELENIUM_SERVER_JAR</key>" not in rendered
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
    discovery = ToolDiscovery(warnings=["Node.js not found. Appium commands may not be available to the service."])

    output = format_dry_run(config, discovery, os_name="Linux")

    assert "GridFleet Agent install dry run" in output
    assert "Manager URL: https://manager.example.com" in output
    assert "Config file: /etc/gridfleet-agent/config.env" in output
    assert "Service file: /etc/systemd/system/gridfleet-agent.service" in output
    assert "Selenium JAR" not in output
    assert "Node.js not found. Appium commands may not be available to the service." in output
    assert "AGENT_MANAGER_URL=https://manager.example.com" in output
    assert "ExecStart=/opt/gridfleet-agent/venv/bin/gridfleet-agent serve" in output


def test_build_service_path_prioritizes_node_before_system_dirs() -> None:
    discovery = ToolDiscovery(node_bin_dir="/opt/node/bin", android_home="/opt/sdk")

    assert build_service_path(discovery).startswith("/opt/node/bin:/opt/sdk/platform-tools:")


def test_find_node_bin_dir_prefers_home_nvm_over_system_node(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    nvm_node = tmp_path / ".nvm/versions/node/v24.12.0/bin/node"
    nvm_node.parent.mkdir(parents=True)
    nvm_node.write_text("")
    nvm_node.chmod(0o755)
    monkeypatch.setattr("agent_app.installer.plan.shutil.which", lambda _name: "/usr/bin/node")

    assert _find_node_bin_dir({}, tmp_path) == str(nvm_node.parent)


def test_find_node_bin_dir_uses_highest_nvm_version(tmp_path: Path) -> None:
    old_node = tmp_path / ".nvm/versions/node/v9.9.0/bin/node"
    new_node = tmp_path / ".nvm/versions/node/v24.12.0/bin/node"
    for node in (old_node, new_node):
        node.parent.mkdir(parents=True)
        node.write_text("")
        node.chmod(0o755)

    assert _find_node_bin_dir({}, tmp_path) == str(new_node.parent)


def test_discover_tools_uses_sudo_user_home_for_nvm(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sudo_home = tmp_path / "operator"
    nvm_node = sudo_home / ".nvm/versions/node/v24.12.0/bin/node"
    nvm_node.parent.mkdir(parents=True)
    nvm_node.write_text("")
    nvm_node.chmod(0o755)
    monkeypatch.setattr("agent_app.installer.plan.Path.expanduser", lambda path: sudo_home)
    monkeypatch.setattr("agent_app.installer.plan.shutil.which", lambda _name: "/usr/bin/node")

    discovery = discover_tools(env={"SUDO_USER": "operator"}, os_name="Linux")

    assert discovery.node_bin_dir == str(nvm_node.parent)


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


def test_install_config_rejects_api_auth_pair_partial() -> None:
    from agent_app.installer.plan import InstallConfig

    with pytest.raises(ValueError, match="AGENT_API_AUTH"):
        InstallConfig(api_auth_username="ops", api_auth_password=None)
    with pytest.raises(ValueError, match="AGENT_API_AUTH"):
        InstallConfig(api_auth_username=None, api_auth_password="secret")


def test_render_config_env_writes_api_auth_pair() -> None:
    from agent_app.installer.plan import InstallConfig, ToolDiscovery, render_config_env

    config = InstallConfig(api_auth_username="ops", api_auth_password="secret")
    rendered = render_config_env(config, ToolDiscovery())
    assert "AGENT_API_AUTH_USERNAME=ops" in rendered
    assert "AGENT_API_AUTH_PASSWORD=secret" in rendered


def test_render_config_env_redacts_api_auth_password() -> None:
    from agent_app.installer.plan import InstallConfig, ToolDiscovery, render_config_env

    config = InstallConfig(api_auth_username="ops", api_auth_password="secret")
    rendered = render_config_env(config, ToolDiscovery(), redact_secrets=True)
    assert "AGENT_API_AUTH_USERNAME=ops" in rendered
    assert "AGENT_API_AUTH_PASSWORD=<redacted>" in rendered


def test_load_installed_config_round_trips_api_auth(tmp_path: Path) -> None:
    from agent_app.installer.plan import InstallConfig, ToolDiscovery, load_installed_config, render_config_env

    config_dir = tmp_path / "etc"
    config_dir.mkdir()
    base = InstallConfig(
        config_dir=str(config_dir),
        api_auth_username="ops",
        api_auth_password="secret",
    )
    (config_dir / "config.env").write_text(render_config_env(base, ToolDiscovery()))

    loaded = load_installed_config(InstallConfig(config_dir=str(config_dir)))
    assert loaded.api_auth_username == "ops"
    assert loaded.api_auth_password == "secret"


def test_render_launchd_plist_includes_api_auth_pair() -> None:
    from agent_app.installer.plan import InstallConfig, ToolDiscovery, render_launchd_plist

    config = InstallConfig(api_auth_username="ops", api_auth_password="secret")
    plist = render_launchd_plist(config, ToolDiscovery())
    assert "<key>AGENT_API_AUTH_USERNAME</key>" in plist
    assert "<string>ops</string>" in plist
    assert "<key>AGENT_API_AUTH_PASSWORD</key>" in plist
    assert "<string>secret</string>" in plist


def test_format_dry_run_darwin_redacts_api_auth_password() -> None:
    from agent_app.installer.plan import InstallConfig, ToolDiscovery, format_dry_run

    config = InstallConfig(api_auth_username="ops", api_auth_password="secret")
    output = format_dry_run(config, ToolDiscovery(), os_name="Darwin")
    assert "secret" not in output
    assert "AGENT_API_AUTH_USERNAME" in output
    # The launchd plist embeds the redacted password in a <string> element.
    assert "<string>&lt;redacted&gt;</string>" in output or "<string><redacted></string>" in output


def test_redacted_config_masks_api_auth_password() -> None:
    from agent_app.installer.plan import InstallConfig, _redacted_config

    config = InstallConfig(api_auth_username="ops", api_auth_password="secret")
    redacted = _redacted_config(config)
    assert redacted.api_auth_username == "ops"
    assert redacted.api_auth_password == "<redacted>"


def test_systemd_unit_uses_explicit_user() -> None:
    config = InstallConfig(user="ops")
    rendered = render_systemd_unit(config)
    assert "User=ops" in rendered


def test_default_install_config_linux_uses_xdg(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    from agent_app.installer.plan import default_install_config

    config = default_install_config("Linux")

    assert config.agent_dir == str(tmp_path / "data/gridfleet-agent")
    assert config.config_dir == str(tmp_path / "cfg/gridfleet-agent")


def test_default_install_config_linux_falls_back_to_dot_local(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))  # type: ignore[arg-type]
    from agent_app.installer.plan import default_install_config

    config = default_install_config("Linux")

    assert config.agent_dir == str(tmp_path / ".local/share/gridfleet-agent")
    assert config.config_dir == str(tmp_path / ".config/gridfleet-agent")


def test_default_install_config_darwin_uses_application_support(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))  # type: ignore[arg-type]
    from agent_app.installer.plan import default_install_config

    config = default_install_config("Darwin")

    assert config.agent_dir == str(tmp_path / "Library/Application Support/gridfleet-agent")
    assert config.config_dir == str(tmp_path / "Library/Application Support/gridfleet-agent/config")


def test_default_install_config_rejects_unknown_os() -> None:
    from agent_app.installer.plan import default_install_config

    with pytest.raises(RuntimeError, match="Unsupported OS"):
        default_install_config("Plan9")
