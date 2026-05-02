from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from agent_app.installer.install import HealthCheckResult
from agent_app.installer.plan import InstallConfig
from agent_app.installer.status import _run_status_command, collect_status, format_status, parse_config_env

if TYPE_CHECKING:
    import pytest


def _make_config(tmp_path: Path) -> InstallConfig:
    return InstallConfig(
        agent_dir=str(tmp_path / "opt/gridfleet-agent"),
        config_dir=str(tmp_path / "etc/gridfleet-agent"),
    )


def test_parse_config_env_skips_comments_and_blank_lines(tmp_path: Path) -> None:
    path = tmp_path / "config.env"
    path.write_text("\n# comment\nAGENT_MANAGER_URL=https://manager.example.com\nAGENT_AGENT_PORT=5200\nMALFORMED\n")

    assert parse_config_env(path) == {
        "AGENT_MANAGER_URL": "https://manager.example.com",
        "AGENT_AGENT_PORT": "5200",
    }


def test_collect_status_reads_files_service_state_and_health(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    Path(config.config_dir).mkdir(parents=True)
    Path(config.config_env_path).write_text(
        "AGENT_MANAGER_URL=https://manager.example.com\nAGENT_AGENT_PORT=5200\nAGENT_MANAGER_AUTH_PASSWORD=secret\n"
    )
    service_file = tmp_path / "etc/systemd/system/gridfleet-agent.service"
    service_file.parent.mkdir(parents=True)
    service_file.write_text("[Service]\n")
    commands: list[list[str]] = []

    def fake_command(command: list[str]) -> str:
        commands.append(command)
        if command == ["systemctl", "is-active", "gridfleet-agent"]:
            return "active\n"
        if command == ["systemctl", "is-enabled", "gridfleet-agent"]:
            return "enabled\n"
        raise AssertionError(command)

    status = collect_status(
        config,
        os_name="Linux",
        run_command=fake_command,
        health_check=lambda url: HealthCheckResult(ok=True, message=f"healthy at {url}"),
    )

    assert status.config_exists is True
    assert status.service_file == service_file
    assert status.service_exists is True
    assert status.service_active == "active"
    assert status.service_enabled == "enabled"
    assert status.health == HealthCheckResult(ok=True, message="healthy at http://localhost:5200/agent/health")
    assert status.env["AGENT_MANAGER_URL"] == "https://manager.example.com"
    assert commands == [
        ["systemctl", "is-active", "gridfleet-agent"],
        ["systemctl", "is-enabled", "gridfleet-agent"],
    ]


def test_collect_status_handles_missing_config_without_health_check(tmp_path: Path) -> None:
    config = _make_config(tmp_path)

    status = collect_status(
        config,
        os_name="Linux",
        run_command=lambda _command: "inactive\n",
        health_check=lambda _url: HealthCheckResult(ok=True, message="should not run"),
    )

    assert status.config_exists is False
    assert status.health == HealthCheckResult(ok=False, message="config.env missing; health check skipped")


def test_collect_status_reports_unreadable_config_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    config_env = Path(config.config_env_path)
    config_env.parent.mkdir(parents=True)
    config_env.write_text("AGENT_AGENT_PORT=5200\n")
    original_read_text = Path.read_text

    def fake_read_text(path: Path, *args: object, **kwargs: object) -> str:
        if path == config_env:
            raise PermissionError("permission denied")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fake_read_text)

    status = collect_status(
        config,
        os_name="Linux",
        run_command=lambda _command: "inactive\n",
        health_check=lambda _url: HealthCheckResult(ok=False, message="down"),
    )

    assert status.config_exists is True
    assert status.config_error is not None
    assert "permission denied" in status.config_error
    assert "Config read: failed" in format_status(status)


def test_run_status_command_reports_missing_service_manager(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(*_args: object, **_kwargs: object) -> object:
        raise FileNotFoundError("systemctl")

    monkeypatch.setattr("agent_app.installer.status.subprocess.run", fake_run)

    assert _run_status_command(["systemctl", "is-active", "gridfleet-agent"]) == "systemctl unavailable: systemctl"


def test_collect_status_uses_launchctl_on_macos(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    config = _make_config(tmp_path)
    Path(config.config_dir).mkdir(parents=True)
    Path(config.config_env_path).write_text("AGENT_AGENT_PORT=5200\n")
    commands: list[list[str]] = []

    collect_status(
        config,
        os_name="Darwin",
        run_command=lambda command: commands.append(command) or "123\t0\tcom.gridfleet.agent\n",
        health_check=lambda _url: HealthCheckResult(ok=False, message="down"),
    )

    assert commands == [["launchctl", "list", "com.gridfleet.agent"]]


def test_format_status_redacts_secrets() -> None:
    status = collect_status(
        InstallConfig(),
        os_name="Linux",
        env={
            "AGENT_MANAGER_URL": "https://manager.example.com",
            "AGENT_MANAGER_AUTH_PASSWORD": "secret",
            "AGENT_TERMINAL_TOKEN": "terminal-token",
            "AGENT_AGENT_PORT": "5200",
        },
        run_command=lambda _command: "active\n",
        health_check=lambda _url: HealthCheckResult(ok=True, message="healthy"),
    )

    output = format_status(status)

    assert "GridFleet Agent status" in output
    assert "https://manager.example.com" in output
    assert "secret" not in output
    assert "terminal-token" not in output
    assert "AGENT_MANAGER_AUTH_PASSWORD=<redacted>" in output
    assert "AGENT_TERMINAL_TOKEN=<redacted>" in output
    assert "Local health: ok - healthy" in output
