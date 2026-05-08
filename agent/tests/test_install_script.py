import os
import subprocess
from pathlib import Path
from stat import S_IXUSR

import pytest


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content)
    path.chmod(0o755)


def test_bootstrap_wrapper_uses_uv_tool_install() -> None:
    script = (Path(__file__).resolve().parents[2] / "scripts/install-agent.sh").read_text()
    assert script.startswith("#!/bin/sh")
    assert "uv tool install" in script
    assert "gridfleet-agent" in script
    assert "--python 3.12" in script


def test_bootstrap_wrapper_installs_uv_if_missing() -> None:
    script = (Path(__file__).resolve().parents[2] / "scripts/install-agent.sh").read_text()
    assert "astral.sh/uv/install.sh" in script
    assert "command -v uv" in script


def test_bootstrap_wrapper_calls_gridfleet_agent_install() -> None:
    script = (Path(__file__).resolve().parents[2] / "scripts/install-agent.sh").read_text()
    assert "gridfleet-agent install" in script


def test_bootstrap_wrapper_is_executable() -> None:
    script_path = Path(__file__).resolve().parents[2] / "scripts/install-agent.sh"
    assert script_path.stat().st_mode & S_IXUSR


def test_bootstrap_wrapper_runs_under_sh(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "commands.log"
    env = os.environ | {"PATH": f"{bin_dir}:{os.environ['PATH']}", "COMMAND_LOG": str(log)}
    script_path = Path(__file__).resolve().parents[2] / "scripts/install-agent.sh"

    _write_executable(
        bin_dir / "uv",
        '#!/usr/bin/env bash\nprintf \'uv %s\\n\' "$*" >> "$COMMAND_LOG"\n',
    )
    _write_executable(
        bin_dir / "gridfleet-agent",
        '#!/usr/bin/env bash\nprintf \'gridfleet-agent %s\\n\' "$*" >> "$COMMAND_LOG"\n',
    )
    _write_executable(bin_dir / "uname", "#!/usr/bin/env bash\necho Linux\n")
    _write_executable(bin_dir / "id", '#!/usr/bin/env bash\n[ "$1" = "-u" ] && echo 0\n')

    result = subprocess.run(
        ["sh", str(script_path), "--dry-run", "--manager-url", "https://manager.example.com"],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode == 0, result.stderr
    commands = log.read_text()
    assert "uv tool install --upgrade --python 3.12 gridfleet-agent" in commands
    assert "gridfleet-agent install --dry-run --manager-url https://manager.example.com" in commands


def test_bootstrap_wrapper_supports_version_pinning() -> None:
    script = (Path(__file__).resolve().parents[2] / "scripts/install-agent.sh").read_text()
    assert "VERSION" in script
    assert "gridfleet-agent==" in script


def test_bootstrap_wrapper_defaults_to_start_mode() -> None:
    script = (Path(__file__).resolve().parents[2] / "scripts/install-agent.sh").read_text()
    assert "--start" in script
    assert "--dry-run" in script
    assert "--no-start" in script


@pytest.mark.parametrize("mode_args", [("--dry-run",), ("--no-start",), ("--start", "--dry-run")])
def test_bootstrap_wrapper_only_stops_service_for_start_mode(tmp_path: Path, mode_args: tuple[str, ...]) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "commands.log"
    env = os.environ | {"PATH": f"{bin_dir}:{os.environ['PATH']}", "COMMAND_LOG": str(log)}
    script_path = Path(__file__).resolve().parents[2] / "scripts/install-agent.sh"

    _write_executable(
        bin_dir / "uv",
        '#!/usr/bin/env bash\nprintf \'uv %s\\n\' "$*" >> "$COMMAND_LOG"\n',
    )
    _write_executable(
        bin_dir / "gridfleet-agent",
        '#!/usr/bin/env bash\nprintf \'gridfleet-agent %s\\n\' "$*" >> "$COMMAND_LOG"\n',
    )
    _write_executable(
        bin_dir / "systemctl",
        '#!/usr/bin/env bash\nprintf \'systemctl %s\\n\' "$*" >> "$COMMAND_LOG"\n',
    )
    _write_executable(bin_dir / "uname", "#!/usr/bin/env bash\necho Linux\n")
    _write_executable(bin_dir / "id", '#!/usr/bin/env bash\n[ "$1" = "-u" ] && echo 1000\n')
    _write_executable(
        bin_dir / "sudo",
        '#!/usr/bin/env bash\nprintf \'sudo %s\\n\' "$*" >> "$COMMAND_LOG"\n"$@"\n',
    )

    result = subprocess.run(
        [str(script_path), *mode_args, "--manager-url", "https://manager.example.com"],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode == 0, result.stderr
    commands = log.read_text()
    assert "systemctl stop gridfleet-agent" not in commands
    assert f"gridfleet-agent install {' '.join(mode_args)} --manager-url https://manager.example.com" in commands


def test_bootstrap_wrapper_does_not_use_python_venv() -> None:
    script = (Path(__file__).resolve().parents[2] / "scripts/install-agent.sh").read_text()
    assert "python3 -m venv" not in script
    assert "pip install" not in script


def _stage_fakes(tmp_path: Path, captured: Path) -> dict[str, str]:
    """Create fake binaries (sudo, gridfleet-agent, id, uv, curl, launchctl, systemctl) that log to `captured`."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()

    def _make(name: str, body: str) -> None:
        path = bin_dir / name
        path.write_text("#!/bin/sh\n" + body)
        path.chmod(0o755)

    _make("sudo", f'echo "sudo $@" >> "{captured}"\nexec "$@"\n')
    _make("gridfleet-agent", f'echo "gridfleet-agent $@" >> "{captured}"\n')
    _make("id", "echo 1001\n")
    _make("uv", f'echo "uv $@" >> "{captured}"\n')
    _make("curl", "")
    _make("launchctl", "")
    _make("systemctl", "")

    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
    env["USER"] = "ops"
    env["HOME"] = str(tmp_path / "home" / "ops")
    Path(env["HOME"]).mkdir(parents=True, exist_ok=True)
    return env


def test_install_script_appends_user_when_missing(tmp_path: Path) -> None:
    captured = tmp_path / "calls.log"
    captured.touch()
    env = _stage_fakes(tmp_path, captured)

    repo_script = Path(__file__).resolve().parents[2] / "scripts" / "install-agent.sh"
    subprocess.run(
        ["sh", str(repo_script), "--manager-url", "http://m"],
        env=env,
        check=True,
    )

    log = captured.read_text()
    assert "gridfleet-agent install --start --manager-url http://m --user ops" in log


def test_install_script_does_not_duplicate_user(tmp_path: Path) -> None:
    captured = tmp_path / "calls.log"
    captured.touch()
    env = _stage_fakes(tmp_path, captured)

    repo_script = Path(__file__).resolve().parents[2] / "scripts" / "install-agent.sh"
    subprocess.run(
        ["sh", str(repo_script), "--manager-url", "http://m", "--user", "alice"],
        env=env,
        check=True,
    )

    log = captured.read_text()
    invocation = next(line for line in log.splitlines() if "gridfleet-agent install" in line)
    assert invocation.count("--user") == 1
    assert "--user alice" in invocation


def test_operator_docs_point_to_bootstrap_wrapper_not_legacy_install_script() -> None:
    root = Path(__file__).resolve().parents[2]
    docs = {
        "README.md": (root / "README.md").read_text(),
        "docs/guides/deployment.md": (root / "docs/guides/deployment.md").read_text(),
        "docs/reference/environment.md": (root / "docs/reference/environment.md").read_text(),
    }
    for text in docs.values():
        assert "scripts/install-agent.sh" in text
        assert "bash agent/install.sh" not in text
        assert "./agent/install.sh" not in text
        assert "./agent/update.sh" not in text
