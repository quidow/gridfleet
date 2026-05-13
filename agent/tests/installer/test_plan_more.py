from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from agent_app.installer.plan import (
    ToolDiscovery,
    _find_android_home,
    _find_node_bin_dir,
    _node_version_key,
    _parse_config_env_values,
    build_service_path,
    discover_tools,
    load_installed_config,
    render_launchd_plist,
    render_systemd_unit,
)

if TYPE_CHECKING:
    import pytest


def test_node_version_key_with_non_decimal() -> None:
    path = Path("/tmp/.nvm/versions/node/v24.x.y/bin/node")
    assert _node_version_key(path) == (24,)

    path2 = Path("/tmp/.nvm/versions/node/abc/bin/node")
    assert _node_version_key(path2) == ()


def test_find_node_bin_dir_fnm_aliases(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fnm_bin = tmp_path / ".local/share/fnm/aliases/default/bin/node"
    fnm_bin.parent.mkdir(parents=True)
    fnm_bin.write_text("")
    fnm_bin.chmod(0o755)
    monkeypatch.setattr("agent_app.installer.plan.shutil.which", lambda _name: None)

    result = _find_node_bin_dir({}, tmp_path)
    assert result == str(fnm_bin.parent)


def test_find_node_bin_dir_missing_everything(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("agent_app.installer.plan.shutil.which", lambda _name: None)
    assert _find_node_bin_dir({}, tmp_path) is None


def test_find_android_home_checks_directories(tmp_path: Path) -> None:
    sdk = tmp_path / "android-sdk"
    (sdk / "platform-tools").mkdir(parents=True)
    assert _find_android_home({}, tmp_path) is None  # env empty, no home/Library/Android/sdk

    # Now with ANDROID_HOME set
    assert _find_android_home({"ANDROID_HOME": str(sdk)}, tmp_path) == str(sdk)


def test_discover_tools_warns_when_tools_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("agent_app.installer.plan.shutil.which", lambda _name: None)
    discovery = discover_tools(env={}, home=tmp_path, os_name="Linux")
    assert len(discovery.warnings) == 2
    assert "Node.js not found" in discovery.warnings[0]
    assert "Android SDK" in discovery.warnings[1]


def test_build_service_path_with_none_values() -> None:
    discovery = ToolDiscovery(node_bin_dir=None, android_home=None)
    assert build_service_path(discovery) == "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"


def test_parse_config_env_values_missing_file_returns_empty() -> None:
    assert _parse_config_env_values(Path("/does/not/exist")) == {}


def test_parse_config_env_values_ignores_comments_and_blanks(tmp_path: Path) -> None:
    path = tmp_path / "config.env"
    path.write_text("\n  \n# comment\nKEY=value\n  \n")
    assert _parse_config_env_values(path) == {"KEY": "value"}


def test_parse_config_env_values_skip_lines_without_equals(tmp_path: Path) -> None:
    path = tmp_path / "config.env"
    path.write_text("FOO\nBAR=baz\n")
    assert _parse_config_env_values(path) == {"BAR": "baz"}


def test_load_installed_config_uses_defaults_when_file_missing(tmp_path: Path) -> None:
    base = load_installed_config(
        defaults=type(
            "C",
            (),
            {
                "config_env_path": str(tmp_path / "missing.env"),
                "agent_dir": "/opt/gridfleet-agent",
                "config_dir": "/etc/gridfleet-agent",
                "user": "root",
                "port": 5100,
                "manager_url": "http://localhost:8000",
                "manager_auth_username": None,
                "manager_auth_password": None,
                "api_auth_username": None,
                "api_auth_password": None,
                "grid_hub_url": "http://localhost:4444",
                "grid_publish_url": "tcp://localhost:4442",
                "grid_subscribe_url": "tcp://localhost:4443",
                "grid_node_port_start": 5555,
                "enable_web_terminal": False,
                "terminal_token": None,
            },
        )()
    )
    assert base.port == 5100


def test_render_systemd_unit_no_manager_auth() -> None:
    from agent_app.installer.plan import InstallConfig

    rendered = render_systemd_unit(InstallConfig())
    assert "AGENT_MANAGER_AUTH" not in rendered


def test_render_launchd_plist_without_android_or_auth() -> None:
    from agent_app.installer.plan import InstallConfig

    rendered = render_launchd_plist(InstallConfig(), ToolDiscovery())
    assert "ANDROID_HOME" not in rendered
    assert "AGENT_MANAGER_AUTH" not in rendered
