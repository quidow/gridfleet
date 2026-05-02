from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from agent_app import cli
from agent_app.installer.install import HealthCheckResult, InstallResult
from agent_app.installer.plan import InstallConfig, ToolDiscovery

if TYPE_CHECKING:
    import pytest


def test_install_dry_run_prints_plan(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    def fake_discover_tools() -> ToolDiscovery:
        return ToolDiscovery(java_bin="/usr/bin/java")

    monkeypatch.setattr(cli, "discover_tools", fake_discover_tools)

    assert cli.main(["install", "--dry-run", "--manager-url", "https://manager.example.com", "--port", "5200"]) == 0

    output = capsys.readouterr().out
    assert "GridFleet Agent install dry run" in output
    assert "Manager URL: https://manager.example.com" in output
    assert "Agent port: 5200" in output
    assert "AGENT_MANAGER_URL=https://manager.example.com" in output


def test_install_dry_run_validates_terminal_token(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["install", "--dry-run", "--enable-web-terminal"]) == 2

    assert "AGENT_TERMINAL_TOKEN must be set" in capsys.readouterr().err


def test_install_without_dry_run_is_not_implemented(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["install"]) == 2

    assert "--no-start" in capsys.readouterr().err


def test_install_no_start_invokes_file_writer(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_discover_tools() -> ToolDiscovery:
        return ToolDiscovery()

    def fake_install_no_start(
        config: InstallConfig,
        discovery: ToolDiscovery,
    ) -> InstallResult:
        captured["config"] = config
        captured["discovery"] = discovery
        return InstallResult(
            config_env=Path("config.env"),
            service_file=Path("service"),
            selenium_jar=Path("jar"),
            started=False,
        )

    monkeypatch.setattr(cli, "discover_tools", fake_discover_tools)
    monkeypatch.setattr(cli, "install_no_start", fake_install_no_start)

    assert cli.main(["install", "--no-start", "--manager-url", "https://manager.example.com"]) == 0

    assert isinstance(captured["config"], InstallConfig)
    assert captured["config"].manager_url == "https://manager.example.com"


def test_install_start_is_rejected_until_implemented(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["install", "--start"]) == 2

    assert "must run from" in capsys.readouterr().err


def test_install_start_invokes_starting_installer(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_discover_tools() -> ToolDiscovery:
        return ToolDiscovery()

    def fake_install_with_start(config: InstallConfig, discovery: ToolDiscovery) -> InstallResult:
        captured["config"] = config
        captured["discovery"] = discovery
        return InstallResult(
            config_env=Path("config.env"),
            service_file=Path("service"),
            selenium_jar=Path("jar"),
            started=True,
            health=HealthCheckResult(ok=True, message="healthy"),
        )

    monkeypatch.setattr(cli, "discover_tools", fake_discover_tools)
    monkeypatch.setattr(cli, "install_with_start", fake_install_with_start)

    assert cli.main(["install", "--start", "--manager-url", "https://manager.example.com"]) == 0

    assert isinstance(captured["config"], InstallConfig)
    assert captured["config"].manager_url == "https://manager.example.com"


def test_install_args_build_expected_config(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, InstallConfig] = {}

    def fake_discover_tools() -> ToolDiscovery:
        return ToolDiscovery()

    def fake_format_dry_run(config: InstallConfig, discovery: ToolDiscovery, *, os_name: str | None = None) -> str:
        captured["config"] = config
        return "dry run\n"

    monkeypatch.setattr(cli, "discover_tools", fake_discover_tools)
    monkeypatch.setattr(cli, "format_dry_run", fake_format_dry_run)

    assert (
        cli.main(
            [
                "install",
                "--dry-run",
                "--manager-url",
                "https://manager.example.com",
                "--user",
                "gridfleet",
                "--manager-auth-username",
                "machine",
                "--manager-auth-password",
                "secret",
                "--grid-hub-url",
                "http://grid:4444",
                "--grid-publish-url",
                "tcp://grid:4442",
                "--grid-subscribe-url",
                "tcp://grid:4443",
                "--grid-node-port-start",
                "6000",
            ]
        )
        == 0
    )

    config = captured["config"]
    assert config.manager_url == "https://manager.example.com"
    assert config.user == "gridfleet"
    assert config.manager_auth_username == "machine"
    assert config.manager_auth_password == "secret"
    assert config.grid_hub_url == "http://grid:4444"
    assert config.grid_publish_url == "tcp://grid:4442"
    assert config.grid_subscribe_url == "tcp://grid:4443"
    assert config.grid_node_port_start == 6000


def test_status_prints_collected_status(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    sentinel = object()

    def fake_collect_status(config: InstallConfig) -> object:
        assert isinstance(config, InstallConfig)
        return sentinel

    monkeypatch.setattr(cli, "collect_status", fake_collect_status)
    monkeypatch.setattr(cli, "format_status", lambda status: "status text" if status is sentinel else "wrong")

    assert cli.main(["status"]) == 0

    assert capsys.readouterr().out == "status text\n"
