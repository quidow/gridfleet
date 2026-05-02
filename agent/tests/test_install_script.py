from pathlib import Path
from stat import S_IXUSR


def test_macos_launchd_includes_optional_manager_auth_env_vars() -> None:
    script = (Path(__file__).resolve().parents[1] / "install.sh").read_text()

    assert "<key>AGENT_MANAGER_AUTH_USERNAME</key>" in script
    assert "<string>$MANAGER_AUTH_USERNAME</string>" in script
    assert "<key>AGENT_MANAGER_AUTH_PASSWORD</key>" in script
    assert "<string>$MANAGER_AUTH_PASSWORD</string>" in script
    assert "$MANAGER_AUTH_PLIST_ENTRIES" in script


def test_installer_fails_when_terminal_enabled_without_token() -> None:
    script = (Path(__file__).resolve().parents[1] / "install.sh").read_text()

    assert "ERROR: AGENT_ENABLE_WEB_TERMINAL=true requires AGENT_TERMINAL_TOKEN." in script
    assert "terminal accepts unauthenticated connections" not in script


def test_bootstrap_wrapper_installs_agent_into_dedicated_venv() -> None:
    script_path = Path(__file__).resolve().parents[2] / "scripts/install-agent.sh"
    script = script_path.read_text()

    assert script_path.stat().st_mode & S_IXUSR
    assert 'AGENT_DIR="${AGENT_DIR:-/opt/gridfleet-agent}"' in script
    assert 'python3 -m venv "$VENV_DIR"' in script
    assert '"$VENV_DIR/bin/python" -m pip install --upgrade "$PACKAGE_SPEC"' in script
    assert '"$VENV_DIR/bin/gridfleet-agent" install "${INSTALL_ARGS[@]}"' in script


def test_bootstrap_wrapper_defaults_to_start_mode_but_preserves_explicit_install_mode() -> None:
    script = (Path(__file__).resolve().parents[2] / "scripts/install-agent.sh").read_text()

    assert 'INSTALL_ARGS=(--start "$@")' in script
    assert "--dry-run|--no-start|--start" in script
    assert 'INSTALL_ARGS=("$@")' in script


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
