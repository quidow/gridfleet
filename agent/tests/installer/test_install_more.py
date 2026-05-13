from __future__ import annotations

from typing import Never

import pytest

from agent_app.installer.install import (
    _host_list_contains,
    _manager_hosts_url,
    _run_command,
    _start_service,
    poll_agent_health,
    poll_manager_registration,
    resolve_bin_path,
)
from agent_app.installer.plan import InstallConfig


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


def test_start_service_darwin_with_uid() -> None:
    import subprocess

    monkeypatch = pytest.MonkeyPatch()
    with monkeypatch.context() as m:
        commands: list[list[str]] = []

        def fake_run(command: list[str]) -> None:
            commands.append(command)

        m.setattr(subprocess, "run", fake_run)
        from pathlib import Path

        _start_service("Darwin", Path("/tmp/com.gridfleet.agent.plist"), run_command=fake_run, uid=501)
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
