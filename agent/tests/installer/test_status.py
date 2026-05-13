from __future__ import annotations

from pathlib import Path

import pytest

from agent_app.installer.identity import OperatorIdentity
from agent_app.installer.install import HealthCheckResult
from agent_app.installer.plan import InstallConfig
from agent_app.installer.status import _run_status_command, collect_status, format_status, parse_config_env
from agent_app.installer.uv_runtime import UvRuntime

_DEFAULT_OPERATOR = OperatorIdentity(login="testop", uid=9999, home=Path("/home/testop"))
_DEFAULT_UV = UvRuntime(bin_path=None, source="missing", searched=())


def _stub_health(url: str, *, auth: tuple[str, str] | None = None) -> HealthCheckResult:
    return HealthCheckResult(ok=True, message="stubbed")


def _make_config(tmp_path: Path) -> InstallConfig:
    return InstallConfig(
        agent_dir=str(tmp_path / "opt/gridfleet-agent"),
        config_dir=str(tmp_path / "etc/gridfleet-agent"),
    )


def test_parse_config_env_skips_comments_and_blank_lines(tmp_path: Path) -> None:
    path = tmp_path / "config.env"
    path.write_text("\n# comment\nAGENT_MANAGER_URL=https://manager.example.com\nAGENT_AGENT_PORT=5200\nMALFORMED\n")

    assert parse_config_env(path) == {
        "AGENT_MANAGER_URL": "https://manager.example.com",
        "AGENT_AGENT_PORT": "5200",
    }


def test_collect_status_reads_files_service_state_and_health(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))  # type: ignore[arg-type]
    config = _make_config(tmp_path)
    Path(config.config_dir).mkdir(parents=True)
    Path(config.config_env_path).write_text(
        "AGENT_MANAGER_URL=https://manager.example.com\nAGENT_AGENT_PORT=5200\nAGENT_MANAGER_AUTH_PASSWORD=secret\n"
    )
    service_file = tmp_path / ".config/systemd/user/gridfleet-agent.service"
    service_file.parent.mkdir(parents=True)
    service_file.write_text("[Service]\n")
    commands: list[list[str]] = []

    def fake_command(command: list[str]) -> str:
        commands.append(command)
        if command == ["systemctl", "--user", "is-active", "gridfleet-agent"]:
            return "active\n"
        if command == ["systemctl", "--user", "is-enabled", "gridfleet-agent"]:
            return "enabled\n"
        raise AssertionError(command)

    status = collect_status(
        config,
        operator=_DEFAULT_OPERATOR,
        uv_runtime=_DEFAULT_UV,
        os_name="Linux",
        run_command=fake_command,
        health_check=lambda url: HealthCheckResult(ok=True, message=f"healthy at {url}"),
    )

    assert status.config_exists is True
    assert status.service_file == service_file
    assert status.service_exists is True
    assert status.service_active == "active"
    assert status.service_enabled == "enabled"
    assert status.health == HealthCheckResult(ok=True, message="healthy at http://localhost:5200/agent/health")
    assert status.env["AGENT_MANAGER_URL"] == "https://manager.example.com"
    assert commands == [
        ["systemctl", "--user", "is-active", "gridfleet-agent"],
        ["systemctl", "--user", "is-enabled", "gridfleet-agent"],
    ]


@pytest.mark.parametrize("os_name", ["Linux", "Darwin"])
def test_collect_status_handles_missing_config_without_health_check(tmp_path: Path, os_name: str) -> None:
    config = _make_config(tmp_path)

    status = collect_status(
        config,
        operator=_DEFAULT_OPERATOR,
        uv_runtime=_DEFAULT_UV,
        os_name=os_name,
        run_command=lambda _command: "inactive\n",
        health_check=lambda _url: HealthCheckResult(ok=True, message="should not run"),
    )

    assert status.config_exists is False
    assert status.health == HealthCheckResult(ok=False, message="config.env missing; health check skipped")


