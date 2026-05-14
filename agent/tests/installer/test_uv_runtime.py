from pathlib import Path
from unittest.mock import patch

import pytest

from agent_app.installer.identity import OperatorIdentity
from agent_app.installer.plan import InstallConfig
from agent_app.installer.uv_runtime import UvRuntime, build_upgrade_command, discover_uv

OPERATOR = OperatorIdentity(login="ops", uid=1001, home=Path("/home/ops"))


def test_explicit_override_wins(tmp_path: Path) -> None:
    explicit = tmp_path / "uv"
    explicit.write_text("#!/bin/sh\n")
    explicit.chmod(0o755)
    runtime = discover_uv(operator=OPERATOR, override=explicit)
    assert runtime.bin_path == explicit
    assert runtime.source == "explicit"


def test_explicit_override_missing_raises(tmp_path: Path) -> None:
    bogus = tmp_path / "missing-uv"
    with pytest.raises(RuntimeError, match="not an executable file"):
        discover_uv(operator=OPERATOR, override=bogus)


def test_explicit_override_non_executable_raises(tmp_path: Path) -> None:
    nonexec = tmp_path / "not-executable"
    nonexec.write_text("#!/bin/sh\n")
    nonexec.chmod(0o644)
    with pytest.raises(RuntimeError, match="not an executable file"):
        discover_uv(operator=OPERATOR, override=nonexec)


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


def test_build_upgrade_command_uses_dedicated_venv_python(tmp_path: Path) -> None:
    bin_path = tmp_path / "uv"
    bin_path.write_text("")
    bin_path.chmod(0o755)
    runtime = UvRuntime(bin_path=bin_path, source="operator_home", searched=())
    config = InstallConfig(agent_dir=str(tmp_path / "agent"), config_dir=str(tmp_path / "config"))
    cmd = build_upgrade_command(
        runtime,
        operator=OPERATOR,
        package_spec="gridfleet-agent==0.4.0",
        config=config,
    )
    assert cmd[0] == str(bin_path)
    assert cmd[1:] == [
        "pip",
        "install",
        "--python",
        str(tmp_path / "agent/venv/bin/python"),
        "--upgrade",
        "gridfleet-agent==0.4.0",
    ]


def test_build_upgrade_command_raises_if_missing(tmp_path: Path) -> None:
    runtime = UvRuntime(bin_path=None, source="missing", searched=("/foo", "/bar"))
    config = InstallConfig(agent_dir=str(tmp_path / "agent"), config_dir=str(tmp_path / "config"))
    with pytest.raises(RuntimeError, match="uv not found"):
        build_upgrade_command(
            runtime,
            operator=OPERATOR,
            package_spec="gridfleet-agent",
            config=config,
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
