from pathlib import Path


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
