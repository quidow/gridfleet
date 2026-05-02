from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from agent_app import cli
from agent_app.installer.install import HealthCheckResult, InstallResult, RegistrationCheckResult
from agent_app.installer.plan import InstallConfig, ToolDiscovery
from agent_app.installer.update import DrainResult, UpdateResult

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


def test_install_rejects_conflicting_modes(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["install", "--start", "--no-start"]) == 2

    assert "choose only one" in capsys.readouterr().err


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


def test_install_start_warns_when_manager_registration_is_pending(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fake_discover_tools() -> ToolDiscovery:
        return ToolDiscovery()

    def fake_install_with_start(config: InstallConfig, discovery: ToolDiscovery) -> InstallResult:
        return InstallResult(
            config_env=Path("config.env"),
            service_file=Path("service"),
            selenium_jar=Path("jar"),
            started=True,
            health=HealthCheckResult(ok=True, message="healthy"),
            registration=RegistrationCheckResult(ok=False, message="agent registration pending"),
        )

    monkeypatch.setattr(cli, "discover_tools", fake_discover_tools)
    monkeypatch.setattr(cli, "install_with_start", fake_install_with_start)

    assert cli.main(["install", "--start"]) == 0

    assert "agent registration pending" in capsys.readouterr().err


def test_install_start_returns_nonzero_when_health_fails(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fake_discover_tools() -> ToolDiscovery:
        return ToolDiscovery()

    def fake_install_with_start(config: InstallConfig, discovery: ToolDiscovery) -> InstallResult:
        return InstallResult(
            config_env=Path("config.env"),
            service_file=Path("service"),
            selenium_jar=Path("jar"),
            started=True,
            health=HealthCheckResult(ok=False, message="agent health check timed out: connection refused"),
        )

    monkeypatch.setattr(cli, "discover_tools", fake_discover_tools)
    monkeypatch.setattr(cli, "install_with_start", fake_install_with_start)

    assert cli.main(["install", "--start"]) == 1

    assert "agent health check timed out" in capsys.readouterr().err


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


def test_uninstall_requires_confirmation(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["uninstall"]) == 2

    assert "--yes" in capsys.readouterr().err


def test_uninstall_invokes_uninstaller(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    captured: dict[str, object] = {}

    def fake_uninstall(
        config: InstallConfig,
        *,
        remove_agent_dir: bool = True,
        remove_config_dir: bool = True,
    ) -> object:
        captured["config"] = config
        captured["remove_agent_dir"] = remove_agent_dir
        captured["remove_config_dir"] = remove_config_dir
        return object()

    monkeypatch.setattr(cli, "uninstall", fake_uninstall)

    assert cli.main(["uninstall", "--yes", "--keep-config", "--keep-agent-dir"]) == 0

    assert isinstance(captured["config"], InstallConfig)
    assert captured["remove_agent_dir"] is False
    assert captured["remove_config_dir"] is False
    assert "GridFleet agent uninstalled" in capsys.readouterr().out


def test_update_dry_run_prints_plan(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    captured: dict[str, object] = {}
    loaded_config = InstallConfig(port=5300)

    def fake_format_update_dry_run(config: InstallConfig, *, to_version: str | None = None) -> str:
        captured["config"] = config
        captured["to_version"] = to_version
        return "update plan"

    monkeypatch.setattr(cli, "load_installed_config", lambda: loaded_config)
    monkeypatch.setattr(cli, "format_update_dry_run", fake_format_update_dry_run)

    assert cli.main(["update", "--dry-run", "--to", "0.3.0"]) == 0

    assert captured["config"] == loaded_config
    assert captured["to_version"] == "0.3.0"
    assert capsys.readouterr().out == "update plan\n"


def test_update_dry_run_reports_installed_config_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "load_installed_config", lambda: (_ for _ in ()).throw(ValueError("bad config")))

    assert cli.main(["update", "--dry-run"]) == 2

    assert "ERROR: bad config" in capsys.readouterr().err


def test_update_invokes_updater(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    captured: dict[str, object] = {}
    loaded_config = InstallConfig(port=5300)

    def fake_update_agent(config: InstallConfig, *, to_version: str | None = None) -> UpdateResult:
        captured["config"] = config
        captured["to_version"] = to_version
        return UpdateResult(
            to_version=to_version,
            restarted=True,
            drain=DrainResult(ok=True, message="no active local nodes"),
            health=HealthCheckResult(ok=True, message="healthy"),
        )

    monkeypatch.setattr(cli, "load_installed_config", lambda: loaded_config)
    monkeypatch.setattr(cli, "update_agent", fake_update_agent)

    assert cli.main(["update", "--to", "0.3.0"]) == 0

    assert captured["config"] == loaded_config
    assert captured["to_version"] == "0.3.0"
    output = capsys.readouterr().out
    assert "Drain: no active local nodes" in output
    assert "GridFleet agent updated" in output
