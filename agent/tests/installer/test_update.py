from __future__ import annotations

from typing import TYPE_CHECKING

from agent_app.installer.install import HealthCheckResult
from agent_app.installer.plan import InstallConfig
from agent_app.installer.update import (
    DrainResult,
    UpdateResult,
    format_update_dry_run,
    update_agent,
    wait_for_update_drain,
)

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def _make_config(tmp_path: Path) -> InstallConfig:
    return InstallConfig(
        agent_dir=str(tmp_path / "opt/gridfleet-agent"),
        config_dir=str(tmp_path / "etc/gridfleet-agent"),
        port=5200,
    )


def test_format_update_dry_run_names_uv_and_restart_commands(tmp_path: Path) -> None:
    config = _make_config(tmp_path)

    output = format_update_dry_run(config, to_version="0.3.0", os_name="Linux")

    assert "GridFleet Agent update dry run" in output
    assert "uv tool upgrade gridfleet-agent==0.3.0" in output
    assert "systemctl restart gridfleet-agent" in output
    assert "Wait for active local nodes to drain" in output
    assert "http://localhost:5200/agent/health" in output


def test_format_update_dry_run_reports_unsupported_os_without_traceback(tmp_path: Path) -> None:
    config = _make_config(tmp_path)

    output = format_update_dry_run(config, os_name="Plan9")

    assert "Restart service: unsupported OS: Plan9" in output


