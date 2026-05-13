from __future__ import annotations

from pathlib import Path
from typing import Never

import pytest

from agent_app.installer.identity import OperatorIdentity
from agent_app.installer.install import (
    _host_list_contains,
    _manager_hosts_url,
    _run_command,
    _start_service,
    poll_agent_health,
    poll_manager_registration,
    resolve_bin_path,
)
from agent_app.installer.plan import InstallConfig, ToolDiscovery


def test_resolve_bin_path_with_none_uses_sys_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.argv", ["/usr/local/bin/gridfleet-agent"])
    assert resolve_bin_path(executable=None) == "/usr/local/bin/gridfleet-agent"


def test_resolve_bin_path_relative_falls_back_to_shutil_which(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.argv", ["gridfleet-agent"])
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}" if name == "gridfleet-agent" else None)
    assert resolve_bin_path() == "/usr/bin/gridfleet-agent"


def test_run_command_timeout_expired() -> None:
    import subprocess

    monkeypatch = pytest.MonkeyPatch()
    with monkeypatch.context() as m:

        class FakeExpired(subprocess.TimeoutExpired):
            pass

        def fake_run(*args: object, **kwargs: object) -> Never:
            raise FakeExpired(args[0], kwargs.get("timeout"))

        m.setattr(subprocess, "run", fake_run)
        with pytest.raises(RuntimeError, match="timed out after 2s"):
            _run_command(["echo", "hello"], timeout=2)


def test_run_command_nonzero_exit_with_stdout() -> None:
    import subprocess

    monkeypatch = pytest.MonkeyPatch()
    with monkeypatch.context() as m:

        class FakeResult:
            returncode = 1
            stderr = ""
            stdout = "some output"

        m.setattr(subprocess, "run", lambda *a, **k: FakeResult())
        with pytest.raises(RuntimeError, match="some output"):
            _run_command(["false"], timeout=10)


