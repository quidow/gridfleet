from __future__ import annotations

from pathlib import Path

import pytest

from agent_app.cli import main as cli_main
from agent_app.installer.identity import OperatorIdentity
from agent_app.installer.install import HealthCheckResult
from agent_app.installer.plan import InstallConfig
from agent_app.installer.update import (
    DrainResult,
    UpdateDrainError,
    UpdateHealthError,
    UpdateRestartError,
    UpdateResult,
    UpdateUpgradeError,
    UvNotFoundError,
    format_update_dry_run,
    update_agent,
    wait_for_update_drain,
)
from agent_app.installer.uv_runtime import UvRuntime


def _make_config(tmp_path: Path) -> InstallConfig:
    return InstallConfig(
        agent_dir=str(tmp_path / "opt/gridfleet-agent"),
        config_dir=str(tmp_path / "etc/gridfleet-agent"),
        port=5200,
    )


def _make_operator(tmp_path: Path, uid: int = 1001) -> OperatorIdentity:
    return OperatorIdentity(login="ops", uid=uid, home=tmp_path / "home" / "ops")


def _make_uv_runtime(tmp_path: Path) -> UvRuntime:
    """Create a real executable uv stub so build_upgrade_command accepts it."""
    bin_path = tmp_path / "uv"
    bin_path.write_text("")
    bin_path.chmod(0o755)
    return UvRuntime(bin_path=bin_path, source="operator_home", searched=())