def test_collect_status_reports_unreadable_config_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    config_env = Path(config.config_env_path)
    config_env.parent.mkdir(parents=True)
    config_env.write_text("AGENT_AGENT_PORT=5200\n")
    original_read_text = Path.read_text

    def fake_read_text(path: Path, *args: object, **kwargs: object) -> str:
        if path == config_env:
            raise PermissionError("permission denied")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fake_read_text)

    status = collect_status(
        config,
        operator=_DEFAULT_OPERATOR,
        uv_runtime=_DEFAULT_UV,
        os_name="Linux",
        run_command=lambda _command: "inactive\n",
        health_check=lambda _url: HealthCheckResult(ok=True, message="should not run"),
    )

    assert status.config_exists is True
    assert status.config_error is not None
    assert "permission denied" in status.config_error
    assert status.health == HealthCheckResult(ok=False, message="config.env unreadable; health check skipped")
    assert "Config read: failed" in format_status(status)


def test_run_status_command_reports_missing_service_manager(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(*_args: object, **_kwargs: object) -> object:
        raise FileNotFoundError("systemctl")

    monkeypatch.setattr("agent_app.installer.status.subprocess.run", fake_run)

    assert _run_status_command(["systemctl", "is-active", "gridfleet-agent"]) == "systemctl unavailable: systemctl"


def test_collect_status_uses_launchctl_on_macos(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    config = _make_config(tmp_path)
    Path(config.config_dir).mkdir(parents=True)
    Path(config.config_env_path).write_text("AGENT_AGENT_PORT=5200\n")
    commands: list[list[str]] = []

    collect_status(
        config,
        operator=_DEFAULT_OPERATOR,
        uv_runtime=_DEFAULT_UV,
        os_name="Darwin",
        run_command=lambda command: commands.append(command) or "123\t0\tcom.gridfleet.agent\n",
        health_check=lambda _url: HealthCheckResult(ok=False, message="down"),
    )

    assert commands == [["launchctl", "print", f"gui/{_DEFAULT_OPERATOR.uid}/com.gridfleet.agent"]]


def test_format_status_redacts_secrets() -> None:
    status = collect_status(
        InstallConfig(),
        operator=_DEFAULT_OPERATOR,
        uv_runtime=_DEFAULT_UV,
        os_name="Linux",
        env={
            "AGENT_MANAGER_URL": "https://manager.example.com",
            "AGENT_MANAGER_AUTH_PASSWORD": "secret",
            "AGENT_TERMINAL_TOKEN": "terminal-token",
            "AGENT_AGENT_PORT": "5200",
        },
        run_command=lambda _command: "active\n",
        health_check=lambda _url: HealthCheckResult(
            ok=True,
            message="healthy",
            details={
                "version_guidance": {
                    "required_agent_version": "0.2.0",
                    "recommended_agent_version": "0.3.0",
                    "agent_version_status": "outdated",
                    "agent_update_available": True,
                }
            },
        ),
    )

    output = format_status(status)

    assert "GridFleet Agent status" in output
    assert "  AGENT_MANAGER_URL=https://manager.example.com" in output.splitlines()
    assert "secret" not in output
    assert "terminal-token" not in output
    assert "AGENT_MANAGER_AUTH_PASSWORD=<redacted>" in output
    assert "AGENT_TERMINAL_TOKEN=<redacted>" in output
    assert "Local health: ok - healthy" in output
    assert "Agent version guidance: installed version is outdated" in output
    assert "Recommended agent version: 0.3.0" in output
    assert "Minimum supported agent version: 0.2.0" in output


def test_format_status_reports_missing_config_as_not_read(tmp_path: Path) -> None:
    status = collect_status(
        _make_config(tmp_path),
        operator=_DEFAULT_OPERATOR,
        uv_runtime=_DEFAULT_UV,
        os_name="Linux",
        run_command=lambda _command: "inactive\n",
        health_check=lambda _url: HealthCheckResult(ok=True, message="should not run"),
    )

    output = format_status(status)

    assert "Config file:" in output
    assert "(missing)" in output
    assert "Config read: skipped - config.env missing" in output


def test_collect_status_passes_api_auth_to_health_check() -> None:
    from agent_app.installer.install import HealthCheckResult
    from agent_app.installer.plan import InstallConfig
    from agent_app.installer.status import collect_status

    captured: dict[str, object] = {}

    def _fake_health(url: str, *, auth: tuple[str, str] | None = None) -> HealthCheckResult:
        captured["url"] = url
        captured["auth"] = auth
        return HealthCheckResult(ok=True, message="ok", details={})

    config = InstallConfig(api_auth_username="ops", api_auth_password="secret")
    collect_status(
        config,
        operator=_DEFAULT_OPERATOR,
        uv_runtime=_DEFAULT_UV,
        os_name="Linux",
        env={
            "AGENT_AGENT_PORT": "5100",
            "AGENT_API_AUTH_USERNAME": "ops",
            "AGENT_API_AUTH_PASSWORD": "secret",
        },
        run_command=lambda _cmd: "active",
        health_check=_fake_health,
    )
    assert captured["auth"] == ("ops", "secret")


def test_collect_status_redacts_api_auth_password() -> None:
    from agent_app.installer.install import HealthCheckResult
    from agent_app.installer.plan import InstallConfig
    from agent_app.installer.status import _format_env, collect_status

    config = InstallConfig(api_auth_username="ops", api_auth_password="secret")
    status = collect_status(
        config,
        operator=_DEFAULT_OPERATOR,
        uv_runtime=_DEFAULT_UV,
        os_name="Linux",
        env={
            "AGENT_AGENT_PORT": "5100",
            "AGENT_API_AUTH_USERNAME": "ops",
            "AGENT_API_AUTH_PASSWORD": "secret",
        },
        run_command=lambda _cmd: "active",
        health_check=lambda url, auth=None: HealthCheckResult(ok=True, message="ok"),
    )
    formatted = "\n".join(_format_env(status.env))
    assert "AGENT_API_AUTH_PASSWORD=<redacted>" in formatted
    assert "AGENT_API_AUTH_USERNAME=ops" in formatted


def test_status_reports_operator_and_uv(tmp_path: Path) -> None:
    operator = OperatorIdentity(login="ops", uid=1001, home=tmp_path / "home" / "ops")
    runtime = UvRuntime(
        bin_path=Path("/home/ops/.local/bin/uv"),
        source="operator_home",
        searched=("/home/ops/.local/bin/uv",),
    )
    config = InstallConfig(user="ops")
    status = collect_status(
        config,
        operator=operator,
        uv_runtime=runtime,
        os_name="Linux",
        env={"AGENT_AGENT_PORT": "5100"},
        run_command=lambda cmd: "active",
        health_check=_stub_health,
    )
    rendered = format_status(status)
    assert "Operator: ops (uid 1001" in rendered
    assert "uv path: /home/ops/.local/bin/uv" in rendered


def test_collect_status_uses_operator_home_for_macos_plist(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    operator = OperatorIdentity(login="alice", uid=2002, home=tmp_path / "Users" / "alice")
    monkeypatch.setattr(Path, "home", lambda: operator.home)
    runtime = UvRuntime(bin_path=None, source="missing", searched=())
    config = InstallConfig(user="alice")
    status = collect_status(
        config,
        operator=operator,
        uv_runtime=runtime,
        os_name="Darwin",
        env={},
        run_command=lambda cmd: "",
        health_check=_stub_health,
    )
    expected = operator.home / "Library/LaunchAgents/com.gridfleet.agent.plist"
    assert status.service_file == expected


def test_status_reports_uv_missing(tmp_path: Path) -> None:
    operator = OperatorIdentity(login="ops", uid=1001, home=tmp_path / "home" / "ops")
    runtime = UvRuntime(bin_path=None, source="missing", searched=("/x", "/y"))
    config = InstallConfig(user="ops")
    status = collect_status(
        config,
        operator=operator,
        uv_runtime=runtime,
        os_name="Linux",
        env={},
        run_command=lambda cmd: "",
        health_check=_stub_health,
    )
    rendered = format_status(status)
    assert "uv path: not found" in rendered
    assert "/x" in rendered and "/y" in rendered
