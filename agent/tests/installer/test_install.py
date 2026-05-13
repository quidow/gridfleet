from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from agent_app.installer.identity import OperatorIdentity
from agent_app.installer.install import (
    HealthCheckResult,
    InstallResult,
    RegistrationCheckResult,
    _service_file_path,
    install_no_start,
    install_with_start,
    poll_agent_health,
    poll_manager_registration,
    resolve_bin_path,
)
from agent_app.installer.plan import InstallConfig, ToolDiscovery


def _make_config(tmp_path: Path) -> InstallConfig:
    return InstallConfig(
        agent_dir=str(tmp_path / "opt/gridfleet-agent"),
        config_dir=str(tmp_path / "etc/gridfleet-agent"),
        manager_url="https://manager.example.com",
        port=5200,
    )


def _make_operator(
    config: InstallConfig | None = None,
    *,
    login: str = "testoperator",
    uid: int = 4242,
    home: Path | None = None,
) -> OperatorIdentity:
    """Build a deterministic OperatorIdentity for tests.

    Default login is "testoperator" (a non-current user), so the chown gate in
    install_no_start fires and exercises the chown callable. Pass
    ``login=config.user`` explicitly when you intentionally want operator.login
    to match config.user (e.g., to skip chown).
    """
    return OperatorIdentity(login=login, uid=uid, home=home or Path("/tmp"))


def test_resolve_bin_path_returns_resolved_executable(tmp_path: Path) -> None:
    executable = tmp_path / "bin/gridfleet-agent"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\n")
    result = resolve_bin_path(executable=executable)
    assert result == str(executable.resolve())


def test_resolve_bin_path_defaults_to_sys_argv(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake_argv = str(tmp_path / "bin/gridfleet-agent")
    monkeypatch.setattr("sys.argv", [fake_argv])
    result = resolve_bin_path()
    assert result == str(Path(fake_argv).resolve())


def test_resolve_bin_path_uses_shutil_which_for_bare_command(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.argv", ["gridfleet-agent"])

    def fake_which(name: str) -> str | None:
        return "/usr/local/bin/gridfleet-agent" if name == "gridfleet-agent" else None

    monkeypatch.setattr("shutil.which", fake_which)
    result = resolve_bin_path()
    assert result == "/usr/local/bin/gridfleet-agent"


def test_default_linux_service_path_is_user_systemd(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    from agent_app.installer.plan import default_install_config

    config = default_install_config("Linux")

    assert _service_file_path(config, "Linux") == (tmp_path / "cfg/systemd/user/gridfleet-agent.service")


def test_user_systemd_path_falls_back_to_dot_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))  # type: ignore[arg-type]
    from agent_app.installer.plan import default_install_config

    config = default_install_config("Linux")

    assert _service_file_path(config, "Linux") == (tmp_path / ".config/systemd/user/gridfleet-agent.service")


def test_default_macos_service_path_uses_home_launch_agents(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home/agent")

    assert _service_file_path(InstallConfig(), "Darwin") == (
        tmp_path / "home/agent/Library/LaunchAgents/com.gridfleet.agent.plist"
    )


def test_install_no_start_writes_config_runtime_dir_and_service(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))  # type: ignore[arg-type]
    config = _make_config(tmp_path)
    operator = _make_operator(config, login=config.user)
    executable = Path(config.venv_bin_dir) / "gridfleet-agent"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\n")
    result = install_no_start(
        config,
        ToolDiscovery(),
        operator=operator,
        os_name="Linux",
        executable=executable,
    )

    assert result == InstallResult(
        config_env=Path(config.config_env_path),
        service_file=tmp_path / ".config/systemd/user/gridfleet-agent.service",
        started=False,
    )
    assert (Path(config.agent_dir) / "runtimes").is_dir()
    assert Path(config.config_env_path).read_text().startswith("AGENT_MANAGER_URL=https://manager.example.com\n")
    assert stat.S_IMODE(os.stat(config.config_env_path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(result.service_file).st_mode) == 0o600
    assert "ExecStart=" + str(executable) in result.service_file.read_text()


def test_install_no_start_aligns_linux_writable_paths_to_service_user(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))  # type: ignore[arg-type]
    config = InstallConfig(
        agent_dir=str(tmp_path / "opt/gridfleet-agent"),
        config_dir=str(tmp_path / "etc/gridfleet-agent"),
        user="gridfleet",
    )
    operator = OperatorIdentity(login="gridfleet", uid=1001, home=Path("/home/gridfleet"))
    executable = Path(config.venv_bin_dir) / "gridfleet-agent"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\n")
    ownership: list[tuple[Path, str]] = []
    monkeypatch.setattr("agent_app.installer.install.os.geteuid", lambda: 0)

    install_no_start(
        config,
        ToolDiscovery(),
        operator=operator,
        os_name="Linux",
        executable=executable,
        chown=lambda path, user: ownership.append((path, user)),
    )

    service_file = tmp_path / ".config/systemd/user/gridfleet-agent.service"
    assert ownership == [
        (Path(config.agent_dir), "gridfleet"),
        (Path(config.agent_dir) / "runtimes", "gridfleet"),
        (Path(config.config_dir), "gridfleet"),
        (Path(config.config_env_path), "gridfleet"),
        (service_file, "gridfleet"),
    ]


def test_macos_service_path_uses_current_user_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))  # type: ignore[arg-type]

    assert _service_file_path(InstallConfig(), "Darwin") == (
        tmp_path / "Library/LaunchAgents/com.gridfleet.agent.plist"
    )


