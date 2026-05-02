from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from agent_app.installer.plan import InstallConfig
from agent_app.installer.uninstall import UninstallResult, uninstall

if TYPE_CHECKING:
    import pytest


def _make_config(tmp_path: Path) -> InstallConfig:
    return InstallConfig(
        agent_dir=str(tmp_path / "opt/gridfleet-agent"),
        config_dir=str(tmp_path / "etc/gridfleet-agent"),
    )


def test_uninstall_linux_stops_disables_removes_files_and_reloads(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    agent_dir = Path(config.agent_dir)
    config_dir = Path(config.config_dir)
    service_file = tmp_path / "etc/systemd/system/gridfleet-agent.service"
    agent_dir.mkdir(parents=True)
    config_dir.mkdir(parents=True)
    service_file.parent.mkdir(parents=True)
    service_file.write_text("[Service]\n")
    commands: list[tuple[list[str], bool]] = []

    result = uninstall(
        config,
        os_name="Linux",
        run_command=lambda command, *, check=True: commands.append((command, check)),
    )

    assert result == UninstallResult(
        service_file=service_file,
        removed_service_file=True,
        removed_agent_dir=True,
        removed_config_dir=True,
    )
    assert commands == [
        (["systemctl", "stop", "gridfleet-agent"], False),
        (["systemctl", "disable", "gridfleet-agent"], False),
        (["systemctl", "daemon-reload"], True),
    ]
    assert not agent_dir.exists()
    assert not config_dir.exists()
    assert not service_file.exists()


def test_uninstall_macos_unloads_launchd_and_removes_files(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    config = _make_config(tmp_path)
    agent_dir = Path(config.agent_dir)
    config_dir = Path(config.config_dir)
    service_file = tmp_path / "Library/LaunchAgents/com.gridfleet.agent.plist"
    agent_dir.mkdir(parents=True)
    config_dir.mkdir(parents=True)
    service_file.parent.mkdir(parents=True)
    service_file.write_text("<plist />\n")
    commands: list[tuple[list[str], bool]] = []

    result = uninstall(
        config,
        os_name="Darwin",
        run_command=lambda command, *, check=True: commands.append((command, check)),
    )

    assert result.removed_service_file is True
    assert result.service_file == service_file
    assert commands == [(["launchctl", "unload", str(service_file)], False)]
    assert not agent_dir.exists()
    assert not config_dir.exists()
    assert not service_file.exists()


def test_uninstall_keep_flags_preserve_agent_and_config_dirs(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    agent_dir = Path(config.agent_dir)
    config_dir = Path(config.config_dir)
    agent_dir.mkdir(parents=True)
    config_dir.mkdir(parents=True)

    result = uninstall(
        config,
        os_name="Linux",
        run_command=lambda _command, *, check=True: None,
        remove_agent_dir=False,
        remove_config_dir=False,
    )

    assert result.removed_agent_dir is False
    assert result.removed_config_dir is False
    assert agent_dir.exists()
    assert config_dir.exists()


def test_uninstall_is_idempotent_when_files_are_missing(tmp_path: Path) -> None:
    config = _make_config(tmp_path)

    result = uninstall(
        config,
        os_name="Linux",
        run_command=lambda _command, *, check=True: None,
    )

    assert result.removed_service_file is False
    assert result.removed_agent_dir is False
    assert result.removed_config_dir is False
