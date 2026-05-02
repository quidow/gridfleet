from pathlib import Path
from stat import S_IXUSR


def test_bootstrap_wrapper_uses_uv_tool_install() -> None:
    script = (Path(__file__).resolve().parents[2] / "scripts/install-agent.sh").read_text()
    assert script.startswith("#!/usr/bin/env bash")
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


def test_bootstrap_wrapper_supports_version_pinning() -> None:
    script = (Path(__file__).resolve().parents[2] / "scripts/install-agent.sh").read_text()
    assert "VERSION" in script
    assert "gridfleet-agent==" in script


def test_bootstrap_wrapper_defaults_to_start_mode() -> None:
    script = (Path(__file__).resolve().parents[2] / "scripts/install-agent.sh").read_text()
    assert "--start" in script
    assert "--dry-run|--no-start|--start" in script


def test_bootstrap_wrapper_does_not_use_python_venv() -> None:
    script = (Path(__file__).resolve().parents[2] / "scripts/install-agent.sh").read_text()
    assert "python3 -m venv" not in script
    assert "pip install" not in script


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