def test_start_service_darwin_with_uid(monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess

    with monkeypatch.context() as m:
        commands: list[list[str]] = []

        def fake_run(command: list[str]) -> None:
            commands.append(command)

        m.setattr(subprocess, "run", fake_run)
        m.setattr("os.getuid", lambda: 501)
        from pathlib import Path

        _start_service("Darwin", Path("/tmp/com.gridfleet.agent.plist"), run_command=fake_run)
        assert any("launchctl" in str(cmd) and "bootstrap" in str(cmd) for cmd in commands)


def test_start_service_unsupported_os() -> None:
    from pathlib import Path

    with pytest.raises(RuntimeError, match="Unsupported OS"):
        _start_service("Windows", Path("/tmp/x.plist"), run_command=lambda _cmd: None)


def test_poll_agent_health_bad_json_detail() -> None:
    def fake_get(url: str, timeout: float = 2.0) -> object:
        class Response:
            status_code = 200

            @staticmethod
            def json() -> list[object]:
                return []  # not a dict

        return Response()

    result = poll_agent_health("http://localhost:5100/agent/health", timeout_sec=0.01, interval_sec=0.01, get=fake_get)
    assert result.ok is True
    assert result.details == {}


def test_poll_agent_health_auth_rejected_on_401() -> None:
    def fake_get(url: str, timeout: float = 2.0) -> object:
        class Response:
            status_code = 401

        return Response()

    result = poll_agent_health("http://x/agent/health", timeout_sec=0.01, interval_sec=0.01, get=fake_get)
    assert result.ok is False
    assert "rejected credentials" in result.message


def test_poll_manager_registration_401_advises_credentials() -> None:
    config = InstallConfig(manager_url="https://manager.example.com")

    def fake_get(_url: str, timeout: float = 2.0, auth: tuple[str, str] | None = None) -> object:
        class Response:
            status_code = 401

        return Response()

    result = poll_manager_registration(config, hostname="host", timeout_sec=0.01, interval_sec=0.01, get=fake_get)
    assert result.ok is False
    assert "machine auth" in result.message


def test_poll_manager_registration_unexpected_status() -> None:
    config = InstallConfig(manager_url="https://manager.example.com")

    def fake_get(_url: str, timeout: float = 2.0, auth: tuple[str, str] | None = None) -> object:
        class Response:
            status_code = 503

        return Response()

    result = poll_manager_registration(config, hostname="host", timeout_sec=0.01, interval_sec=0.01, get=fake_get)
    assert result.ok is False
    assert "unexpected status 503" in result.message


def test_host_list_contains_non_list() -> None:
    assert _host_list_contains("not-a-list", "host") is False
    assert _host_list_contains(None, "host") is False


def test_host_list_contains_empty_hostname() -> None:
    assert _host_list_contains([{"hostname": ""}], "host") is False
    assert _host_list_contains([{"hostname": None}], "host") is False


def test_manager_hosts_url_trailing_slash() -> None:
    config = InstallConfig(manager_url="https://manager.example.com/")
    assert _manager_hosts_url(config) == "https://manager.example.com/api/hosts"


def test_validate_dedicated_venv_accepts_xdg_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    from agent_app.installer.install import validate_dedicated_venv
    from agent_app.installer.plan import default_install_config

    config = default_install_config("Linux")
    venv_bin = Path(config.agent_dir) / "venv/bin/gridfleet-agent"
    venv_bin.parent.mkdir(parents=True)
    venv_bin.write_text("#!/bin/sh\n")
    venv_bin.chmod(0o755)

    validate_dedicated_venv(config, executable=venv_bin)  # should not raise


def test_validate_dedicated_venv_rejects_other_path(tmp_path: Path) -> None:
    from agent_app.installer.install import validate_dedicated_venv
    from agent_app.installer.plan import InstallConfig

    config = InstallConfig(
        agent_dir=str(tmp_path / "agent"),
        config_dir=str(tmp_path / "config"),
        bin_path=str(tmp_path / "agent/venv/bin/gridfleet-agent"),
    )
    other = tmp_path / "elsewhere/gridfleet-agent"
    other.parent.mkdir(parents=True)
    other.write_text("#!/bin/sh\n")

    with pytest.raises(RuntimeError, match="must run from"):
        validate_dedicated_venv(config, executable=other)


LEGACY_MARKERS = [
    Path("/opt/gridfleet-agent"),
    Path("/etc/gridfleet-agent/config.env"),
    Path("/etc/systemd/system/gridfleet-agent.service"),
]


def test_detect_legacy_install_returns_first_existing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from agent_app.installer.install import detect_legacy_install

    fake_paths = {marker: (tmp_path / marker.name) for marker in LEGACY_MARKERS}
    # Only the second marker exists.
    second = fake_paths[LEGACY_MARKERS[1]]
    second.parent.mkdir(parents=True, exist_ok=True)
    second.write_text("")
    monkeypatch.setattr(
        "agent_app.installer.install._LEGACY_PATHS",
        tuple(fake_paths.values()),
    )

    found = detect_legacy_install()

    assert found == second


def test_detect_legacy_install_returns_none_when_clean(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from agent_app.installer.install import detect_legacy_install

    monkeypatch.setattr(
        "agent_app.installer.install._LEGACY_PATHS",
        (tmp_path / "nope", tmp_path / "also-nope"),
    )

    assert detect_legacy_install() is None


def test_install_no_start_aborts_on_legacy_paths(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from agent_app.installer.install import LegacyInstallDetectedError, install_no_start
    from agent_app.installer.plan import default_install_config

    fake_legacy = tmp_path / "fake-opt-gridfleet-agent"
    fake_legacy.mkdir()
    monkeypatch.setattr("agent_app.installer.install._LEGACY_PATHS", (fake_legacy,))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))  # type: ignore[arg-type]

    config = default_install_config("Linux")
    discovery = ToolDiscovery(node_bin_dir=None, android_home=None, warnings=[])
    operator = OperatorIdentity(login="anyone", uid=4242, home=tmp_path / "home")

    with pytest.raises(LegacyInstallDetectedError, match="Legacy root-scope install"):
        install_no_start(config, discovery, operator=operator, os_name="Linux")


def test_check_linger_returns_warning_when_off(monkeypatch: pytest.MonkeyPatch) -> None:
    import getpass

    from agent_app.installer.install import check_linger

    captured: list[list[str]] = []

    def fake_run(cmd: list[str]) -> str:
        captured.append(cmd)
        return "Linger=no"

    warning = check_linger(run_command=fake_run)

    user = getpass.getuser()
    assert captured == [["loginctl", "show-user", user, "--property=Linger"]]
    assert warning is not None
    assert "loginctl enable-linger" in warning
    assert user in warning


def test_check_linger_returns_none_when_on() -> None:
    from agent_app.installer.install import check_linger

    assert check_linger(run_command=lambda _cmd: "Linger=yes") is None


def test_check_linger_returns_warning_when_unknown() -> None:
    from agent_app.installer.install import check_linger

    warning = check_linger(run_command=lambda _cmd: "")
    assert warning is not None
