from __future__ import annotations

from pathlib import Path

import pytest

from agent_app.installer.install import InstallResult, install_no_start, validate_dedicated_venv
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


def test_install_no_start_rejects_start_request_until_implemented(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    executable = Path(config.venv_bin_dir) / "gridfleet-agent"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\n")

    with pytest.raises(NotImplementedError, match="service start is not implemented"):
        install_no_start(
            config,
            ToolDiscovery(),
            os_name="Linux",
            executable=executable,
            download=lambda _url, dest: dest.write_text("selenium"),
            start=True,
        )
