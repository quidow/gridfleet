from __future__ import annotations

from pathlib import Path

import pytest

from agent_app.installer.identity import OperatorIdentity
from agent_app.installer.plan import InstallConfig
from agent_app.installer.uninstall import UninstallResult, uninstall

_LINUX_OPERATOR = OperatorIdentity(login="ops", uid=1000, home=Path("/home/ops"))


@pytest.fixture(autouse=True)
def _patch_legacy_paths(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr("agent_app.installer.install._LEGACY_PATHS", (tmp_path / "nope",))


def _make_config(tmp_path: Path) -> InstallConfig:
    return InstallConfig(
        agent_dir=str(tmp_path / "opt/gridfleet-agent"),
        config_dir=str(tmp_path / "etc/gridfleet-agent"),
    )


def test_uninstall_linux_stops_disables_removes_files_and_reloads(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))  # type: ignore[arg-type]
    config = _make_config(tmp_path)
    agent_dir = Path(config.agent_dir)
    config_dir = Path(config.config_dir)
    service_file = tmp_path / ".config/systemd/user/gridfleet-agent.service"
    agent_dir.mkdir(parents=True)
    config_dir.mkdir(parents=True)
    service_file.parent.mkdir(parents=True)
    service_file.write_text("[Service]\n")
    commands: list[tuple[list[str], bool]] = []

    result = uninstall(
        config,
        operator=_LINUX_OPERATOR,
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
        (["systemctl", "--user", "stop", "gridfleet-agent"], False),
        (["systemctl", "--user", "disable", "gridfleet-agent"], False),
        (["systemctl", "--user", "daemon-reload"], True),
    ]
    assert not agent_dir.exists()
    assert not config_dir.exists()
    assert not service_file.exists()


def test_uninstall_macos_boots_out_launchd_domain_and_removes_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
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

    darwin_operator = OperatorIdentity(login="ops", uid=501, home=tmp_path)
    result = uninstall(
        config,
        operator=darwin_operator,
        os_name="Darwin",
        run_command=lambda command, *, check=True: commands.append((command, check)),
    )

    assert result.removed_service_file is True
    assert result.service_file == service_file
    assert commands == [(["launchctl", "bootout", "gui/501/com.gridfleet.agent"], False)]
    assert not agent_dir.exists()
    assert not config_dir.exists()
    assert not service_file.exists()


@pytest.mark.parametrize("os_name", ["Linux", "Darwin"])
def test_uninstall_keep_flags_preserve_agent_and_config_dirs(tmp_path: Path, os_name: str) -> None:
    config = _make_config(tmp_path)
    agent_dir = Path(config.agent_dir)
    config_dir = Path(config.config_dir)
    agent_dir.mkdir(parents=True)
    config_dir.mkdir(parents=True)
    operator = OperatorIdentity(login="ops", uid=1000, home=tmp_path)

    result = uninstall(
        config,
        operator=operator,
        os_name=os_name,
        run_command=lambda _command, *, check=True: None,
        remove_agent_dir=False,
        remove_config_dir=False,
    )

    assert result.removed_agent_dir is False
    assert result.removed_config_dir is False
    assert agent_dir.exists()
    assert config_dir.exists()


@pytest.mark.parametrize("os_name", ["Linux", "Darwin"])
def test_uninstall_is_idempotent_when_files_are_missing(tmp_path: Path, os_name: str) -> None:
    config = _make_config(tmp_path)
    operator = OperatorIdentity(login="ops", uid=1000, home=tmp_path)

    result = uninstall(
        config,
        operator=operator,
        os_name=os_name,
        run_command=lambda _command, *, check=True: None,
    )

    assert result.removed_service_file is False
    assert result.removed_agent_dir is False
    assert result.removed_config_dir is False


def test_uninstall_darwin_bootout_runs_when_plist_file_missing(tmp_path: Path) -> None:
    """Half-uninstalled hosts (plist deleted but service still loaded) must still bootout."""
    config = _make_config(tmp_path)
    operator = OperatorIdentity(login="ops", uid=501, home=tmp_path)
    commands: list[list[str]] = []

    result = uninstall(
        config,
        operator=operator,
        os_name="Darwin",
        run_command=lambda command, *, check=True: commands.append(list(command)),
    )

    assert commands == [["launchctl", "bootout", "gui/501/com.gridfleet.agent"]]
    assert result.removed_service_file is False


def test_uninstall_uses_operator_uid_for_bootout() -> None:
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], *, check: bool = True) -> None:
        calls.append(list(cmd))

    operator = OperatorIdentity(login="ops", uid=1001, home=Path("/home/ops"))
    config = InstallConfig(user="ops")
    uninstall(
        config,
        operator=operator,
        os_name="Darwin",
        run_command=fake_run,
        remove_agent_dir=False,
        remove_config_dir=False,
    )
    assert any("gui/1001" in arg for cmd in calls for arg in cmd)
