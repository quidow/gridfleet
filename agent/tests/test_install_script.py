import os
import subprocess
from pathlib import Path
from stat import S_IXUSR

import pytest


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content)
    path.chmod(0o755)


def test_bootstrap_wrapper_uses_uv_pip_install() -> None:
    script = (Path(__file__).resolve().parents[2] / "scripts/install-agent.sh").read_text()
    assert script.startswith("#!/bin/sh")
    assert "uv pip install" in script
    assert "--python" in script
    assert "--upgrade" in script
    assert "gridfleet-agent" in script


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


def test_bootstrap_wrapper_refuses_root() -> None:
    script = (Path(__file__).resolve().parents[2] / "scripts/install-agent.sh").read_text()
    assert "id -u" in script
    assert "do not run this installer as root" in script


def test_bootstrap_wrapper_runs_under_sh(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "commands.log"
    env = os.environ | {"PATH": f"{bin_dir}:{os.environ['PATH']}", "COMMAND_LOG": str(log), "HOME": str(tmp_path)}
    script_path = Path(__file__).resolve().parents[2] / "scripts/install-agent.sh"

    _write_executable(
        bin_dir / "uv",
        '#!/usr/bin/env bash\nprintf \'uv %s\\n\' "$*" >> "$COMMAND_LOG"\n',
    )
    venv_agent = tmp_path / ".local/share/gridfleet-agent/venv/bin/gridfleet-agent"
    venv_agent.parent.mkdir(parents=True)
    _write_executable(
        venv_agent,
        '#!/usr/bin/env bash\nprintf \'gridfleet-agent %s\\n\' "$*" >> "$COMMAND_LOG"\n',
    )
    _write_executable(bin_dir / "uname", "#!/usr/bin/env bash\necho Linux\n")
    _write_executable(bin_dir / "id", '#!/usr/bin/env bash\n[ "$1" = "-u" ] && echo 1000\n')

    result = subprocess.run(
        ["sh", str(script_path), "--dry-run", "--manager-url", "https://manager.example.com"],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode == 0, result.stderr
    commands = log.read_text()
    assert "uv venv --python 3.12" in commands
    assert "uv pip install --python" in commands
    assert "--upgrade" in commands
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
    env = os.environ | {"PATH": f"{bin_dir}:{os.environ['PATH']}", "COMMAND_LOG": str(log), "HOME": str(tmp_path)}
    script_path = Path(__file__).resolve().parents[2] / "scripts/install-agent.sh"

    _write_executable(
        bin_dir / "uv",
        '#!/usr/bin/env bash\nprintf \'uv %s\\n\' "$*" >> "$COMMAND_LOG"\n',
    )
    venv_agent = tmp_path / ".local/share/gridfleet-agent/venv/bin/gridfleet-agent"
    venv_agent.parent.mkdir(parents=True)
    _write_executable(
        venv_agent,
        '#!/usr/bin/env bash\nprintf \'gridfleet-agent %s\\n\' "$*" >> "$COMMAND_LOG"\n',
    )
    _write_executable(
        bin_dir / "systemctl",
        '#!/usr/bin/env bash\nprintf \'systemctl %s\\n\' "$*" >> "$COMMAND_LOG"\n',
    )
    _write_executable(bin_dir / "uname", "#!/usr/bin/env bash\necho Linux\n")
    _write_executable(bin_dir / "id", '#!/usr/bin/env bash\n[ "$1" = "-u" ] && echo 1000\n')

    result = subprocess.run(
        [str(script_path), *mode_args, "--manager-url", "https://manager.example.com"],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode == 0, result.stderr
    commands = log.read_text()
    assert "systemctl --user stop gridfleet-agent" not in commands
    assert f"gridfleet-agent install {' '.join(mode_args)} --manager-url https://manager.example.com" in commands


def test_bootstrap_wrapper_uses_uv_venv() -> None:
    script = (Path(__file__).resolve().parents[2] / "scripts/install-agent.sh").read_text()
    assert "python3 -m venv" not in script
    assert "uv venv" in script


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