def test_format_update_dry_run_names_uv_and_restart_commands(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    operator = _make_operator(tmp_path)
    uv_runtime = _make_uv_runtime(tmp_path)

    output = format_update_dry_run(
        config, operator=operator, uv_runtime=uv_runtime, to_version="0.3.0", os_name="Linux"
    )

    assert "GridFleet Agent update dry run" in output
    assert "pip install" in output
    assert "gridfleet-agent==0.3.0" in output
    assert "systemctl --user restart gridfleet-agent" in output
    assert "Wait for active local nodes to drain" in output
    assert "http://localhost:5200/agent/health" in output


def test_format_update_dry_run_reports_unsupported_os_without_traceback(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    operator = _make_operator(tmp_path)
    uv_runtime = _make_uv_runtime(tmp_path)

    output = format_update_dry_run(config, operator=operator, uv_runtime=uv_runtime, os_name="Plan9")

    assert "Restart service: unsupported OS: Plan9" in output


def test_update_agent_waits_for_drain_then_runs_uv_restart_and_health_check_on_linux(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    uv_runtime = _make_uv_runtime(tmp_path)
    operator = _make_operator(tmp_path, uid=1001)
    commands: list[list[str]] = []
    drains: list[str] = []

    def record(command: list[str]) -> None:
        commands.append(command)

    result = update_agent(
        config,
        operator=operator,
        uv_runtime=uv_runtime,
        to_version="0.3.0",
        os_name="Linux",
        current_uid=1001,
        run_command=record,
        drain_check=lambda url, *, auth=None: drains.append(url) or DrainResult(ok=True, message="drained"),
        health_check=lambda url, *, auth=None: HealthCheckResult(ok=True, message=f"healthy at {url}"),
    )

    assert result == UpdateResult(
        to_version="0.3.0",
        restarted=True,
        drain=DrainResult(ok=True, message="drained"),
        health=HealthCheckResult(ok=True, message="healthy at http://localhost:5200/agent/health"),
    )
    assert drains == ["http://localhost:5200/agent/health"]
    assert commands[0][1:4] == ["pip", "install", "--python"]
    assert commands[0][-2:] == ["--upgrade", "gridfleet-agent==0.3.0"]
    assert commands[1] == ["systemctl", "--user", "restart", "gridfleet-agent"]


def test_update_agent_without_version_upgrades_latest(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    uv_runtime = _make_uv_runtime(tmp_path)
    operator = _make_operator(tmp_path, uid=1001)
    commands: list[list[str]] = []

    def record(command: list[str]) -> None:
        commands.append(command)

    update_agent(
        config,
        operator=operator,
        uv_runtime=uv_runtime,
        to_version=None,
        os_name="Linux",
        current_uid=1001,
        run_command=record,
        drain_check=lambda _url, *, auth=None: DrainResult(ok=True, message="drained"),
        health_check=lambda _url, *, auth=None: HealthCheckResult(ok=True, message="healthy"),
    )

    assert commands[0][1:4] == ["pip", "install", "--python"]
    assert commands[0][-2:] == ["--upgrade", "gridfleet-agent"]


def test_update_agent_restarts_launchd_on_macos(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    uv_runtime = _make_uv_runtime(tmp_path)
    operator = _make_operator(tmp_path, uid=0)
    commands: list[list[str]] = []

    def record(command: list[str]) -> None:
        commands.append(command)

    monkeypatch.setattr("os.getuid", lambda: 0)
    update_agent(
        config,
        operator=operator,
        uv_runtime=uv_runtime,
        to_version="0.3.0",
        os_name="Darwin",
        current_uid=0,
        run_command=record,
        drain_check=lambda _url, *, auth=None: DrainResult(ok=True, message="drained"),
        health_check=lambda _url, *, auth=None: HealthCheckResult(ok=True, message="healthy"),
    )

    assert commands[1] == ["launchctl", "kickstart", "-k", "gui/0/com.gridfleet.agent"]


def test_update_agent_uses_current_uid_for_launchd_restart_on_macos(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = _make_config(tmp_path)
    uv_runtime = _make_uv_runtime(tmp_path)
    operator = OperatorIdentity(login="ops", uid=1001, home=tmp_path / "home" / "ops")
    commands: list[list[str]] = []

    def record(command: list[str]) -> None:
        commands.append(command)

    monkeypatch.setattr("os.getuid", lambda: 501)
    update_agent(
        config,
        operator=operator,
        uv_runtime=uv_runtime,
        to_version=None,
        os_name="Darwin",
        current_uid=1001,
        run_command=record,
        drain_check=lambda _url, *, auth=None: DrainResult(ok=True, message="drained"),
        health_check=lambda _url, *, auth=None: HealthCheckResult(ok=True, message="healthy"),
    )

    assert commands[1] == ["launchctl", "kickstart", "-k", "gui/501/com.gridfleet.agent"]


@pytest.mark.parametrize("os_name", ["Linux", "Darwin"])
def test_update_agent_refuses_to_upgrade_when_drain_times_out(tmp_path: Path, os_name: str) -> None:
    config = _make_config(tmp_path)
    uv_runtime = _make_uv_runtime(tmp_path)
    operator = _make_operator(tmp_path)
    commands: list[list[str]] = []

    with pytest.raises(UpdateDrainError, match="active local nodes remain"):
        update_agent(
            config,
            operator=operator,
            uv_runtime=uv_runtime,
            to_version="0.3.0",
            os_name=os_name,
            current_uid=1001,
            run_command=lambda command: commands.append(command),
            drain_check=lambda _url, *, auth=None: DrainResult(ok=False, message="active local nodes remain"),
            health_check=lambda _url, *, auth=None: HealthCheckResult(ok=True, message="healthy"),
        )

    assert commands == []


@pytest.mark.parametrize("os_name", ["Linux", "Darwin"])
def test_update_agent_raises_when_uv_not_installed(tmp_path: Path, os_name: str) -> None:
    config = _make_config(tmp_path)
    # bin_path=None → build_upgrade_command raises RuntimeError → UvNotFoundError
    uv_runtime = UvRuntime(bin_path=None, source="missing", searched=("/usr/local/bin/uv",))
    operator = _make_operator(tmp_path)

    with pytest.raises(UvNotFoundError, match="uv not found"):
        update_agent(
            config,
            operator=operator,
            uv_runtime=uv_runtime,
            to_version="0.3.0",
            os_name=os_name,
            current_uid=1001,
            run_command=lambda _command: None,
            drain_check=lambda _url, *, auth=None: DrainResult(ok=True, message="drained"),
            health_check=lambda _url, *, auth=None: HealthCheckResult(ok=True, message="healthy"),
        )


def test_wait_for_update_drain_returns_success_when_running_nodes_are_empty() -> None:
    class Response:
        status_code = 200

        @staticmethod
        def json() -> dict[str, object]:
            return {"appium_processes": {"running_nodes": []}}

    result = wait_for_update_drain(
        "http://localhost:5200/agent/health",
        timeout_sec=0.1,
        interval_sec=0.01,
        get=lambda _url, timeout=2.0: Response(),
    )

    assert result == DrainResult(ok=True, message="no active local nodes")


def test_wait_for_update_drain_times_out_while_running_nodes_remain() -> None:
    class Response:
        status_code = 200

        @staticmethod
        def json() -> dict[str, object]:
            return {"appium_processes": {"running_nodes": [{"port": 4723}]}}

    result = wait_for_update_drain(
        "http://localhost:5200/agent/health",
        timeout_sec=0.01,
        interval_sec=0.01,
        get=lambda _url, timeout=2.0: Response(),
    )

    assert result.ok is False
    assert "1 active local node" in result.message


def test_wait_for_update_drain_passes_basic_auth() -> None:
    class Response:
        status_code = 200

        @staticmethod
        def json() -> dict[str, object]:
            return {"appium_processes": {"running_nodes": []}}

    captured: list[tuple[str, str] | None] = []

    def fake_get(url: str, *, timeout: float = 2.0, auth: tuple[str, str] | None = None) -> Response:
        captured.append(auth)
        return Response()

    result = wait_for_update_drain(
        "http://localhost:5200/agent/health",
        timeout_sec=5.0,
        interval_sec=0.01,
        get=fake_get,
        auth=("ops", "secret"),
    )

    assert result == DrainResult(ok=True, message="no active local nodes")
    assert captured == [("ops", "secret")]


def test_update_agent_forwards_api_auth_to_drain_and_health(tmp_path: Path) -> None:
    config = InstallConfig(
        agent_dir=str(tmp_path / "opt/gridfleet-agent"),
        config_dir=str(tmp_path / "etc/gridfleet-agent"),
        port=5200,
        api_auth_username="ops",
        api_auth_password="secret",
    )
    uv_runtime = _make_uv_runtime(tmp_path)
    operator = _make_operator(tmp_path, uid=1001)

    drain_auths: list[tuple[str, str] | None] = []
    health_auths: list[tuple[str, str] | None] = []

    def fake_drain(url: str, *, auth: tuple[str, str] | None = None) -> DrainResult:
        drain_auths.append(auth)
        return DrainResult(ok=True, message="drained")

    def fake_health(url: str, *, auth: tuple[str, str] | None = None) -> HealthCheckResult:
        health_auths.append(auth)
        return HealthCheckResult(ok=True, message="healthy")

    update_agent(
        config,
        operator=operator,
        uv_runtime=uv_runtime,
        to_version=None,
        os_name="Linux",
        current_uid=1001,
        run_command=lambda _command: None,
        drain_check=fake_drain,
        health_check=fake_health,
    )

    assert drain_auths == [("ops", "secret")]
    assert health_auths == [("ops", "secret")]


# ---------------------------------------------------------------------------
# New tests from Task 8
# ---------------------------------------------------------------------------


def test_update_drain_failure_raises_typed_and_skips_upgrade(tmp_path: Path) -> None:
    invoked: list[list[str]] = []
    operator = OperatorIdentity(login="ops", uid=1001, home=tmp_path / "home" / "ops")
    runtime = UvRuntime(bin_path=tmp_path / "uv", source="operator_home", searched=())

    with pytest.raises(UpdateDrainError):
        update_agent(
            InstallConfig(user="ops"),
            operator=operator,
            uv_runtime=runtime,
            os_name="Linux",
            run_command=lambda cmd: invoked.append(list(cmd)),
            drain_check=lambda url, **kw: DrainResult(ok=False, message="busy"),
            current_uid=0,
        )
    assert invoked == []


def test_update_uv_missing_raises_typed(tmp_path: Path) -> None:
    operator = OperatorIdentity(login="ops", uid=1001, home=tmp_path / "home" / "ops")
    runtime = UvRuntime(bin_path=None, source="missing", searched=("/foo",))

    with pytest.raises(UvNotFoundError):
        update_agent(
            InstallConfig(user="ops"),
            operator=operator,
            uv_runtime=runtime,
            os_name="Linux",
            run_command=lambda cmd: None,
            drain_check=lambda url, **kw: DrainResult(ok=True, message="idle"),
            current_uid=0,
        )


def test_update_runs_uv_pip_install_to_dedicated_venv(tmp_path: Path) -> None:
    bin_path = tmp_path / "uv"
    bin_path.write_text("")
    bin_path.chmod(0o755)
    operator = OperatorIdentity(login="ops", uid=1001, home=tmp_path / "home" / "ops")
    runtime = UvRuntime(bin_path=bin_path, source="operator_home", searched=())
    invoked: list[list[str]] = []

    update_agent(
        InstallConfig(user="ops", agent_dir=str(tmp_path / "agent")),
        operator=operator,
        uv_runtime=runtime,
        os_name="Linux",
        run_command=lambda cmd: invoked.append(list(cmd)),
        drain_check=lambda url, **kw: DrainResult(ok=True, message="idle"),
        health_check=lambda url, **kw: HealthCheckResult(ok=True, message="ok"),
        current_uid=0,
    )
    upgrade_cmd = invoked[0]
    assert upgrade_cmd[0] == str(bin_path)
    assert upgrade_cmd[1:4] == ["pip", "install", "--python"]
    assert upgrade_cmd[-2:] == ["--upgrade", "gridfleet-agent"]


def test_cli_update_invalid_uv_bin_exits_one(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def raiser(*args: object, **kwargs: object) -> None:
        raise RuntimeError("--uv-bin '/missing' is not an executable file; refusing to fall back to discovery")

    monkeypatch.setattr("agent_app.cli.discover_uv", raiser)
    monkeypatch.setattr("agent_app.cli.load_installed_config", lambda: InstallConfig(user="ops"))
    monkeypatch.setattr(
        "agent_app.cli.resolve_operator_identity",
        lambda login=None: OperatorIdentity(login="ops", uid=1001, home=Path("/home/ops")),
    )
    rc = cli_main(["update", "--uv-bin", "/missing"])
    assert rc == 1
    err = capsys.readouterr().err
    assert err.startswith("ERROR:")
    assert "Traceback" not in err


def test_cli_update_dry_run_invalid_uv_bin_exits_one(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def raiser(*args: object, **kwargs: object) -> None:
        raise RuntimeError("--uv-bin '/missing' is not an executable file; refusing to fall back to discovery")

    monkeypatch.setattr("agent_app.cli.discover_uv", raiser)
    monkeypatch.setattr("agent_app.cli.load_installed_config", lambda: InstallConfig(user="ops"))
    monkeypatch.setattr(
        "agent_app.cli.resolve_operator_identity",
        lambda login=None: OperatorIdentity(login="ops", uid=1001, home=Path("/home/ops")),
    )
    rc = cli_main(["update", "--dry-run", "--uv-bin", "/missing"])
    assert rc == 1
    err = capsys.readouterr().err
    assert err.startswith("ERROR:")
    assert "Traceback" not in err


def test_cli_update_exit_code_for_drain(monkeypatch: pytest.MonkeyPatch) -> None:
    def raiser(*args: object, **kwargs: object) -> None:
        raise UpdateDrainError("busy")

    monkeypatch.setattr("agent_app.cli.update_agent", raiser)
    monkeypatch.setattr("agent_app.cli.load_installed_config", lambda: InstallConfig(user="ops"))
    monkeypatch.setattr(
        "agent_app.cli.discover_uv",
        lambda **kw: UvRuntime(bin_path=Path("/x"), source="path", searched=("/x",)),
    )
    monkeypatch.setattr(
        "agent_app.cli.resolve_operator_identity",
        lambda login=None: OperatorIdentity(login="ops", uid=1001, home=Path("/home/ops")),
    )
    rc = cli_main(["update"])
    assert rc == 1


def test_cli_update_exit_code_for_restart_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def raiser(*args: object, **kwargs: object) -> None:
        raise UpdateRestartError("systemctl failed")

    monkeypatch.setattr("agent_app.cli.update_agent", raiser)
    monkeypatch.setattr("agent_app.cli.load_installed_config", lambda: InstallConfig(user="ops"))
    monkeypatch.setattr(
        "agent_app.cli.discover_uv",
        lambda **kw: UvRuntime(bin_path=Path("/x"), source="path", searched=("/x",)),
    )
    monkeypatch.setattr(
        "agent_app.cli.resolve_operator_identity",
        lambda login=None: OperatorIdentity(login="ops", uid=1001, home=Path("/home/ops")),
    )
    rc = cli_main(["update"])
    assert rc == 2


@pytest.mark.parametrize("os_name", ["Linux", "Darwin"])
def test_update_health_failure_raises_typed(tmp_path: Path, os_name: str) -> None:
    bin_path = tmp_path / "uv"
    bin_path.write_text("")
    bin_path.chmod(0o755)
    operator = OperatorIdentity(login="ops", uid=1001, home=tmp_path / "home" / "ops")
    runtime = UvRuntime(bin_path=bin_path, source="operator_home", searched=())

    with pytest.raises(UpdateHealthError):
        update_agent(
            InstallConfig(user="ops"),
            operator=operator,
            uv_runtime=runtime,
            os_name=os_name,
            run_command=lambda cmd: None,
            drain_check=lambda url, **kw: DrainResult(ok=True, message="idle"),
            health_check=lambda url, **kw: HealthCheckResult(ok=False, message="unhealthy"),
            current_uid=0,
        )


def _raise_oserror(_cmd: list[str]) -> None:
    raise FileNotFoundError("[Errno 2] No such file or directory: 'sudo'")


def test_update_upgrade_oserror_wraps_typed(tmp_path: Path) -> None:
    bin_path = tmp_path / "uv"
    bin_path.write_text("")
    bin_path.chmod(0o755)
    operator = OperatorIdentity(login="ops", uid=1001, home=tmp_path / "home" / "ops")
    runtime = UvRuntime(bin_path=bin_path, source="operator_home", searched=())

    with pytest.raises(UpdateUpgradeError):
        update_agent(
            InstallConfig(user="ops"),
            operator=operator,
            uv_runtime=runtime,
            os_name="Linux",
            run_command=_raise_oserror,
            drain_check=lambda url, **kw: DrainResult(ok=True, message="idle"),
            current_uid=0,
        )


def test_update_restart_oserror_wraps_typed(tmp_path: Path) -> None:
    bin_path = tmp_path / "uv"
    bin_path.write_text("")
    bin_path.chmod(0o755)
    operator = OperatorIdentity(login="ops", uid=1001, home=tmp_path / "home" / "ops")
    runtime = UvRuntime(bin_path=bin_path, source="operator_home", searched=())
    calls = 0

    def fake_run(cmd: list[str]) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise FileNotFoundError("[Errno 2] No such file or directory: 'systemctl'")

    with pytest.raises(UpdateRestartError):
        update_agent(
            InstallConfig(user="ops"),
            operator=operator,
            uv_runtime=runtime,
            os_name="Linux",
            run_command=fake_run,
            drain_check=lambda url, **kw: DrainResult(ok=True, message="idle"),
            current_uid=0,
        )