def test_install_no_start_uses_private_launchd_path_on_macos(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    config = _make_config(tmp_path)
    operator = _make_operator(home=tmp_path)
    executable = Path(config.venv_bin_dir) / "gridfleet-agent"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\n")

    result = install_no_start(
        config,
        ToolDiscovery(),
        operator=operator,
        os_name="Darwin",
        executable=executable,
        download=lambda _url, dest: dest.write_text("selenium"),
        chown=lambda path, user: None,
    )

    assert result.service_file == tmp_path / "Library/LaunchAgents/com.gridfleet.agent.plist"
    assert "<string>com.gridfleet.agent</string>" in result.service_file.read_text()
    assert stat.S_IMODE(os.stat(config.config_env_path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(result.service_file).st_mode) == 0o600


def test_install_with_start_runs_systemd_commands_and_health_check(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    operator = _make_operator(config, login=config.user)
    executable = Path(config.venv_bin_dir) / "gridfleet-agent"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\n")
    commands: list[list[str]] = []
    health_urls: list[str] = []

    def fake_run(command: list[str]) -> None:
        commands.append(command)

    def fake_health(
        url: str,
        timeout_sec: float = 30.0,
        interval_sec: float = 1.0,
        *,
        auth: tuple[str, str] | None = None,
    ) -> HealthCheckResult:
        del timeout_sec, interval_sec, auth
        health_urls.append(url)
        return HealthCheckResult(ok=True, message="healthy")

    result = install_with_start(
        config,
        ToolDiscovery(),
        operator=operator,
        os_name="Linux",
        executable=executable,
        download=lambda _url, dest: dest.write_text("selenium"),
        run_command=fake_run,
        health_check=fake_health,
        registration_check=lambda _config: RegistrationCheckResult(ok=True, message="registered"),
    )

    assert result.started is True
    assert result.health == HealthCheckResult(ok=True, message="healthy")
    assert commands == [
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", "gridfleet-agent"],
        ["systemctl", "--user", "start", "gridfleet-agent"],
    ]
    assert health_urls == ["http://localhost:5200/agent/health"]


@pytest.mark.parametrize("os_name", ["Linux", "Darwin"])
def test_install_with_start_checks_manager_registration_after_health_passes(tmp_path: Path, os_name: str) -> None:
    config = _make_config(tmp_path)
    operator = _make_operator(config, login=config.user, home=tmp_path)
    executable = Path(config.venv_bin_dir) / "gridfleet-agent"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\n")
    registration_checks: list[InstallConfig] = []

    result = install_with_start(
        config,
        ToolDiscovery(),
        operator=operator,
        os_name=os_name,
        executable=executable,
        download=lambda _url, dest: dest.write_text("selenium"),
        run_command=lambda _command: None,
        health_check=lambda _url, *, auth=None: HealthCheckResult(ok=True, message="healthy"),
        registration_check=lambda checked_config: (
            registration_checks.append(checked_config) or RegistrationCheckResult(ok=True, message="registered")
        ),
    )

    assert result.registration == RegistrationCheckResult(ok=True, message="registered")
    assert registration_checks == [config]


@pytest.mark.parametrize("os_name", ["Linux", "Darwin"])
def test_install_with_start_skips_manager_registration_when_health_fails(tmp_path: Path, os_name: str) -> None:
    config = _make_config(tmp_path)
    operator = _make_operator(config, login=config.user, home=tmp_path)
    executable = Path(config.venv_bin_dir) / "gridfleet-agent"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\n")

    def fail_registration(_config: InstallConfig) -> RegistrationCheckResult:
        raise AssertionError("registration check should not run when local health fails")

    result = install_with_start(
        config,
        ToolDiscovery(),
        operator=operator,
        os_name=os_name,
        executable=executable,
        download=lambda _url, dest: dest.write_text("selenium"),
        run_command=lambda _command: None,
        health_check=lambda _url, *, auth=None: HealthCheckResult(ok=False, message="health failed"),
        registration_check=fail_registration,
    )

    assert result.registration is None


def test_install_with_start_runs_launchctl_bootstrap_on_macos(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr("os.getuid", lambda: 1729)
    config = _make_config(tmp_path)
    operator = _make_operator(login=config.user, home=tmp_path)
    executable = Path(config.venv_bin_dir) / "gridfleet-agent"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\n")
    commands: list[list[str]] = []

    result = install_with_start(
        config,
        ToolDiscovery(),
        operator=operator,
        os_name="Darwin",
        executable=executable,
        download=lambda _url, dest: dest.write_text("selenium"),
        run_command=lambda command: commands.append(command),
        health_check=lambda _url, *, auth=None: HealthCheckResult(ok=False, message="health check timed out"),
    )

    assert result.started is True
    assert result.health == HealthCheckResult(ok=False, message="health check timed out")
    assert commands == [
        ["launchctl", "bootout", "gui/1729/com.gridfleet.agent"],
        ["launchctl", "bootstrap", "gui/1729", str(result.service_file)],
    ]


def test_poll_manager_registration_returns_success_when_hostname_is_listed() -> None:
    calls: list[tuple[str, object]] = []
    config = InstallConfig(
        manager_url="https://manager.example.com/",
        manager_auth_username="machine",
        manager_auth_password="secret",
    )

    def fake_get(url: str, timeout: float = 2.0, auth: tuple[str, str] | None = None) -> object:
        calls.append((url, auth))

        class Response:
            status_code = 200

            @staticmethod
            def json() -> list[dict[str, str]]:
                return [{"hostname": "agent-host", "status": "online"}]

        return Response()

    result = poll_manager_registration(config, hostname="agent-host", timeout_sec=0.1, interval_sec=0.01, get=fake_get)

    assert result == RegistrationCheckResult(ok=True, message="agent registered with manager as agent-host")
    assert calls == [("https://manager.example.com/api/hosts", ("machine", "secret"))]


def test_poll_manager_registration_times_out_when_hostname_is_missing() -> None:
    config = InstallConfig(manager_url="https://manager.example.com")

    def fake_get(_url: str, timeout: float = 2.0, auth: tuple[str, str] | None = None) -> object:
        class Response:
            status_code = 200

            @staticmethod
            def json() -> list[dict[str, str]]:
                return [{"hostname": "other-host"}]

        return Response()

    result = poll_manager_registration(config, hostname="agent-host", timeout_sec=0.01, interval_sec=0.01, get=fake_get)

    assert result.ok is False
    assert "agent-host was not listed" in result.message


def test_poll_manager_registration_explains_auth_required_on_401() -> None:
    config = InstallConfig(manager_url="https://manager.example.com")

    def fake_get(_url: str, timeout: float = 2.0, auth: tuple[str, str] | None = None) -> object:
        class Response:
            status_code = 401

        return Response()

    result = poll_manager_registration(config, hostname="agent-host", timeout_sec=0.01, interval_sec=0.01, get=fake_get)

    assert result.ok is False
    assert "--manager-auth-username" in result.message
    assert "--manager-auth-password" in result.message


@pytest.mark.parametrize("os_name", ["Linux", "Darwin"])
def test_install_with_start_raises_when_service_command_fails(tmp_path: Path, os_name: str) -> None:
    config = _make_config(tmp_path)
    operator = _make_operator(config, login=config.user, home=tmp_path)
    executable = Path(config.venv_bin_dir) / "gridfleet-agent"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\n")

    def fail_command(_command: list[str]) -> None:
        raise RuntimeError("service command failed")

    with pytest.raises(RuntimeError, match="service command failed"):
        install_with_start(
            config,
            ToolDiscovery(),
            operator=operator,
            os_name=os_name,
            executable=executable,
            download=lambda _url, dest: dest.write_text("selenium"),
            run_command=fail_command,
            health_check=lambda _url, *, auth=None: HealthCheckResult(ok=True, message="healthy"),
        )


def test_poll_agent_health_returns_success_on_http_200() -> None:
    attempts: list[str] = []

    def fake_get(url: str, timeout: float = 2.0) -> object:
        attempts.append(url)

        class Response:
            status_code = 200

            @staticmethod
            def json() -> dict[str, object]:
                return {
                    "version_guidance": {
                        "required_agent_version": "0.2.0",
                        "recommended_agent_version": "0.3.0",
                        "agent_version_status": "outdated",
                        "agent_update_available": True,
                    }
                }

        return Response()

    result = poll_agent_health("http://localhost:5200/agent/health", timeout_sec=0.1, interval_sec=0.01, get=fake_get)

    assert result.ok is True
    assert result.message == "agent health check passed"
    assert result.details["version_guidance"] == {
        "required_agent_version": "0.2.0",
        "recommended_agent_version": "0.3.0",
        "agent_version_status": "outdated",
        "agent_update_available": True,
    }
    assert attempts == ["http://localhost:5200/agent/health"]


def test_poll_agent_health_times_out_after_failed_attempts() -> None:
    def fail_get(_url: str, timeout: float = 2.0) -> object:
        raise OSError("connection refused")

    result = poll_agent_health("http://localhost:5200/agent/health", timeout_sec=0.01, interval_sec=0.01, get=fail_get)

    assert result.ok is False
    assert "connection refused" in result.message


def test_install_no_start_uses_operator_identity_for_systemd_user(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))  # type: ignore[arg-type]
    from agent_app.installer.identity import OperatorIdentity

    operator = OperatorIdentity(login="ops", uid=1001, home=tmp_path / "home" / "ops")
    config = InstallConfig(
        agent_dir=str(tmp_path / "opt" / "gridfleet-agent"),
        config_dir=str(tmp_path / "etc" / "gridfleet-agent"),
        user=operator.login,
    )
    discovery = ToolDiscovery()
    result = install_no_start(
        config,
        discovery,
        operator=operator,
        os_name="Linux",
        download=lambda url, dest: dest.write_text("jar"),
        chown=lambda path, login: None,
    )
    rendered = result.service_file.read_text()
    # User-scope units must not contain a User= directive.
    assert "User=" not in rendered
    assert tmp_path in result.service_file.parents


def test_poll_agent_health_passes_basic_auth() -> None:
    from agent_app.installer.install import HealthCheckResult, poll_agent_health

    captured: dict[str, object] = {}

    class _StubResponse:
        status_code = 200

        def json(self) -> dict[str, object]:
            return {}

    def _fake_get(
        url: str,
        *,
        timeout: float,
        auth: tuple[str, str] | None = None,
        **_kwargs: object,
    ) -> _StubResponse:
        captured["url"] = url
        captured["auth"] = auth
        return _StubResponse()

    result = poll_agent_health(
        "http://localhost:5100/agent/health",
        timeout_sec=1.0,
        interval_sec=0.1,
        get=_fake_get,
        auth=("ops", "secret"),
    )
    assert isinstance(result, HealthCheckResult)
    assert result.ok is True
    assert captured["auth"] == ("ops", "secret")


@pytest.mark.parametrize("os_name", ["Linux", "Darwin"])
def test_install_with_start_forwards_api_auth_to_health_check(tmp_path: Path, os_name: str) -> None:
    from agent_app.installer.install import HealthCheckResult, RegistrationCheckResult, install_with_start
    from agent_app.installer.plan import InstallConfig, ToolDiscovery

    captured: dict[str, object] = {}

    def _hc(url: str, *, auth: tuple[str, str] | None = None) -> HealthCheckResult:
        captured["url"] = url
        captured["auth"] = auth
        return HealthCheckResult(ok=True, message="ok", details={})

    config = InstallConfig(
        agent_dir=str(tmp_path / "agent"),
        config_dir=str(tmp_path / "etc"),
        api_auth_username="ops",
        api_auth_password="secret",
    )
    op = _make_operator(config, login=config.user, home=tmp_path)
    install_with_start(
        config,
        ToolDiscovery(),
        operator=op,
        os_name=os_name,
        download=lambda _url, _dest: None,
        run_command=lambda _cmd: None,
        health_check=_hc,
        registration_check=lambda _c: RegistrationCheckResult(ok=True, message="ok"),
    )

    assert captured["auth"] == ("ops", "secret")


def test_install_no_start_chowns_on_darwin(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    chown_calls: list[tuple[Path, str]] = []

    def fake_chown(path: Path, login: str) -> None:
        chown_calls.append((path, login))

    operator = OperatorIdentity(login="ops", uid=1001, home=tmp_path / "home" / "ops")
    config = InstallConfig(
        agent_dir=str(tmp_path / "opt" / "gridfleet-agent"),
        config_dir=str(tmp_path / "etc" / "gridfleet-agent"),
        user="ops",
    )
    discovery = ToolDiscovery()
    monkeypatch.setattr("agent_app.installer.install.os.geteuid", lambda: 0)
    install_no_start(
        config,
        discovery,
        operator=operator,
        os_name="Darwin",
        download=lambda url, dest: dest.write_text("jar"),
        chown=fake_chown,
    )
    paths = {path for path, login in chown_calls}
    assert any("config.env" in str(p) for p in paths)
    assert any("LaunchAgents/com.gridfleet.agent.plist" in str(p) for p in paths)
    assert all(login == "ops" for _, login in chown_calls)


def test_install_no_start_chowns_when_root_even_if_login_matches(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Under sudo -E getpass.getuser() may match operator.login while euid is 0.

    The chown gate must rely on euid, not login, so /opt artefacts are still chowned.
    """
    chown_calls: list[tuple[Path, str]] = []

    def fake_chown(path: Path, login: str) -> None:
        chown_calls.append((path, login))

    operator = OperatorIdentity(login="ops", uid=1001, home=tmp_path / "home" / "ops")
    config = InstallConfig(
        agent_dir=str(tmp_path / "opt" / "gridfleet-agent"),
        config_dir=str(tmp_path / "etc" / "gridfleet-agent"),
        user="ops",
    )
    monkeypatch.setattr("agent_app.installer.install.os.geteuid", lambda: 0)
    monkeypatch.setattr("agent_app.installer.install.getpass.getuser", lambda: "ops")
    discovery = ToolDiscovery()
    install_no_start(
        config,
        discovery,
        operator=operator,
        os_name="Linux",
        download=lambda url, dest: dest.write_text("jar"),
        chown=fake_chown,
    )
    assert chown_calls, "chown must fire when running as root even if login matches"


def test_install_no_start_skips_chown_when_already_operator(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    chown_calls: list[tuple[Path, str]] = []

    def fake_chown(path: Path, login: str) -> None:
        chown_calls.append((path, login))

    operator = OperatorIdentity(login="ops", uid=1001, home=tmp_path / "home" / "ops")
    config = InstallConfig(
        agent_dir=str(tmp_path / "opt" / "gridfleet-agent"),
        config_dir=str(tmp_path / "etc" / "gridfleet-agent"),
        user="ops",
    )
    monkeypatch.setattr("agent_app.installer.install.os.geteuid", lambda: operator.uid)
    discovery = ToolDiscovery()
    install_no_start(
        config,
        discovery,
        operator=operator,
        os_name="Linux",
        download=lambda url, dest: dest.write_text("jar"),
        chown=fake_chown,
    )
    assert chown_calls == []


@pytest.mark.parametrize("os_name", ["Linux", "Darwin"])
def test_install_with_start_omits_auth_when_unset(tmp_path: Path, os_name: str) -> None:
    from agent_app.installer.install import HealthCheckResult, RegistrationCheckResult, install_with_start
    from agent_app.installer.plan import InstallConfig, ToolDiscovery

    captured: dict[str, object] = {}

    def _hc(url: str, *, auth: tuple[str, str] | None = None) -> HealthCheckResult:
        captured["url"] = url
        captured["auth"] = auth
        return HealthCheckResult(ok=True, message="ok", details={})

    config = InstallConfig(
        agent_dir=str(tmp_path / "agent"),
        config_dir=str(tmp_path / "etc"),
    )
    op = _make_operator(config, login=config.user, home=tmp_path)
    install_with_start(
        config,
        ToolDiscovery(),
        operator=op,
        os_name=os_name,
        download=lambda _url, _dest: None,
        run_command=lambda _cmd: None,
        health_check=_hc,
        registration_check=lambda _c: RegistrationCheckResult(ok=True, message="ok"),
    )

    assert captured["auth"] is None


def test_start_service_linux_invokes_systemctl_user(tmp_path: Path) -> None:
    from agent_app.installer.install import _start_service

    service_file = tmp_path / "systemd/user/gridfleet-agent.service"
    service_file.parent.mkdir(parents=True)
    service_file.write_text("")

    calls: list[list[str]] = []

    def fake_run(cmd: list[str]) -> None:
        calls.append(cmd)

    _start_service("Linux", service_file, run_command=fake_run)

    assert calls == [
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", "gridfleet-agent"],
        ["systemctl", "--user", "start", "gridfleet-agent"],
    ]


def test_start_service_darwin_uses_current_uid(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from agent_app.installer.install import _start_service

    service_file = tmp_path / "com.gridfleet.agent.plist"
    service_file.write_text("")

    calls: list[list[str]] = []

    def fake_run(cmd: list[str]) -> None:
        calls.append(cmd)

    monkeypatch.setattr("os.getuid", lambda: 1729)

    _start_service("Darwin", service_file, run_command=fake_run)

    assert calls == [
        ["launchctl", "bootout", "gui/1729/com.gridfleet.agent"],
        ["launchctl", "bootstrap", "gui/1729", str(service_file)],
    ]