def test_update_agent_waits_for_drain_then_runs_uv_restart_and_health_check_on_linux(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    commands: list[list[str]] = []
    drains: list[str] = []

    def record(command: list[str]) -> None:
        commands.append(command)

    result = update_agent(
        config,
        to_version="0.3.0",
        os_name="Linux",
        run_command=record,
        drain_check=lambda url, *, auth=None: drains.append(url) or DrainResult(ok=True, message="drained"),
        health_check=lambda url, *, auth=None: HealthCheckResult(ok=True, message=f"healthy at {url}"),
    )

    assert result == UpdateResult(
        to_version="0.3.0",
        restarted=True,
        drain=DrainResult(ok=True, message="drained"),
        health=HealthCheckResult(ok=True, message="healthy at http://localhost:5200/agent/health"),
    )
    assert drains == ["http://localhost:5200/agent/health"]
    assert commands == [
        ["uv", "tool", "upgrade", "gridfleet-agent==0.3.0"],
        ["systemctl", "restart", "gridfleet-agent"],
    ]


def test_update_agent_without_version_upgrades_latest(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    commands: list[list[str]] = []

    def record(command: list[str]) -> None:
        commands.append(command)

    update_agent(
        config,
        to_version=None,
        os_name="Linux",
        run_command=record,
        drain_check=lambda _url, *, auth=None: DrainResult(ok=True, message="drained"),
        health_check=lambda _url, *, auth=None: HealthCheckResult(ok=True, message="healthy"),
    )

    assert commands[0] == ["uv", "tool", "upgrade", "gridfleet-agent"]


def test_update_agent_restarts_launchd_on_macos(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    commands: list[list[str]] = []

    def record(command: list[str]) -> None:
        commands.append(command)

    update_agent(
        config,
        to_version="0.3.0",
        os_name="Darwin",
        run_command=record,
        drain_check=lambda _url, *, auth=None: DrainResult(ok=True, message="drained"),
        health_check=lambda _url, *, auth=None: HealthCheckResult(ok=False, message="health failed"),
        uid=0,
    )

    assert commands == [
        ["uv", "tool", "upgrade", "gridfleet-agent==0.3.0"],
        ["launchctl", "kickstart", "-k", "gui/0/com.gridfleet.agent"],
    ]


def test_update_agent_uses_sudo_uid_for_launchd_restart_on_macos(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("SUDO_UID", "501")
    monkeypatch.setattr("agent_app.installer.update.os.getuid", lambda: 0)
    config = _make_config(tmp_path)
    commands: list[list[str]] = []

    def record(command: list[str]) -> None:
        commands.append(command)

    update_agent(
        config,
        to_version=None,
        os_name="Darwin",
        run_command=record,
        drain_check=lambda _url, *, auth=None: DrainResult(ok=True, message="drained"),
        health_check=lambda _url, *, auth=None: HealthCheckResult(ok=True, message="healthy"),
    )

    assert commands[1] == ["launchctl", "kickstart", "-k", "gui/501/com.gridfleet.agent"]


def test_update_agent_refuses_to_upgrade_when_drain_times_out(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    commands: list[list[str]] = []

    try:
        update_agent(
            config,
            to_version="0.3.0",
            os_name="Linux",
            run_command=lambda command: commands.append(command),
            drain_check=lambda _url, *, auth=None: DrainResult(ok=False, message="active local nodes remain"),
            health_check=lambda _url, *, auth=None: HealthCheckResult(ok=True, message="healthy"),
        )
    except RuntimeError as exc:
        assert "active local nodes remain" in str(exc)
    else:
        raise AssertionError("expected RuntimeError")

    assert commands == []


def test_update_agent_raises_when_uv_not_installed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr("shutil.which", lambda name: None)
    config = _make_config(tmp_path)

    try:
        update_agent(
            config,
            to_version="0.3.0",
            os_name="Linux",
            run_command=lambda _command: None,
            drain_check=lambda _url, *, auth=None: DrainResult(ok=True, message="drained"),
            health_check=lambda _url, *, auth=None: HealthCheckResult(ok=True, message="healthy"),
        )
    except RuntimeError as exc:
        assert "uv is not installed" in str(exc)
    else:
        raise AssertionError("expected RuntimeError")


def test_wait_for_update_drain_returns_success_when_running_nodes_are_empty() -> None:
    class Response:
        status_code = 200

        @staticmethod
        def json() -> dict[str, object]:
            return {"appium_processes": {"running_nodes": []}}

    result = wait_for_update_drain(
        "http://localhost:5200/agent/health",
        timeout_sec=0.1,
        interval_sec=0.01,
        get=lambda _url, timeout=2.0: Response(),
    )

    assert result == DrainResult(ok=True, message="no active local nodes")


def test_wait_for_update_drain_times_out_while_running_nodes_remain() -> None:
    class Response:
        status_code = 200

        @staticmethod
        def json() -> dict[str, object]:
            return {"appium_processes": {"running_nodes": [{"port": 4723}]}}

    result = wait_for_update_drain(
        "http://localhost:5200/agent/health",
        timeout_sec=0.01,
        interval_sec=0.01,
        get=lambda _url, timeout=2.0: Response(),
    )

    assert result.ok is False
    assert "1 active local node" in result.message


def test_wait_for_update_drain_passes_basic_auth() -> None:
    class Response:
        status_code = 200

        @staticmethod
        def json() -> dict[str, object]:
            return {"appium_processes": {"running_nodes": []}}

    captured: list[tuple[str, str] | None] = []

    def fake_get(url: str, *, timeout: float = 2.0, auth: tuple[str, str] | None = None) -> Response:
        captured.append(auth)
        return Response()

    result = wait_for_update_drain(
        "http://localhost:5200/agent/health",
        timeout_sec=5.0,
        interval_sec=0.01,
        get=fake_get,
        auth=("ops", "secret"),
    )

    assert result == DrainResult(ok=True, message="no active local nodes")
    assert captured == [("ops", "secret")]


def test_update_agent_forwards_api_auth_to_drain_and_health(tmp_path: Path) -> None:
    config = InstallConfig(
        agent_dir=str(tmp_path / "opt/gridfleet-agent"),
        config_dir=str(tmp_path / "etc/gridfleet-agent"),
        port=5200,
        api_auth_username="ops",
        api_auth_password="secret",
    )

    drain_auths: list[tuple[str, str] | None] = []
    health_auths: list[tuple[str, str] | None] = []

    def fake_drain(url: str, *, auth: tuple[str, str] | None = None) -> DrainResult:
        drain_auths.append(auth)
        return DrainResult(ok=True, message="drained")

    def fake_health(url: str, *, auth: tuple[str, str] | None = None) -> HealthCheckResult:
        health_auths.append(auth)
        return HealthCheckResult(ok=True, message="healthy")

    update_agent(
        config,
        to_version=None,
        os_name="Linux",
        run_command=lambda _command: None,
        drain_check=fake_drain,
        health_check=fake_health,
    )

    assert drain_auths == [("ops", "secret")]
    assert health_auths == [("ops", "secret")]
