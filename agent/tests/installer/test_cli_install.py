from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

from agent_app import cli
from agent_app.installer.identity import OperatorIdentity
from agent_app.installer.install import HealthCheckResult, InstallResult, RegistrationCheckResult
from agent_app.installer.plan import InstallConfig, ToolDiscovery
from agent_app.installer.update import DrainResult, UpdateResult

if TYPE_CHECKING:
    import pytest

_TEST_OPERATOR = OperatorIdentity(login="testop", uid=4242, home=Path("/home/testop"))


def _patch_operator(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("agent_app.cli.resolve_operator_identity", lambda: _TEST_OPERATOR)


def test_install_dry_run_prints_plan(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    def fake_discover_tools() -> ToolDiscovery:
        return ToolDiscovery()

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
    _patch_operator(monkeypatch)

    def fake_discover_tools() -> ToolDiscovery:
        return ToolDiscovery()

    def fake_install_no_start(
        config: InstallConfig,
        discovery: ToolDiscovery,
        **kwargs: object,
    ) -> InstallResult:
        captured["config"] = config
        captured["discovery"] = discovery
        return InstallResult(
            config_env=Path("config.env"),
            service_file=Path("service"),
            started=False,
        )

    monkeypatch.setattr(cli, "discover_tools", fake_discover_tools)
    monkeypatch.setattr(cli, "install_no_start", fake_install_no_start)

    assert cli.main(["install", "--no-start", "--manager-url", "https://manager.example.com"]) == 0

    assert isinstance(captured["config"], InstallConfig)
    assert captured["config"].manager_url == "https://manager.example.com"


def test_install_start_invokes_starting_installer(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}
    _patch_operator(monkeypatch)

    def fake_discover_tools() -> ToolDiscovery:
        return ToolDiscovery()

    def fake_install_with_start(config: InstallConfig, discovery: ToolDiscovery, **kwargs: object) -> InstallResult:
        captured["config"] = config
        captured["discovery"] = discovery
        return InstallResult(
            config_env=Path("config.env"),
            service_file=Path("service"),
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
    _patch_operator(monkeypatch)

    def fake_discover_tools() -> ToolDiscovery:
        return ToolDiscovery()

    def fake_install_with_start(config: InstallConfig, discovery: ToolDiscovery, **kwargs: object) -> InstallResult:
        return InstallResult(
            config_env=Path("config.env"),
            service_file=Path("service"),
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
    _patch_operator(monkeypatch)

    def fake_discover_tools() -> ToolDiscovery:
        return ToolDiscovery()

    def fake_install_with_start(config: InstallConfig, discovery: ToolDiscovery, **kwargs: object) -> InstallResult:
        return InstallResult(
            config_env=Path("config.env"),
            service_file=Path("service"),
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
    monkeypatch.setattr(
        cli,
        "resolve_operator_identity",
        lambda: OperatorIdentity(login="gridfleet", uid=os.getuid(), home=Path("/home/gridfleet")),
    )

    assert (
        cli.main(
            [
                "install",
                "--dry-run",
                "--manager-url",
                "https://manager.example.com",
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
    from agent_app.installer.uv_runtime import UvRuntime

    sentinel = object()
    fake_runtime = UvRuntime(bin_path=None, source="missing", searched=())

    def fake_collect_status(config: InstallConfig, **kwargs: object) -> object:
        assert isinstance(config, InstallConfig)
        return sentinel

    _patch_operator(monkeypatch)
    monkeypatch.setattr(cli, "discover_uv", lambda **kw: fake_runtime)
    monkeypatch.setattr(cli, "collect_status", fake_collect_status)
    monkeypatch.setattr(cli, "format_status", lambda status: "status text" if status is sentinel else "wrong")

    assert cli.main(["status"]) == 0

    assert capsys.readouterr().out == "status text\n"


def test_uninstall_requires_confirmation(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["uninstall"]) == 2

    assert "--yes" in capsys.readouterr().err


def test_uninstall_invokes_uninstaller(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    captured: dict[str, object] = {}
    _patch_operator(monkeypatch)

    def fake_uninstall(
        config: InstallConfig,
        *,
        operator: OperatorIdentity,
        remove_agent_dir: bool = True,
        remove_config_dir: bool = True,
    ) -> object:
        captured["config"] = config
        captured["operator"] = operator
        captured["remove_agent_dir"] = remove_agent_dir
        captured["remove_config_dir"] = remove_config_dir
        return object()

    monkeypatch.setattr(cli, "uninstall", fake_uninstall)

    assert cli.main(["uninstall", "--yes", "--keep-config", "--keep-agent-dir"]) == 0

    assert isinstance(captured["config"], InstallConfig)
    assert captured["operator"] == _TEST_OPERATOR
    assert captured["remove_agent_dir"] is False
    assert captured["remove_config_dir"] is False
    assert "GridFleet agent uninstalled" in capsys.readouterr().out


def test_update_dry_run_prints_plan(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    from agent_app.installer.uv_runtime import UvRuntime

    captured: dict[str, object] = {}
    loaded_config = InstallConfig(port=5300)
    fake_runtime = UvRuntime(bin_path=Path("/usr/bin/uv"), source="path", searched=())

    def fake_format_update_dry_run(
        config: InstallConfig,
        *,
        operator: OperatorIdentity,
        uv_runtime: UvRuntime,
        to_version: str | None = None,
        **_kw: object,
    ) -> str:
        captured["config"] = config
        captured["operator"] = operator
        captured["to_version"] = to_version
        return "update plan"

    _patch_operator(monkeypatch)
    monkeypatch.setattr(cli, "load_installed_config", lambda _defaults=None: loaded_config)
    monkeypatch.setattr(cli, "format_update_dry_run", fake_format_update_dry_run)
    monkeypatch.setattr(cli, "discover_uv", lambda **kw: fake_runtime)

    assert cli.main(["update", "--dry-run", "--to", "0.3.0"]) == 0

    assert captured["config"] == loaded_config
    assert captured["operator"] == _TEST_OPERATOR
    assert captured["to_version"] == "0.3.0"
    assert capsys.readouterr().out == "update plan\n"


def test_update_dry_run_reports_installed_config_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        cli, "load_installed_config", lambda _defaults=None: (_ for _ in ()).throw(ValueError("bad config"))
    )

    assert cli.main(["update", "--dry-run"]) == 2

    assert "ERROR: bad config" in capsys.readouterr().err


def test_update_invokes_updater(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    from agent_app.installer.uv_runtime import UvRuntime

    captured: dict[str, object] = {}
    loaded_config = InstallConfig(port=5300)
    fake_runtime = UvRuntime(bin_path=Path("/usr/bin/uv"), source="path", searched=())

    def fake_update_agent(
        config: InstallConfig,
        *,
        operator: OperatorIdentity,
        uv_runtime: UvRuntime,
        to_version: str | None = None,
        **_kw: object,
    ) -> UpdateResult:
        captured["config"] = config
        captured["operator"] = operator
        captured["to_version"] = to_version
        return UpdateResult(
            to_version=to_version,
            restarted=True,
            drain=DrainResult(ok=True, message="no active local nodes"),
            health=HealthCheckResult(ok=True, message="healthy"),
        )

    _patch_operator(monkeypatch)
    monkeypatch.setattr(cli, "load_installed_config", lambda _defaults=None: loaded_config)
    monkeypatch.setattr(cli, "update_agent", fake_update_agent)
    monkeypatch.setattr(cli, "discover_uv", lambda **kw: fake_runtime)

    assert cli.main(["update", "--to", "0.3.0"]) == 0

    assert captured["config"] == loaded_config
    assert captured["operator"] == _TEST_OPERATOR
    assert captured["to_version"] == "0.3.0"
    output = capsys.readouterr().out
    assert "Drain: no active local nodes" in output
    assert "GridFleet agent updated" in output


def test_install_parser_accepts_api_auth_flags() -> None:
    from agent_app.cli import _build_parser

    ns = _build_parser().parse_args(
        [
            "install",
            "--no-start",
            "--api-auth-username",
            "ops",
            "--api-auth-password",
            "secret",
        ]
    )
    assert ns.api_auth_username == "ops"
    assert ns.api_auth_password == "secret"


def test_install_cli_rejects_user_flag(capsys: pytest.CaptureFixture[str]) -> None:
    """--user has been removed; argparse must reject it."""
    exit_code = cli.main(
        [
            "install",
            "--no-start",
            "--manager-url",
            "http://localhost:8000",
            "--user",
            "anyone",
        ]
    )
    assert exit_code == 2


def test_status_cli_rejects_user_flag(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = cli.main(["status", "--user", "anyone"])
    assert exit_code == 2


def test_install_exit_zero_when_registration_pending_only(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    _patch_operator(monkeypatch)
    fake_result = SimpleNamespace(
        started=True,
        health=SimpleNamespace(ok=True, message="ok", details={}),
        registration=SimpleNamespace(ok=False, message="not yet"),
        linger_warning=None,
    )
    monkeypatch.setattr("agent_app.cli.install_with_start", lambda *a, **kw: fake_result)
    monkeypatch.setattr("agent_app.cli.discover_tools", lambda: None)
    rc = cli.main(["install", "--start", "--manager-url", "http://m"])
    assert rc == 0


def test_install_exit_one_when_health_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    _patch_operator(monkeypatch)
    fake_result = SimpleNamespace(
        started=True,
        health=SimpleNamespace(ok=False, message="bad", details={}),
        registration=None,
        linger_warning=None,
    )
    monkeypatch.setattr("agent_app.cli.install_with_start", lambda *a, **kw: fake_result)
    monkeypatch.setattr("agent_app.cli.discover_tools", lambda: None)
    rc = cli.main(["install", "--start", "--manager-url", "http://m"])
    assert rc == 1


def test_install_main_threads_api_auth_into_install_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """`cli.main` must construct an `InstallConfig` carrying the API auth fields."""
    from agent_app import cli
    from agent_app.installer.plan import ToolDiscovery

    captured: dict[str, InstallConfig] = {}
    _patch_operator(monkeypatch)

    def _fake_install_no_start(config: InstallConfig, _discovery: ToolDiscovery, **_kwargs: object) -> object:
        captured["config"] = config
        return type(
            "Result",
            (),
            {
                "started": False,
                "config_env": "",
                "service_file": "",
                "health": None,
                "registration": None,
            },
        )()

    monkeypatch.setattr(cli, "install_no_start", _fake_install_no_start)
    monkeypatch.setattr(cli, "discover_tools", lambda: ToolDiscovery())

    rc = cli.main(
        [
            "install",
            "--no-start",
            "--api-auth-username",
            "ops",
            "--api-auth-password",
            "secret",
        ]
    )
    assert rc == 0
    cfg = captured["config"]
    assert cfg.api_auth_username == "ops"
    assert cfg.api_auth_password == "secret"
