from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from agent_app import cli
from agent_app.installer.identity import OperatorIdentity
from agent_app.installer.install import HealthCheckResult, InstallResult, RegistrationCheckResult
from agent_app.installer.plan import InstallConfig, ToolDiscovery
from agent_app.installer.status import AgentStatus
from agent_app.installer.update import DrainResult, UpdateResult
from agent_app.installer.uv_runtime import UvRuntime

_UvRuntime = UvRuntime

if TYPE_CHECKING:
    import pytest


def test_serve_runs_uvicorn_with_default_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded: dict[str, Any] = {}
    monkeypatch.setattr(cli.agent_settings, "agent_port", 5301)

    def fake_run(app: str, *, host: str, port: int) -> None:
        recorded.update({"app": app, "host": host, "port": port})

    monkeypatch.setattr(cli.uvicorn, "run", fake_run)

    assert cli.main(["serve"]) == 0
    assert recorded == {"app": "agent_app.main:app", "host": "0.0.0.0", "port": cli.agent_settings.agent_port}


def test_serve_allows_host_and_port_override(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded: dict[str, Any] = {}

    def fake_run(app: str, *, host: str, port: int) -> None:
        recorded.update({"app": app, "host": host, "port": port})

    monkeypatch.setattr(cli.uvicorn, "run", fake_run)

    assert cli.main(["serve", "--host", "127.0.0.1", "--port", "5200"]) == 0
    assert recorded == {"app": "agent_app.main:app", "host": "127.0.0.1", "port": 5200}


def test_version_prints_public_package_version(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["--version"]) == 0

    assert capsys.readouterr().out == f"gridfleet-agent {cli.__version__}\n"


def test_install_rejects_multiple_modes(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["install", "--dry-run", "--start"]) == 2
    assert "choose only one" in capsys.readouterr().err


def test_install_requires_a_mode(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["install"]) == 2
    assert "pass --dry-run" in capsys.readouterr().err


def test_install_dry_run(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        cli, "resolve_operator_identity", lambda: OperatorIdentity(login="root", uid=0, home=Path("/root"))
    )
    monkeypatch.setattr(
        cli, "discover_tools", lambda: ToolDiscovery(node_bin_dir="/usr/bin", android_home="/opt/android-sdk")
    )
    monkeypatch.setattr(cli, "format_dry_run", lambda config, discovery: "DRY RUN")
    assert cli.main(["install", "--dry-run"]) == 0
    assert capsys.readouterr().out == "DRY RUN\n"


def test_install_no_start(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        cli, "resolve_operator_identity", lambda: OperatorIdentity(login="root", uid=0, home=Path("/root"))
    )
    monkeypatch.setattr(
        cli, "discover_tools", lambda: ToolDiscovery(node_bin_dir="/usr/bin", android_home="/opt/android-sdk")
    )
    monkeypatch.setattr(
        cli,
        "install_no_start",
        lambda config, discovery, operator: InstallResult(
            config_env=Path("/etc/gridfleet-agent/config.env"),
            service_file=Path("/etc/systemd/system/gridfleet-agent.service"),
            started=False,
        ),
    )
    assert cli.main(["install", "--no-start"]) == 0
    assert "installed. Service was not started." in capsys.readouterr().out


def test_install_start_with_health_warning(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        cli, "resolve_operator_identity", lambda: OperatorIdentity(login="root", uid=0, home=Path("/root"))
    )
    monkeypatch.setattr(
        cli, "discover_tools", lambda: ToolDiscovery(node_bin_dir="/usr/bin", android_home="/opt/android-sdk")
    )
    result = InstallResult(
        config_env=Path("/etc/gridfleet-agent/config.env"),
        service_file=Path("/etc/systemd/system/gridfleet-agent.service"),
        started=True,
        health=HealthCheckResult(ok=False, message="not healthy"),
    )
    monkeypatch.setattr(cli, "install_with_start", lambda config, discovery, operator: result)
    assert cli.main(["install", "--start"]) == 1
    out, err = capsys.readouterr()
    assert "started" in out
    assert "not healthy" in err


def test_install_start_with_registration(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        cli, "resolve_operator_identity", lambda: OperatorIdentity(login="root", uid=0, home=Path("/root"))
    )
    monkeypatch.setattr(
        cli, "discover_tools", lambda: ToolDiscovery(node_bin_dir="/usr/bin", android_home="/opt/android-sdk")
    )
    result = InstallResult(
        config_env=Path("/etc/gridfleet-agent/config.env"),
        service_file=Path("/etc/systemd/system/gridfleet-agent.service"),
        started=True,
        health=HealthCheckResult(ok=True, message="ok"),
        registration=RegistrationCheckResult(ok=True, message="registered"),
    )
    monkeypatch.setattr(cli, "install_with_start", lambda config, discovery, operator: result)
    assert cli.main(["install", "--start"]) == 0
    out = capsys.readouterr().out
    assert "started" in out
    assert "registered" in out


def test_install_resolve_identity_error(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "resolve_operator_identity", lambda: (_ for _ in ()).throw(ValueError("bad user")))
    assert cli.main(["install", "--dry-run"]) == 2
    assert "bad user" in capsys.readouterr().err


def test_install_config_error(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        cli, "resolve_operator_identity", lambda: OperatorIdentity(login="root", uid=0, home=Path("/root"))
    )
    # Cause InstallConfig to reject: enable terminal without token
    assert cli.main(["install", "--dry-run", "--enable-web-terminal"]) == 2
    assert "AGENT_TERMINAL_TOKEN" in capsys.readouterr().err


def test_install_runtime_error(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        cli, "resolve_operator_identity", lambda: OperatorIdentity(login="root", uid=0, home=Path("/root"))
    )
    monkeypatch.setattr(
        cli, "discover_tools", lambda: ToolDiscovery(node_bin_dir="/usr/bin", android_home="/opt/android-sdk")
    )
    monkeypatch.setattr(
        cli, "install_no_start", lambda config, discovery, operator: (_ for _ in ()).throw(RuntimeError("disk full"))
    )
    assert cli.main(["install", "--no-start"]) == 2
    assert "disk full" in capsys.readouterr().err


def test_status(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        cli, "resolve_operator_identity", lambda: OperatorIdentity(login="root", uid=0, home=Path("/root"))
    )
    uv = UvRuntime(bin_path=Path("/usr/bin/uv"), source="test", searched=())
    monkeypatch.setattr(cli, "discover_uv", lambda operator, override: uv)
    status = AgentStatus(
        config_env=Path("/etc/gridfleet-agent/config.env"),
        config_exists=True,
        config_error=None,
        service_file=Path("/etc/systemd/system/gridfleet-agent.service"),
        service_exists=True,
        service_active="active",
        service_enabled="enabled",
        health=HealthCheckResult(ok=True, message="ok"),
        operator=OperatorIdentity(login="root", uid=0, home=Path("/root")),
        uv=uv,
        env={"AGENT_MANAGER_URL": "http://localhost:8000"},
    )
    monkeypatch.setattr(cli, "collect_status", lambda config, operator, uv_runtime: status)
    monkeypatch.setattr(cli, "format_status", lambda s: "STATUS")
    assert cli.main(["status"]) == 0
    assert capsys.readouterr().out == "STATUS\n"


def test_status_resolve_identity_error(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "resolve_operator_identity", lambda: (_ for _ in ()).throw(ValueError("bad user")))
    assert cli.main(["status"]) == 2
    assert "bad user" in capsys.readouterr().err


def test_uninstall_requires_yes(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["uninstall"]) == 2
    assert "requires --yes" in capsys.readouterr().err


def test_uninstall(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        cli, "resolve_operator_identity", lambda: OperatorIdentity(login="root", uid=0, home=Path("/root"))
    )
    monkeypatch.setattr(cli, "uninstall", lambda config, operator, remove_agent_dir, remove_config_dir: None)
    assert cli.main(["uninstall", "--yes"]) == 0
    assert "uninstalled" in capsys.readouterr().out


def test_uninstall_resolve_identity_error(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "resolve_operator_identity", lambda: (_ for _ in ()).throw(ValueError("bad user")))
    assert cli.main(["uninstall", "--yes"]) == 2
    assert "bad user" in capsys.readouterr().err


def test_uninstall_runtime_error(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        cli, "resolve_operator_identity", lambda: OperatorIdentity(login="root", uid=0, home=Path("/root"))
    )
    monkeypatch.setattr(
        cli,
        "uninstall",
        lambda config, operator, remove_agent_dir, remove_config_dir: (_ for _ in ()).throw(
            OSError("permission denied")
        ),
    )
    assert cli.main(["uninstall", "--yes"]) == 2
    assert "permission denied" in capsys.readouterr().err


def test_update_dry_run(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        cli, "resolve_operator_identity", lambda: OperatorIdentity(login="root", uid=0, home=Path("/root"))
    )
    config = InstallConfig()
    monkeypatch.setattr(cli, "load_installed_config", lambda _defaults=None: config)
    uv = UvRuntime(bin_path=Path("/usr/bin/uv"), source="test", searched=())
    monkeypatch.setattr(cli, "discover_uv", lambda operator, override: uv)
    monkeypatch.setattr(cli, "format_update_dry_run", lambda config, operator, uv_runtime, to_version: "DRY RUN")
    assert cli.main(["update", "--dry-run"]) == 0
    assert capsys.readouterr().out == "DRY RUN\n"


def test_update(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        cli, "resolve_operator_identity", lambda: OperatorIdentity(login="root", uid=0, home=Path("/root"))
    )
    config = InstallConfig()
    monkeypatch.setattr(cli, "load_installed_config", lambda _defaults=None: config)
    uv = UvRuntime(bin_path=Path("/usr/bin/uv"), source="test", searched=())
    monkeypatch.setattr(cli, "discover_uv", lambda operator, override: uv)
    result = UpdateResult(
        to_version="1.0.0",
        restarted=True,
        drain=DrainResult(ok=True, message="drained"),
        health=HealthCheckResult(ok=True, message="ok"),
    )
    monkeypatch.setattr(cli, "update_agent", lambda config, operator, uv_runtime, to_version: result)
    assert cli.main(["update"]) == 0
    out = capsys.readouterr().out
    assert "updated" in out


def test_update_resolve_identity_error(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "resolve_operator_identity", lambda: (_ for _ in ()).throw(ValueError("bad user")))
    assert cli.main(["update"]) == 2
    assert "bad user" in capsys.readouterr().err


def test_update_load_config_error(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        cli, "resolve_operator_identity", lambda: OperatorIdentity(login="root", uid=0, home=Path("/root"))
    )
    monkeypatch.setattr(
        cli, "load_installed_config", lambda _defaults=None: (_ for _ in ()).throw(OSError("not found"))
    )
    assert cli.main(["update"]) == 2
    assert "not found" in capsys.readouterr().err


def test_update_discover_uv_error(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        cli, "resolve_operator_identity", lambda: OperatorIdentity(login="root", uid=0, home=Path("/root"))
    )
    config = InstallConfig()
    monkeypatch.setattr(cli, "load_installed_config", lambda _defaults=None: config)
    monkeypatch.setattr(
        cli, "discover_uv", lambda operator, override: (_ for _ in ()).throw(RuntimeError("missing uv"))
    )
    assert cli.main(["update"]) == 1
    assert "missing uv" in capsys.readouterr().err


def test_update_drain_error(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    from agent_app.installer.update import UpdateDrainError

    monkeypatch.setattr(
        cli, "resolve_operator_identity", lambda: OperatorIdentity(login="root", uid=0, home=Path("/root"))
    )
    config = InstallConfig()
    monkeypatch.setattr(cli, "load_installed_config", lambda _defaults=None: config)
    uv = UvRuntime(bin_path=Path("/usr/bin/uv"), source="test", searched=())
    monkeypatch.setattr(cli, "discover_uv", lambda operator, override: uv)
    monkeypatch.setattr(
        cli,
        "update_agent",
        lambda config, operator, uv_runtime, to_version: (_ for _ in ()).throw(UpdateDrainError("busy")),
    )
    assert cli.main(["update"]) == 1
    assert "busy" in capsys.readouterr().err


def test_update_upgrade_error(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    from agent_app.installer.update import UpdateUpgradeError

    monkeypatch.setattr(
        cli, "resolve_operator_identity", lambda: OperatorIdentity(login="root", uid=0, home=Path("/root"))
    )
    config = InstallConfig()
    monkeypatch.setattr(cli, "load_installed_config", lambda _defaults=None: config)
    uv = UvRuntime(bin_path=Path("/usr/bin/uv"), source="test", searched=())
    monkeypatch.setattr(cli, "discover_uv", lambda operator, override: uv)
    monkeypatch.setattr(
        cli,
        "update_agent",
        lambda config, operator, uv_runtime, to_version: (_ for _ in ()).throw(UpdateUpgradeError("fail")),
    )
    assert cli.main(["update"]) == 2
    assert "fail" in capsys.readouterr().err


def test_update_restart_error(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    from agent_app.installer.update import UpdateRestartError

    monkeypatch.setattr(
        cli, "resolve_operator_identity", lambda: OperatorIdentity(login="root", uid=0, home=Path("/root"))
    )
    config = InstallConfig()
    monkeypatch.setattr(cli, "load_installed_config", lambda _defaults=None: config)
    uv = UvRuntime(bin_path=Path("/usr/bin/uv"), source="test", searched=())
    monkeypatch.setattr(cli, "discover_uv", lambda operator, override: uv)
    monkeypatch.setattr(
        cli,
        "update_agent",
        lambda config, operator, uv_runtime, to_version: (_ for _ in ()).throw(UpdateRestartError("restart fail")),
    )
    assert cli.main(["update"]) == 2
    assert "restart fail" in capsys.readouterr().err


def test_main_no_args(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main([]) == 2
    assert "usage" in capsys.readouterr().out.lower() or "commands" in capsys.readouterr().out.lower()
