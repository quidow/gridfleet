from pathlib import Path
from unittest.mock import patch

import pytest

from agent_app.installer.identity import OperatorIdentity
from agent_app.installer.uv_runtime import UvRuntime, build_upgrade_command, discover_uv

OPERATOR = OperatorIdentity(login="ops", uid=1001, home=Path("/home/ops"))


def test_explicit_override_wins(tmp_path: Path) -> None:
    explicit = tmp_path / "uv"
    explicit.write_text("#!/bin/sh\n")
    explicit.chmod(0o755)
    runtime = discover_uv(operator=OPERATOR, override=explicit)
    assert runtime.bin_path == explicit
    assert runtime.source == "explicit"


def test_operator_home_local_bin(tmp_path: Path) -> None:
    home = tmp_path / "home" / "ops"
    bin_path = home / ".local" / "bin" / "uv"
    bin_path.parent.mkdir(parents=True)
    bin_path.write_text("#!/bin/sh\n")
    bin_path.chmod(0o755)
    operator = OperatorIdentity(login="ops", uid=1001, home=home)
    runtime = discover_uv(operator=operator, override=None)
    assert runtime.bin_path == bin_path
    assert runtime.source == "operator_home"


def test_path_fallback() -> None:
    with (
        patch("agent_app.installer.uv_runtime.shutil.which", return_value="/usr/local/bin/uv"),
        patch("agent_app.installer.uv_runtime.os.path.expanduser", return_value="/nonexistent"),
    ):
        operator = OperatorIdentity(login="ops", uid=1001, home=Path("/nonexistent"))
        runtime = discover_uv(operator=operator, override=None)
    assert runtime.bin_path == Path("/usr/local/bin/uv")
    assert runtime.source == "path"


def test_missing_uv_returns_none() -> None:
    with (
        patch("agent_app.installer.uv_runtime.shutil.which", return_value=None),
        patch("agent_app.installer.uv_runtime.os.path.expanduser", return_value="/nonexistent"),
    ):
        operator = OperatorIdentity(login="ops", uid=1001, home=Path("/nonexistent"))
        runtime = discover_uv(operator=operator, override=None)
    assert runtime.bin_path is None
    assert runtime.source == "missing"
    assert any(".local/bin/uv" in c for c in runtime.searched)


def test_build_upgrade_command_uses_runuser_when_available(tmp_path: Path) -> None:
    bin_path = tmp_path / "uv"
    bin_path.write_text("")
    bin_path.chmod(0o755)
    runtime = UvRuntime(bin_path=bin_path, source="operator_home", searched=())
    with patch("agent_app.installer.uv_runtime.shutil.which", return_value="/usr/sbin/runuser"):
        cmd = build_upgrade_command(
            runtime,
            operator=OPERATOR,
            package_spec="gridfleet-agent==0.4.0",
            os_name="Linux",
            current_uid=0,
        )
    assert cmd[0] == "/usr/sbin/runuser"
    assert cmd[1:4] == ["-u", "ops", "--"]
    assert "tool" in cmd and "upgrade" in cmd and "gridfleet-agent==0.4.0" in cmd
    assert any(arg.startswith("HOME=") for arg in cmd)


def test_build_upgrade_command_falls_back_to_sudo_u_on_linux(tmp_path: Path) -> None:
    bin_path = tmp_path / "uv"
    bin_path.write_text("")
    bin_path.chmod(0o755)
    runtime = UvRuntime(bin_path=bin_path, source="operator_home", searched=())
    with patch("agent_app.installer.uv_runtime.shutil.which", return_value=None):
        cmd = build_upgrade_command(
            runtime,
            operator=OPERATOR,
            package_spec="gridfleet-agent",
            os_name="Linux",
            current_uid=0,
        )
    assert cmd[:3] == ["sudo", "-u", "ops"]
    assert any(arg.startswith("HOME=") for arg in cmd)


def test_build_upgrade_command_skips_wrapper_when_already_operator(tmp_path: Path) -> None:
    bin_path = tmp_path / "uv"
    bin_path.write_text("")
    bin_path.chmod(0o755)
    runtime = UvRuntime(bin_path=bin_path, source="operator_home", searched=())
    cmd = build_upgrade_command(
        runtime,
        operator=OPERATOR,
        package_spec="gridfleet-agent",
        os_name="Linux",
        current_uid=OPERATOR.uid,
    )
    assert cmd[0] == str(bin_path)
    assert cmd[1:] == ["tool", "upgrade", "gridfleet-agent"]


def test_build_upgrade_command_macos_uses_sudo_u(tmp_path: Path) -> None:
    bin_path = tmp_path / "uv"
    bin_path.write_text("")
    bin_path.chmod(0o755)
    runtime = UvRuntime(bin_path=bin_path, source="operator_home", searched=())
    cmd = build_upgrade_command(
        runtime,
        operator=OPERATOR,
        package_spec="gridfleet-agent",
        os_name="Darwin",
        current_uid=0,
    )
    assert cmd[:3] == ["sudo", "-u", "ops"]


def test_build_upgrade_command_raises_if_missing(tmp_path: Path) -> None:
    runtime = UvRuntime(bin_path=None, source="missing", searched=("/foo", "/bar"))
    with pytest.raises(RuntimeError, match="uv not found"):
        build_upgrade_command(
            runtime,
            operator=OPERATOR,
            package_spec="gridfleet-agent",
            os_name="Linux",
            current_uid=0,
        )


def test_current_home_fallback(tmp_path: Path) -> None:
    current_home = tmp_path / "current"
    current_home.mkdir()
    bin_path = current_home / ".local" / "bin" / "uv"
    bin_path.parent.mkdir(parents=True)
    bin_path.write_text("#!/bin/sh\n")
    bin_path.chmod(0o755)
    operator = OperatorIdentity(login="ops", uid=1001, home=Path("/nonexistent"))
    with patch(
        "agent_app.installer.uv_runtime.os.path.expanduser",
        return_value=str(current_home),
    ):
        runtime = discover_uv(operator=operator, override=None)
    assert runtime.bin_path == bin_path
    assert runtime.source == "current_home"
