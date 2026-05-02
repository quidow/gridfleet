from __future__ import annotations

from pathlib import Path

import pytest

from agent_app.installer.install import (
    HealthCheckResult,
    InstallResult,
    install_no_start,
    install_with_start,
    poll_agent_health,
    validate_dedicated_venv,
)
from agent_app.installer.plan import InstallConfig, ToolDiscovery


def _make_config(tmp_path: Path) -> InstallConfig:
    return InstallConfig(
        agent_dir=str(tmp_path / "opt/gridfleet-agent"),
        config_dir=str(tmp_path / "etc/gridfleet-agent"),
        manager_url="https://manager.example.com",
        port=5200,
    )


def test_validate_dedicated_venv_accepts_expected_console_script(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    executable = Path(config.venv_bin_dir) / "gridfleet-agent"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\n")

    validate_dedicated_venv(config, executable=executable)


def test_validate_dedicated_venv_rejects_wrong_console_script_path(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    executable = tmp_path / "other/bin/gridfleet-agent"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\n")

    with pytest.raises(RuntimeError, match="/venv/bin/gridfleet-agent"):
        validate_dedicated_venv(config, executable=executable)


def test_install_no_start_writes_config_runtime_dir_service_and_downloads_selenium(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    executable = Path(config.venv_bin_dir) / "gridfleet-agent"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\n")
    downloads: list[tuple[str, Path]] = []

    def fake_download(url: str, dest: Path) -> None:
        downloads.append((url, dest))
        dest.write_text("selenium")

    result = install_no_start(
        config,
        ToolDiscovery(java_bin="/usr/bin/java"),
        os_name="Linux",
        executable=executable,
        download=fake_download,
    )

    assert result == InstallResult(
        config_env=Path(config.config_env_path),
        service_file=tmp_path / "etc/systemd/system/gridfleet-agent.service",
        selenium_jar=Path(config.selenium_jar),
        started=False,
    )
    assert (Path(config.agent_dir) / "runtimes").is_dir()
    assert Path(config.config_env_path).read_text().startswith("AGENT_MANAGER_URL=https://manager.example.com\n")
    assert "ExecStart=" + str(executable) in result.service_file.read_text()
    assert downloads == [
        (
            "https://github.com/SeleniumHQ/selenium/releases/download/selenium-4.41.0/selenium-server-4.41.0.jar",
            Path(config.selenium_jar),
        )
    ]


def test_install_no_start_skips_existing_selenium_jar(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    executable = Path(config.venv_bin_dir) / "gridfleet-agent"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\n")
    selenium_jar = Path(config.selenium_jar)
    selenium_jar.parent.mkdir(parents=True, exist_ok=True)
    selenium_jar.write_text("already present")

    def fail_download(_url: str, _dest: Path) -> None:
        raise AssertionError("download should not run")

    install_no_start(
        config,
        ToolDiscovery(),
        os_name="Linux",
        executable=executable,
        download=fail_download,
    )

    assert selenium_jar.read_text() == "already present"


def test_install_no_start_uses_launchd_path_on_macos(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    executable = Path(config.venv_bin_dir) / "gridfleet-agent"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\n")

    result = install_no_start(
        config,
        ToolDiscovery(),
        os_name="Darwin",
        executable=executable,
        download=lambda _url, dest: dest.write_text("selenium"),
    )

    assert result.service_file == tmp_path / "Library/LaunchAgents/com.gridfleet.agent.plist"
    assert "<string>com.gridfleet.agent</string>" in result.service_file.read_text()


def test_install_with_start_runs_systemd_commands_and_health_check(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    executable = Path(config.venv_bin_dir) / "gridfleet-agent"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\n")
    commands: list[list[str]] = []
    health_urls: list[str] = []

    def fake_run(command: list[str]) -> None:
        commands.append(command)

    def fake_health(url: str, timeout_sec: float = 30.0, interval_sec: float = 1.0) -> HealthCheckResult:
        health_urls.append(url)
        return HealthCheckResult(ok=True, message="healthy")

    result = install_with_start(
        config,
        ToolDiscovery(),
        os_name="Linux",
        executable=executable,
        download=lambda _url, dest: dest.write_text("selenium"),
        run_command=fake_run,
        health_check=fake_health,
    )

    assert result.started is True
    assert result.health == HealthCheckResult(ok=True, message="healthy")
    assert commands == [
        ["systemctl", "daemon-reload"],
        ["systemctl", "enable", "gridfleet-agent"],
        ["systemctl", "start", "gridfleet-agent"],
    ]
    assert health_urls == ["http://localhost:5200/agent/health"]


def test_install_with_start_runs_launchctl_load_on_macos(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    executable = Path(config.venv_bin_dir) / "gridfleet-agent"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\n")
    commands: list[list[str]] = []

    result = install_with_start(
        config,
        ToolDiscovery(),
        os_name="Darwin",
        executable=executable,
        download=lambda _url, dest: dest.write_text("selenium"),
        run_command=lambda command: commands.append(command),
        health_check=lambda _url: HealthCheckResult(ok=False, message="health check timed out"),
    )

    assert result.started is True
    assert result.health == HealthCheckResult(ok=False, message="health check timed out")
    assert commands == [["launchctl", "load", str(result.service_file)]]


def test_install_with_start_raises_when_service_command_fails(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    executable = Path(config.venv_bin_dir) / "gridfleet-agent"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\n")

    def fail_command(_command: list[str]) -> None:
        raise RuntimeError("systemctl failed")

    with pytest.raises(RuntimeError, match="systemctl failed"):
        install_with_start(
            config,
            ToolDiscovery(),
            os_name="Linux",
            executable=executable,
            download=lambda _url, dest: dest.write_text("selenium"),
            run_command=fail_command,
            health_check=lambda _url: HealthCheckResult(ok=True, message="healthy"),
        )


def test_poll_agent_health_returns_success_on_http_200() -> None:
    attempts: list[str] = []

    def fake_get(url: str, timeout: float = 2.0) -> object:
        attempts.append(url)

        class Response:
            status_code = 200

        return Response()

    result = poll_agent_health("http://localhost:5200/agent/health", timeout_sec=0.1, interval_sec=0.01, get=fake_get)

    assert result == HealthCheckResult(ok=True, message="agent health check passed")
    assert attempts == ["http://localhost:5200/agent/health"]


def test_poll_agent_health_times_out_after_failed_attempts() -> None:
    def fail_get(_url: str, timeout: float = 2.0) -> object:
        raise OSError("connection refused")

    result = poll_agent_health("http://localhost:5200/agent/health", timeout_sec=0.01, interval_sec=0.01, get=fail_get)

    assert result.ok is False
    assert "connection refused" in result.message
