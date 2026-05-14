import asyncio
import collections
import contextlib
import json
import signal
from collections import deque
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import httpx
import pytest

from agent_app.appium.process import (
    AlreadyRunningError,
    AppiumInvocation,
    AppiumLaunchSpec,
    AppiumProcessInfo,
    AppiumProcessManager,
    DeviceNotFoundError,
    InvalidStartPayloadError,
    PortOccupiedError,
    RuntimeMissingError,
    RuntimeNotInstalledError,
    StartupTimeoutError,
    _build_env,
    _find_java,
    _has_lifecycle_action,
    sanitize_appium_driver_capabilities,
)
from agent_app.grid_node.config import GridNodeConfig
from agent_app.pack.adapter_registry import AdapterRegistry
from agent_app.pack.adapter_types import LifecycleActionResult
from agent_app.tools.paths import _parse_node_version

_STUB_INVOCATION = AppiumInvocation(binary="/usr/local/bin/appium")
PACK_START_KWARGS = {"pack_id": "appium-uiautomator2", "platform_id": "android_mobile"}


@pytest.fixture(autouse=True)
def stub_port_probe() -> object:
    with (
        patch(
            "agent_app.appium.process.AppiumProcessManager._can_connect_to_appium",
            new_callable=AsyncMock,
            return_value=False,
        ),
        patch("agent_app.appium.process.start_grid_node_supervisor", return_value=RecordingGridNodeHandle()),
        patch("agent_app.registration.get_local_ip", return_value="127.0.0.1"),
    ):
        yield


def _stream_with_lines(*lines: str) -> asyncio.StreamReader:
    reader = asyncio.StreamReader()
    for line in lines:
        reader.feed_data(f"{line}\n".encode())
    reader.feed_eof()
    return reader


class FakeProcess:
    def __init__(
        self,
        *,
        pid: int,
        returncode: int | None = None,
        stdout: asyncio.StreamReader | None = None,
        stderr: asyncio.StreamReader | None = None,
    ) -> None:
        self.pid = pid
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.sent_signals: list[int] = []
        self.killed = False
        self.wait_calls = 0
        self._wait_future: asyncio.Future[int] | None = None

    def send_signal(self, sig: int) -> None:
        self.sent_signals.append(sig)
        self.set_exit(0)

    def kill(self) -> None:
        self.killed = True
        self.set_exit(-9)

    def set_exit(self, code: int) -> None:
        self.returncode = code
        if self._wait_future is not None and not self._wait_future.done():
            self._wait_future.set_result(code)

    def wait(self) -> asyncio.Future[int]:
        self.wait_calls += 1
        loop = asyncio.get_running_loop()
        if self._wait_future is None:
            self._wait_future = loop.create_future()
            if self.returncode is not None:
                self._wait_future.set_result(self.returncode)
        return asyncio.shield(self._wait_future)


class RecordingGridNodeHandle:
    def __init__(self) -> None:
        self.start_called = False
        self.stop_called = False
        self.wait_until_running_called = False
        self.snapshot_payload = {"status": "up"}

    async def start(self) -> None:
        self.start_called = True

    async def wait_until_running(self) -> None:
        self.wait_until_running_called = True

    async def stop(self) -> None:
        self.stop_called = True

    def is_running(self) -> bool:
        return self.start_called and not self.stop_called

    def snapshot(self) -> dict[str, object]:
        return dict(self.snapshot_payload)


class FailingGridNodeHandle(RecordingGridNodeHandle):
    async def wait_until_running(self) -> None:
        self.wait_until_running_called = True
        raise RuntimeError("grid node failed")


class ReconfigurableGridNodeService:
    def __init__(self, *, busy: bool = False) -> None:
        self.calls: list[dict[str, object]] = []
        self.busy = busy

    def slot_stereotype_caps(self) -> dict[str, object]:
        return {"platformName": "Android", "gridfleet:run_id": "free", "gridfleet:available": True}

    def has_active_session(self) -> bool:
        return self.busy

    async def reregister_with_stereotype(
        self, *, new_caps: dict[str, object], drain_grace_sec: float | None = None
    ) -> None:
        self.calls.append(dict(new_caps))


class ReconfigurableGridNodeHandle(RecordingGridNodeHandle):
    def __init__(self, service: ReconfigurableGridNodeService) -> None:
        super().__init__()
        self.service = service


def test_parse_node_version_prefers_version_tuple() -> None:
    assert _parse_node_version("/Users/me/.nvm/versions/node/v24.12.0/bin/appium") == (24, 12, 0)
    assert _parse_node_version("/usr/local/bin/appium") == (0,)


def test_build_env_adds_paths() -> None:
    with (
        patch("agent_app.appium.process._find_java", return_value="/usr/bin/java"),
        patch("agent_app.appium.process.os.path.realpath", return_value="/usr/lib/jvm/java-21/bin/java"),
        patch("agent_app.appium.process.os.path.isfile", return_value=True),
        patch("agent_app.appium.process.os.access", return_value=True),
        patch("agent_app.appium.process._find_adb", return_value="/opt/android/platform-tools/adb"),
        patch("agent_app.appium.process.find_android_home", return_value="/opt/android"),
        patch.dict("os.environ", {"PATH": "/usr/local/bin"}, clear=True),
    ):
        env = _build_env(
            platform_name="tvos",
            device_type="real_device",
            appium_bin="/usr/local/bin/appium",
        )

    assert env["ANDROID_HOME"] == "/opt/android"
    assert env["ANDROID_SDK_ROOT"] == "/opt/android"
    assert env["JAVA_HOME"] == "/usr/lib/jvm/java-21"
    assert "APPIUM_XCUITEST_PREFER_DEVICECTL" not in env
    assert env["PATH"].startswith("/opt/android/platform-tools:/usr/bin:/usr/local/bin")


def test_build_env_applies_workaround_env() -> None:
    with (
        patch("agent_app.appium.process._find_java", return_value="/usr/bin/java"),
        patch("agent_app.appium.process.os.path.realpath", return_value="/usr/lib/jvm/java-21/bin/java"),
        patch("agent_app.appium.process.os.path.isfile", return_value=True),
        patch("agent_app.appium.process.os.access", return_value=True),
        patch("agent_app.appium.process._find_adb", return_value="/opt/android/platform-tools/adb"),
        patch("agent_app.appium.process.find_android_home", return_value="/opt/android"),
        patch.dict("os.environ", {"PATH": "/usr/bin"}, clear=True),
    ):
        env = _build_env(
            platform_name="tvos",
            device_type="real_device",
            appium_bin="/usr/local/bin/appium",
            appium_home="/tmp/h",
            workaround_env={"APPIUM_XCUITEST_PREFER_DEVICECTL": "1"},
        )
    assert env["APPIUM_XCUITEST_PREFER_DEVICECTL"] == "1"


def test_build_env_does_not_set_devicectl_pref_when_workaround_env_omitted() -> None:
    with (
        patch("agent_app.appium.process._find_java", return_value="/usr/bin/java"),
        patch("agent_app.appium.process.os.path.realpath", return_value="/usr/lib/jvm/java-21/bin/java"),
        patch("agent_app.appium.process.os.path.isfile", return_value=True),
        patch("agent_app.appium.process.os.access", return_value=True),
        patch("agent_app.appium.process._find_adb", return_value="/opt/android/platform-tools/adb"),
        patch("agent_app.appium.process.find_android_home", return_value="/opt/android"),
        patch.dict("os.environ", {"PATH": "/usr/bin"}, clear=True),
    ):
        env = _build_env(
            platform_name="tvos",
            device_type="real_device",
            appium_bin="/usr/local/bin/appium",
            appium_home="/tmp/h",
            workaround_env=None,
        )
    assert "APPIUM_XCUITEST_PREFER_DEVICECTL" not in env


def test_build_env_does_not_derive_java_home_from_fallback_command() -> None:
    with (
        patch("agent_app.appium.process._find_java", return_value="java"),
        patch("agent_app.appium.process._find_adb", return_value="/opt/android/platform-tools/adb"),
        patch("agent_app.appium.process.find_android_home", return_value="/opt/android"),
        patch.dict("os.environ", {"PATH": "/usr/local/bin"}, clear=True),
    ):
        env = _build_env(appium_bin="/usr/local/bin/appium")

    assert "JAVA_HOME" not in env


def test_find_java_prefers_macos_java_home_over_usr_bin_stub() -> None:
    java_home_result = MagicMock(returncode=0, stdout="/Library/Java/JavaVirtualMachines/jdk-21.jdk/Contents/Home\n")
    with (
        patch("agent_app.appium.process.platform.system", return_value="Darwin"),
        patch("agent_app.appium.process.shutil.which", return_value="/usr/bin/java"),
        patch("agent_app.appium.process.os.path.realpath", return_value="/usr/bin/java"),
        patch("agent_app.appium.process.os.path.isdir", return_value=False),
        patch("agent_app.appium.process.os.path.isfile", return_value=True),
        patch("agent_app.appium.process.os.access", return_value=True),
        patch("agent_app.appium.process.subprocess.run", return_value=java_home_result),
        patch.dict("os.environ", {}, clear=True),
    ):
        assert _find_java() == "/Library/Java/JavaVirtualMachines/jdk-21.jdk/Contents/Home/bin/java"


async def test_start_builds_processes_and_tracks_running_info() -> None:
    manager = AppiumProcessManager()
    appium_proc = FakeProcess(
        pid=1234,
        stdout=_stream_with_lines("appium ready"),
        stderr=_stream_with_lines("appium stderr"),
    )

    with (
        patch("agent_app.appium.process.resolve_appium_invocation_for_pack", return_value=_STUB_INVOCATION),
        patch("agent_app.appium.process._build_env", return_value={"PATH": "/usr/bin"}),
        patch.object(manager, "_wait_for_readiness", new_callable=AsyncMock, return_value=True) as wait_ready,
        patch(
            "agent_app.appium.process.asyncio.create_subprocess_exec",
            return_value=appium_proc,
        ) as create_proc,
    ):
        info = await manager.start(
            connection_target="device-001",
            port=4723,
            grid_url="http://grid:4444",
            **PACK_START_KWARGS,
            plugins=["images", "execute-driver"],
            extra_caps={"appium:platform": "phone"},
        )
        await asyncio.sleep(0)

    assert info == AppiumProcessInfo(
        port=4723,
        pid=1234,
        connection_target="device-001",
        platform_id="android_mobile",
    )
    assert wait_ready.await_count == 1
    assert manager.list_running() == [info]
    assert create_proc.await_args_list[0].args[:5] == (
        "/usr/local/bin/appium",
        "server",
        "--port",
        "4723",
        "--default-capabilities",
    )
    assert "--session-override" in create_proc.await_args_list[0].args
    assert "--use-plugins" in create_proc.await_args_list[0].args
    logs = manager.get_logs(4723)
    assert any("appium ready" in line for line in logs)
    await manager.shutdown()


async def test_start_spawns_grid_node_supervisor() -> None:
    manager = AppiumProcessManager()
    appium_proc = FakeProcess(pid=5001)
    handles: list[RecordingGridNodeHandle] = []
    configs: list[GridNodeConfig] = []

    def start_supervisor(*, factory: object, config: GridNodeConfig) -> RecordingGridNodeHandle:
        del factory
        configs.append(config)
        handle = RecordingGridNodeHandle()
        handles.append(handle)
        return handle

    with (
        patch("agent_app.appium.process.resolve_appium_invocation_for_pack", return_value=_STUB_INVOCATION),
        patch("agent_app.appium.process._build_env", return_value={"PATH": "/usr/bin"}),
        patch.object(manager, "_wait_for_readiness", new_callable=AsyncMock, return_value=True),
        patch("agent_app.appium.process.asyncio.create_subprocess_exec", return_value=appium_proc) as create_proc,
        patch("agent_app.appium.process.start_grid_node_supervisor", side_effect=start_supervisor),
    ):
        await manager.start(
            connection_target="device-1",
            port=4723,
            grid_url="http://grid:4444",
            **PACK_START_KWARGS,
            stereotype_caps={"appium:platform": "android_mobile"},
        )

    assert create_proc.await_count == 1
    assert handles[0].start_called is True
    assert handles[0].wait_until_running_called is True
    assert configs[0].appium_upstream == "http://127.0.0.1:4723"
    assert configs[0].slots[0].stereotype.caps["appium:platform"] == "android_mobile"
    await manager.shutdown()


async def test_start_rolls_back_appium_when_grid_node_start_fails() -> None:
    manager = AppiumProcessManager()
    appium_proc = FakeProcess(pid=5004)
    handle = FailingGridNodeHandle()

    with (
        patch("agent_app.appium.process.resolve_appium_invocation_for_pack", return_value=_STUB_INVOCATION),
        patch("agent_app.appium.process._build_env", return_value={"PATH": "/usr/bin"}),
        patch.object(manager, "_wait_for_readiness", new_callable=AsyncMock, return_value=True),
        patch("agent_app.appium.process.asyncio.create_subprocess_exec", return_value=appium_proc),
        patch("agent_app.appium.process.start_grid_node_supervisor", return_value=handle),
        pytest.raises(RuntimeError, match="grid node failed"),
    ):
        await manager.start(
            connection_target="device-1",
            port=4723,
            grid_url="http://grid:4444",
            **PACK_START_KWARGS,
        )

    assert handle.stop_called is True
    assert appium_proc.sent_signals
    assert 4723 not in manager._grid_supervisors
    assert 4723 not in manager._appium_procs
    assert 4723 not in manager._launch_specs


async def test_stop_calls_grid_node_supervisor_stop() -> None:
    manager = AppiumProcessManager()
    handle = RecordingGridNodeHandle()
    appium_proc = FakeProcess(pid=5002)
    manager._grid_supervisors[4723] = handle
    manager._appium_procs[4723] = cast("asyncio.subprocess.Process", appium_proc)
    manager._info[4723] = AppiumProcessInfo(
        port=4723, pid=5002, connection_target="device-1", platform_id="android_mobile"
    )
    manager._logs[4723] = deque(["line"], maxlen=10)
    manager._log_tasks[4723] = []

    await manager.stop(4723)

    assert handle.stop_called is True
    assert 4723 not in manager._grid_supervisors


def test_process_snapshot_includes_grid_node_status() -> None:
    manager = AppiumProcessManager()
    handle = RecordingGridNodeHandle()
    manager._grid_supervisors[4723] = handle
    manager._appium_procs[4723] = cast("asyncio.subprocess.Process", FakeProcess(pid=5003))
    manager._info[4723] = AppiumProcessInfo(
        port=4723, pid=5003, connection_target="device-1", platform_id="android_mobile"
    )

    snapshot = manager.process_snapshot()

    assert snapshot["running_nodes"][0]["grid_node_status"] == "up"


async def test_start_requires_pack_metadata() -> None:
    manager = AppiumProcessManager()

    with pytest.raises(RuntimeError, match="requires pack_id and platform_id"):
        await manager.start(
            connection_target="Pixel_8",
            port=4723,
            grid_url="http://localhost:4444",
            pack_id=None,
            platform_id="android_mobile",
            manage_grid_node=False,
        )

    with pytest.raises(RuntimeError, match="requires pack_id and platform_id"):
        await manager.start(
            connection_target="Pixel_8",
            port=4723,
            grid_url="http://localhost:4444",
            pack_id="appium-uiautomator2",
            platform_id=None,
            manage_grid_node=False,
        )


async def test_start_uses_stereotype_caps_only_for_grid_matching() -> None:
    manager = AppiumProcessManager()
    appium_proc = FakeProcess(pid=1234)
    configs: list[GridNodeConfig] = []

    with (
        patch("agent_app.appium.process.resolve_appium_invocation_for_pack", return_value=_STUB_INVOCATION),
        patch("agent_app.appium.process._build_env", return_value={"PATH": "/usr/bin"}),
        patch.object(manager, "_wait_for_readiness", new_callable=AsyncMock, return_value=True),
        patch(
            "agent_app.appium.process.asyncio.create_subprocess_exec",
            return_value=appium_proc,
        ) as create_proc,
        patch(
            "agent_app.appium.process.start_grid_node_supervisor",
            side_effect=lambda *, factory, config: configs.append(config) or RecordingGridNodeHandle(),
        ),
    ):
        await manager.start(
            connection_target="device-002",
            port=4724,
            grid_url="http://grid:4444",
            **PACK_START_KWARGS,
            extra_caps={
                "appium:automationName": "UiAutomator2",
                "appium:gridfleet:deviceId": "device-id",
                "appium:platform": "android_mobile",
            },
            stereotype_caps={"appium:platform": "android_mobile"},
        )

    # Appium server receives only driver-owned caps.
    default_caps = json.loads(create_proc.await_args_list[0].args[5])
    assert default_caps == {
        "appium:udid": "device-002",
        "platformName": "android_mobile",
        "appium:automationName": "UiAutomator2",
    }
    assert configs[0].slots[0].stereotype.caps == {
        "appium:udid": "device-002",
        "platformName": "android_mobile",
        "appium:platform": "android_mobile",
        "gridfleet:run_id": "free",
        "gridfleet:available": True,
    }
    await manager.shutdown()


async def test_start_with_accepting_new_sessions_false_marks_grid_unavailable() -> None:
    manager = AppiumProcessManager()
    appium_proc = FakeProcess(pid=5678)
    configs: list[GridNodeConfig] = []
    run_id = uuid4()

    with (
        patch("agent_app.appium.process.resolve_appium_invocation_for_pack", return_value=_STUB_INVOCATION),
        patch("agent_app.appium.process._build_env", return_value={"PATH": "/usr/bin"}),
        patch.object(manager, "_wait_for_readiness", new_callable=AsyncMock, return_value=True),
        patch("agent_app.appium.process.asyncio.create_subprocess_exec", return_value=appium_proc),
        patch(
            "agent_app.appium.process.start_grid_node_supervisor",
            side_effect=lambda *, factory, config: configs.append(config) or RecordingGridNodeHandle(),
        ),
    ):
        await manager.start(
            connection_target="device-unavailable",
            port=4728,
            grid_url="http://grid:4444",
            **PACK_START_KWARGS,
            accepting_new_sessions=False,
            grid_run_id=run_id,
        )

    caps = configs[0].slots[0].stereotype.caps
    assert caps["gridfleet:available"] is False
    assert caps["gridfleet:run_id"] == str(run_id)
    await manager.shutdown()


async def test_reconfigure_updates_grid_stereotype() -> None:
    manager = AppiumProcessManager()
    run_id = uuid4()
    service = ReconfigurableGridNodeService()
    manager._grid_supervisors[4723] = cast("Any", ReconfigurableGridNodeHandle(service))
    manager._info[4723] = AppiumProcessInfo(
        port=4723,
        pid=123,
        connection_target="device-1",
        platform_id="android_mobile",
    )

    await manager.reconfigure(
        4723,
        accepting_new_sessions=False,
        stop_pending=False,
        grid_run_id=run_id,
    )

    assert service.calls == [{"platformName": "Android", "gridfleet:run_id": str(run_id), "gridfleet:available": False}]


async def test_reconfigure_unknown_port_raises_device_not_found() -> None:
    manager = AppiumProcessManager()

    with pytest.raises(DeviceNotFoundError):
        await manager.reconfigure(
            4723,
            accepting_new_sessions=True,
            stop_pending=False,
            grid_run_id=None,
        )


async def test_stop_pending_stops_when_no_grid_session_and_blocks_auto_restart() -> None:
    manager = AppiumProcessManager()
    service = ReconfigurableGridNodeService(busy=False)
    handle = ReconfigurableGridNodeHandle(service)
    appium_proc = FakeProcess(pid=5002)
    manager._grid_supervisors[4723] = cast("Any", handle)
    manager._appium_procs[4723] = cast("asyncio.subprocess.Process", appium_proc)
    manager._info[4723] = AppiumProcessInfo(
        port=4723,
        pid=5002,
        connection_target="device-1",
        platform_id="android_mobile",
    )
    manager._logs[4723] = deque(["line"], maxlen=10)
    manager._log_tasks[4723] = []

    await manager.reconfigure(4723, accepting_new_sessions=False, stop_pending=True, grid_run_id=None)
    await manager._auto_restart_appium(4723, exit_code=9)

    assert handle.stop_called is True
    assert 4723 not in manager._grid_supervisors
    assert manager.process_snapshot()["recent_restart_events"] == []


async def test_start_can_disable_session_override() -> None:
    manager = AppiumProcessManager()
    appium_proc = FakeProcess(pid=1234)

    with (
        patch("agent_app.appium.process.resolve_appium_invocation_for_pack", return_value=_STUB_INVOCATION),
        patch("agent_app.appium.process._build_env", return_value={"PATH": "/usr/bin"}),
        patch("agent_app.appium.process.os.path.isfile", return_value=False),
        patch.object(manager, "_wait_for_readiness", new_callable=AsyncMock, return_value=True),
        patch("agent_app.appium.process.asyncio.create_subprocess_exec", return_value=appium_proc) as create_proc,
    ):
        await manager.start(
            connection_target="device-override-off",
            port=4726,
            grid_url="http://grid:4444",
            **PACK_START_KWARGS,
            session_override=False,
        )

    assert "--session-override" not in create_proc.await_args_list[0].args
    await manager.shutdown()


async def test_start_timeout_cleans_up_and_surfaces_logs() -> None:
    manager = AppiumProcessManager()
    appium_proc = FakeProcess(
        pid=4321, stdout=_stream_with_lines("booting"), stderr=_stream_with_lines("still booting")
    )

    with (
        patch("agent_app.appium.process.resolve_appium_invocation_for_pack", return_value=_STUB_INVOCATION),
        patch("agent_app.appium.process._build_env", return_value={"PATH": "/usr/bin"}),
        patch.object(manager, "_wait_for_readiness", new_callable=AsyncMock, return_value=False),
        patch("agent_app.appium.process.asyncio.create_subprocess_exec", return_value=appium_proc),
        patch("agent_app.appium.process.asyncio.sleep", new_callable=AsyncMock),
        pytest.raises(RuntimeError, match="did not become ready"),
    ):
        await manager.start(
            connection_target="device-002",
            port=4724,
            grid_url="http://grid:4444",
            **PACK_START_KWARGS,
        )

    assert appium_proc.killed is True
    assert manager.list_running() == []
    assert manager.get_logs(4724) == []


async def test_stop_escalates_to_kill_after_timeout() -> None:
    manager = AppiumProcessManager()
    appium_proc = FakeProcess(pid=8765, returncode=None)
    handle = RecordingGridNodeHandle()
    manager._appium_procs[4723] = cast("asyncio.subprocess.Process", appium_proc)
    manager._grid_supervisors[4723] = handle
    manager._info[4723] = AppiumProcessInfo(
        port=4723, pid=8765, connection_target="device-001", platform_id="android_mobile"
    )
    manager._logs[4723] = deque(["line"], maxlen=10)
    manager._log_tasks[4723] = []

    async def wait_for_side_effect(_awaitable: object, *, timeout: float) -> object:
        del timeout
        raise TimeoutError

    with patch("agent_app.appium.process.asyncio.wait_for", side_effect=wait_for_side_effect):
        await manager.stop(4723)

    assert handle.stop_called is True
    assert appium_proc.killed is True
    assert manager.list_running() == []


async def test_status_reports_running_and_stopped() -> None:
    manager = AppiumProcessManager()
    proc = FakeProcess(pid=1234)
    manager._appium_procs[4723] = cast("asyncio.subprocess.Process", proc)
    response = MagicMock(spec=httpx.Response)
    response.status_code = 200
    response.json.return_value = {"value": {"ready": True}}

    client = AsyncMock()
    client.__aenter__.return_value = client
    client.__aexit__.return_value = False
    client.get = AsyncMock(return_value=response)

    with patch("agent_app.appium.process.httpx.AsyncClient", return_value=client):
        running = await manager.status(4723)

    assert running["running"] is True
    assert running["pid"] == 1234
    assert running["appium_status"] == {"value": {"ready": True}}

    proc.set_exit(1)
    client.get = AsyncMock(side_effect=httpx.ConnectError("down"))
    with patch("agent_app.appium.process.httpx.AsyncClient", return_value=client):
        stopped = await manager.status(4723)

    assert stopped == {"running": False, "port": 4723}


async def test_start_fails_fast_when_port_has_unmanaged_listener() -> None:
    manager = AppiumProcessManager()
    with (
        patch("agent_app.appium.process.resolve_appium_invocation_for_pack", return_value=_STUB_INVOCATION),
        patch("agent_app.appium.process._build_env", return_value={"PATH": "/usr/bin"}),
        patch.object(manager, "_can_connect_to_appium", new_callable=AsyncMock, return_value=True),
        patch("agent_app.appium.process.asyncio.create_subprocess_exec", new_callable=AsyncMock) as create_proc,
        pytest.raises(RuntimeError, match="already in use by another Appium listener"),
    ):
        await manager.start(
            connection_target="device-port-conflict",
            port=4723,
            grid_url="http://grid:4444",
            **PACK_START_KWARGS,
        )

    create_proc.assert_not_awaited()


async def test_start_rejects_port_outside_configured_range_before_localhost_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = AppiumProcessManager()
    monkeypatch.setattr("agent_app.appium.process.agent_settings.runtime.appium_port_range_start", 4723)
    monkeypatch.setattr("agent_app.appium.process.agent_settings.runtime.appium_port_range_end", 4823)

    with (
        patch.object(manager, "_can_connect_to_appium", new_callable=AsyncMock) as can_connect,
        patch("agent_app.appium.process.asyncio.create_subprocess_exec", new_callable=AsyncMock) as create_proc,
        pytest.raises(InvalidStartPayloadError, match="outside configured Appium port range"),
    ):
        await manager.start(
            connection_target="device-out-of-range-port",
            port=6553,
            grid_url="http://grid:4444",
            **PACK_START_KWARGS,
        )

    can_connect.assert_not_awaited()
    create_proc.assert_not_awaited()


async def test_unexpected_exit_triggers_auto_restart() -> None:
    manager = AppiumProcessManager()
    first_proc = FakeProcess(pid=1111)
    restarted_proc = FakeProcess(pid=2222)
    real_sleep = asyncio.sleep
    delays: list[float] = []

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)
        await real_sleep(0)

    with (
        patch("agent_app.appium.process.resolve_appium_invocation_for_pack", return_value=_STUB_INVOCATION),
        patch("agent_app.appium.process._build_env", return_value={"PATH": "/usr/bin"}),
        patch("agent_app.appium.process.os.path.isfile", return_value=False),
        patch.object(manager, "_wait_for_readiness", new_callable=AsyncMock, side_effect=[True, True]),
        patch(
            "agent_app.appium.process.asyncio.create_subprocess_exec",
            side_effect=[first_proc, restarted_proc],
        ),
        patch("agent_app.appium.process.asyncio.sleep", side_effect=fake_sleep),
    ):
        await manager.start(
            connection_target="device-100",
            port=4723,
            grid_url="http://grid:4444",
            **PACK_START_KWARGS,
        )
        first_proc.set_exit(1)
        await real_sleep(0)
        await real_sleep(0)
        await real_sleep(0)

    assert [info.pid for info in manager.list_running()] == [2222]
    assert delays[0] == 1
    snapshot = manager.process_snapshot()
    assert [event["kind"] for event in snapshot["recent_restart_events"]] == [
        "crash_detected",
        "restart_succeeded",
    ]
    assert snapshot["recent_restart_events"][0]["will_retry"] is True
    await manager.shutdown()


async def test_auto_restart_cap_stops_retrying_after_threshold() -> None:
    manager = AppiumProcessManager()
    current_time = asyncio.get_running_loop().time()
    manager._launch_specs[4723] = AppiumLaunchSpec(
        connection_target="device-loop",
        port=4723,
        plugins=None,
        extra_caps=None,
        stereotype_caps=None,
        session_override=True,
        device_type=None,
        ip_address=None,
        manage_grid_node=True,
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
    )
    manager._info[4723] = AppiumProcessInfo(
        port=4723,
        pid=9999,
        connection_target="device-loop",
        platform_id="android_mobile",
    )
    manager._appium_restart_attempts[4723] = deque(current_time - offset for offset in (1, 2, 3, 4, 5))

    await manager._auto_restart_appium(4723, exit_code=9)

    snapshot = manager.process_snapshot()
    assert [event["kind"] for event in snapshot["recent_restart_events"]] == [
        "crash_detected",
        "restart_exhausted",
    ]
    assert [event["process"] for event in snapshot["recent_restart_events"]] == ["appium", "appium"]
    assert snapshot["recent_restart_events"][0]["will_retry"] is False


async def test_auto_restart_drops_managed_state_when_port_is_taken_by_unmanaged_listener() -> None:
    manager = AppiumProcessManager()
    old_appium_proc = FakeProcess(pid=1111, returncode=1)
    handle = RecordingGridNodeHandle()
    manager._appium_procs[4723] = cast("asyncio.subprocess.Process", old_appium_proc)
    manager._grid_supervisors[4723] = handle
    manager._launch_specs[4723] = AppiumLaunchSpec(
        connection_target="device-conflict",
        port=4723,
        plugins=None,
        extra_caps=None,
        stereotype_caps=None,
        session_override=True,
        device_type=None,
        ip_address=None,
        manage_grid_node=True,
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
    )
    manager._info[4723] = AppiumProcessInfo(
        port=4723,
        pid=1111,
        connection_target="device-conflict",
        platform_id="android_mobile",
    )

    real_sleep = asyncio.sleep

    async def fake_sleep(_delay: float) -> None:
        await real_sleep(0)

    with (
        patch("agent_app.appium.process.resolve_appium_invocation_for_pack", return_value=_STUB_INVOCATION),
        patch("agent_app.appium.process._build_env", return_value={"PATH": "/usr/bin"}),
        patch.object(manager, "_can_connect_to_appium", new_callable=AsyncMock, return_value=True),
        patch("agent_app.appium.process.asyncio.create_subprocess_exec", new_callable=AsyncMock) as create_proc,
        patch("agent_app.appium.process.asyncio.sleep", side_effect=fake_sleep),
    ):
        await manager._auto_restart_appium(4723, exit_code=1)

    create_proc.assert_not_awaited()
    assert manager.list_running() == []
    assert 4723 not in manager._launch_specs
    assert 4723 not in manager._info
    assert 4723 not in manager._grid_supervisors
    assert handle.stop_called is True
    snapshot = manager.process_snapshot()
    assert [event["kind"] for event in snapshot["recent_restart_events"]] == [
        "crash_detected",
        "port_conflict",
    ]
    assert snapshot["recent_restart_events"][-1]["process"] == "appium"


async def test_successful_restart_resets_backoff_step_for_next_crash() -> None:
    manager = AppiumProcessManager()
    first_proc = FakeProcess(pid=1001)
    second_proc = FakeProcess(pid=1002)
    third_proc = FakeProcess(pid=1003)
    real_sleep = asyncio.sleep
    delays: list[float] = []

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)
        await real_sleep(0)

    with (
        patch("agent_app.appium.process.resolve_appium_invocation_for_pack", return_value=_STUB_INVOCATION),
        patch("agent_app.appium.process._build_env", return_value={"PATH": "/usr/bin"}),
        patch("agent_app.appium.process.os.path.isfile", return_value=False),
        patch.object(manager, "_wait_for_readiness", new_callable=AsyncMock, side_effect=[True, True, True]),
        patch(
            "agent_app.appium.process.asyncio.create_subprocess_exec",
            side_effect=[first_proc, second_proc, third_proc],
        ),
        patch("agent_app.appium.process.asyncio.sleep", side_effect=fake_sleep),
    ):
        await manager.start(
            connection_target="device-backoff",
            port=4724,
            grid_url="http://grid:4444",
            **PACK_START_KWARGS,
        )

        first_proc.set_exit(1)
        await real_sleep(0)
        await real_sleep(0)
        await real_sleep(0)

        second_proc.set_exit(2)
        await real_sleep(0)
        await real_sleep(0)
        await real_sleep(0)

    assert delays[:2] == [1, 1]
    await manager.shutdown()


async def test_appium_restart_does_not_create_duplicate_recovery_loop() -> None:
    manager = AppiumProcessManager()
    first_appium_proc = FakeProcess(pid=1001)
    restarted_appium_proc = FakeProcess(pid=1002)
    real_sleep = asyncio.sleep

    async def fake_sleep(delay: float) -> None:
        await real_sleep(0)

    with (
        patch("agent_app.appium.process.resolve_appium_invocation_for_pack", return_value=_STUB_INVOCATION),
        patch("agent_app.appium.process._build_env", return_value={"PATH": "/usr/bin"}),
        patch.object(manager, "_wait_for_readiness", new_callable=AsyncMock, side_effect=[True, True]),
        patch(
            "agent_app.appium.process.asyncio.create_subprocess_exec",
            side_effect=[first_appium_proc, restarted_appium_proc],
        ),
        patch("agent_app.appium.process.asyncio.sleep", side_effect=fake_sleep),
    ):
        await manager.start(
            connection_target="device-shared-crash",
            port=4729,
            grid_url="http://grid:4444",
            **PACK_START_KWARGS,
        )
        first_appium_proc.set_exit(1)
        await real_sleep(0)
        await real_sleep(0)
        await real_sleep(0)
        await real_sleep(0)

    snapshot = manager.process_snapshot()
    assert [event["process"] for event in snapshot["recent_restart_events"]] == [
        "appium",
        "appium",
    ]
    assert [event["kind"] for event in snapshot["recent_restart_events"]] == [
        "crash_detected",
        "restart_succeeded",
    ]
    assert [info.pid for info in manager.list_running()] == [1002]
    await manager.shutdown()


async def test_start_appium_server_does_not_synthesize_wda_url_inline() -> None:
    """Agent must NOT inject appium:wdaBaseUrl for tvOS real devices.

    The backend already provides it via extra_caps (through device_config["appium_caps"]).
    Synthesizing it inline in the agent is the old behaviour that was removed.
    """
    manager = AppiumProcessManager()
    appium_proc = FakeProcess(pid=3001)

    with (
        patch("agent_app.appium.process.resolve_appium_invocation_for_pack", return_value=_STUB_INVOCATION),
        patch("agent_app.appium.process._build_env", return_value={"PATH": "/usr/bin"}),
        patch("agent_app.appium.process.os.path.isfile", return_value=False),
        patch.object(manager, "_wait_for_readiness", new_callable=AsyncMock, return_value=True),
        patch(
            "agent_app.appium.process.asyncio.create_subprocess_exec",
            return_value=appium_proc,
        ) as create_proc,
    ):
        await manager.start(
            connection_target="00008301-ABCDEF",
            port=4731,
            grid_url="http://grid:4444",
            pack_id="appium-xcuitest",
            platform_id="tvos",
            device_type="real_device",
            ip_address="10.0.0.42",
            extra_caps=None,
        )

    args = create_proc.await_args_list[0].args
    caps = json.loads(args[args.index("--default-capabilities") + 1])
    assert "appium:wdaBaseUrl" not in caps
    await manager.shutdown()


@pytest.mark.asyncio
async def test_start_rejects_duplicate_connection_target_on_different_port() -> None:
    manager = AppiumProcessManager()
    first_appium_proc = FakeProcess(pid=3101)
    second_appium_proc = FakeProcess(pid=3102)

    with (
        patch("agent_app.appium.process.resolve_appium_invocation_for_pack", return_value=_STUB_INVOCATION),
        patch("agent_app.appium.process._build_env", return_value={"PATH": "/usr/bin"}),
        patch("agent_app.appium.process.os.path.isfile", return_value=False),
        patch.object(manager, "_wait_for_readiness", new_callable=AsyncMock, return_value=True),
        patch(
            "agent_app.appium.process.asyncio.create_subprocess_exec",
            side_effect=[first_appium_proc, second_appium_proc],
        ) as create_proc,
    ):
        await manager.start(
            connection_target="192.168.1.254:5555",
            port=4732,
            grid_url="http://grid:4444",
            **PACK_START_KWARGS,
            manage_grid_node=False,
        )
        with pytest.raises(RuntimeError, match="already running for target"):
            await manager.start(
                connection_target="192.168.1.254:5555",
                port=4733,
                grid_url="http://grid:4444",
                **PACK_START_KWARGS,
                manage_grid_node=False,
            )

    assert create_proc.await_count == 1
    assert [info.port for info in manager.list_running()] == [4732]
    await manager.shutdown()


@pytest.mark.asyncio
async def test_start_passes_insecure_features_to_appium_command() -> None:
    """insecure_features list is forwarded to Appium via --allow-insecure."""
    manager = AppiumProcessManager()
    appium_proc = FakeProcess(pid=4001)

    with (
        patch("agent_app.appium.process.resolve_appium_invocation_for_pack", return_value=_STUB_INVOCATION),
        patch("agent_app.appium.process._build_env", return_value={"PATH": "/usr/bin"}),
        patch("agent_app.appium.process.os.path.isfile", return_value=False),
        patch.object(manager, "_wait_for_readiness", new_callable=AsyncMock, return_value=True),
        patch(
            "agent_app.appium.process.asyncio.create_subprocess_exec",
            return_value=appium_proc,
        ) as create_proc,
    ):
        await manager.start(
            connection_target="device-insecure",
            port=4740,
            grid_url="http://grid:4444",
            **PACK_START_KWARGS,
            manage_grid_node=False,
            insecure_features=["uiautomator2:chromedriver_autodownload"],
        )

    args = create_proc.await_args_list[0].args
    assert "--allow-insecure" in args
    idx = args.index("--allow-insecure")
    assert args[idx + 1] == "uiautomator2:chromedriver_autodownload"
    await manager.shutdown()


class _LifecycleAdapter:
    pack_id = "appium-uiautomator2"
    pack_release = "2026.04.0"

    def __init__(self, result: LifecycleActionResult) -> None:
        self.result = result
        self.calls: list[tuple[str, dict[str, object], str]] = []

    async def lifecycle_action(
        self,
        action_id: str,
        args: dict[str, object],
        ctx: object,
    ) -> LifecycleActionResult:
        ctx_any = cast("Any", ctx)
        self.calls.append((action_id, args, str(ctx_any.device_identity_value)))
        return self.result

    async def pre_session(self, spec: object) -> dict[str, object]:
        return {}


async def test_start_uses_adapter_lifecycle_when_manifest_lifecycle_data_provided() -> None:
    """Virtual-device boot is delegated to the loaded adapter."""
    manager = AppiumProcessManager()
    appium_proc = FakeProcess(pid=5001)
    lifecycle_actions = [{"id": "boot", "timeout_sec": 120}]
    adapter = _LifecycleAdapter(LifecycleActionResult(ok=True, state="emulator-5554"))
    adapter_registry = AdapterRegistry()
    adapter_registry.set("appium-uiautomator2", "2026.04.0", adapter)  # type: ignore[arg-type]
    manager.set_adapter_registry(adapter_registry)

    with (
        patch("agent_app.appium.process.resolve_appium_invocation_for_pack", return_value=_STUB_INVOCATION),
        patch("agent_app.appium.process._build_env", return_value={"PATH": "/usr/bin"}),
        patch("agent_app.appium.process.os.path.isfile", return_value=False),
        patch.object(manager, "_wait_for_readiness", new_callable=AsyncMock, return_value=True),
        patch(
            "agent_app.appium.process.asyncio.create_subprocess_exec",
            return_value=appium_proc,
        ) as create_proc,
    ):
        info = await manager.start(
            connection_target="Pixel_8_API_35",
            port=4750,
            grid_url="http://grid:4444",
            device_type="emulator",
            pack_id="appium-uiautomator2",
            platform_id="android_mobile",
            lifecycle_actions=lifecycle_actions,
        )

    assert adapter.calls == [("boot", {"headless": True}, "Pixel_8_API_35")]
    assert info.connection_target == "emulator-5554"
    args = create_proc.await_args_list[0].args
    caps = json.loads(args[args.index("--default-capabilities") + 1])
    assert caps["appium:udid"] == "emulator-5554"
    await manager.shutdown()


async def test_start_uses_adapter_for_simulator_boot() -> None:
    """Adapter lifecycle_action is called for simulator boot."""
    manager = AppiumProcessManager()
    appium_proc = FakeProcess(pid=5002)
    lifecycle_actions = [{"id": "boot"}]
    adapter = _LifecycleAdapter(LifecycleActionResult(ok=True, state="SIM-UUID"))
    adapter_registry = AdapterRegistry()
    adapter_registry.set("appium-xcuitest", "2026.04.0", adapter)  # type: ignore[arg-type]
    manager.set_adapter_registry(adapter_registry)

    with (
        patch("agent_app.appium.process.resolve_appium_invocation_for_pack", return_value=_STUB_INVOCATION),
        patch("agent_app.appium.process._build_env", return_value={"PATH": "/usr/bin"}),
        patch("agent_app.appium.process.os.path.isfile", return_value=False),
        patch.object(manager, "_wait_for_readiness", new_callable=AsyncMock, return_value=True),
        patch(
            "agent_app.appium.process.asyncio.create_subprocess_exec",
            return_value=appium_proc,
        ),
    ):
        info = await manager.start(
            connection_target="SIM-UUID",
            port=4751,
            grid_url="http://grid:4444",
            device_type="simulator",
            pack_id="appium-xcuitest",
            platform_id="ios",
            lifecycle_actions=lifecycle_actions,
        )

    assert adapter.calls == [("boot", {"headless": True}, "SIM-UUID")]
    assert info.connection_target == "SIM-UUID"
    await manager.shutdown()


async def test_start_raises_when_adapter_boot_fails() -> None:
    """When adapter lifecycle_action returns ok=False, start() raises RuntimeError."""
    manager = AppiumProcessManager()
    lifecycle_actions = [{"id": "boot"}]
    adapter = _LifecycleAdapter(LifecycleActionResult(ok=False, detail="AVD 'Bad_AVD' could not be started"))
    adapter_registry = AdapterRegistry()
    adapter_registry.set("appium-uiautomator2", "2026.04.0", adapter)  # type: ignore[arg-type]
    manager.set_adapter_registry(adapter_registry)

    with (
        pytest.raises(RuntimeError, match="could not be started"),
    ):
        await manager.start(
            connection_target="Bad_AVD",
            port=4752,
            grid_url="http://grid:4444",
            device_type="emulator",
            pack_id="appium-uiautomator2",
            platform_id="android_mobile",
            lifecycle_actions=lifecycle_actions,
        )


@pytest.mark.asyncio
async def test_start_omits_allow_insecure_when_insecure_features_empty() -> None:
    """When insecure_features is empty, --allow-insecure must NOT appear in the command."""
    manager = AppiumProcessManager()
    appium_proc = FakeProcess(pid=4002)

    with (
        patch("agent_app.appium.process.resolve_appium_invocation_for_pack", return_value=_STUB_INVOCATION),
        patch("agent_app.appium.process._build_env", return_value={"PATH": "/usr/bin"}),
        patch("agent_app.appium.process.os.path.isfile", return_value=False),
        patch.object(manager, "_wait_for_readiness", new_callable=AsyncMock, return_value=True),
        patch(
            "agent_app.appium.process.asyncio.create_subprocess_exec",
            return_value=appium_proc,
        ) as create_proc,
    ):
        await manager.start(
            connection_target="device-no-insecure",
            port=4741,
            grid_url="http://grid:4444",
            **PACK_START_KWARGS,
            manage_grid_node=False,
            insecure_features=[],
        )

    args = create_proc.await_args_list[0].args
    assert "--allow-insecure" not in args
    await manager.shutdown()


@pytest.mark.asyncio
async def test_status_does_not_probe_unmanaged_localhost_port(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = AppiumProcessManager()

    async def fail_if_called(port: int) -> dict[str, Any] | None:
        raise AssertionError(f"unexpected localhost probe for unmanaged port {port}")

    monkeypatch.setattr(manager, "_fetch_appium_status", fail_if_called)

    assert await manager.status(6553) == {"running": False, "port": 6553}


@pytest.mark.asyncio
async def test_require_managed_running_port_rejects_unmanaged_port() -> None:
    manager = AppiumProcessManager()

    with pytest.raises(DeviceNotFoundError, match="No managed Appium process is running on port 6553"):
        manager.require_managed_running_port(6553)


# ---------------------------------------------------------------------------
# Lowest-hanging-fruit coverage for helper functions / simple branches
# ---------------------------------------------------------------------------


def test_has_lifecycle_action_true_and_false() -> None:
    assert _has_lifecycle_action([{"id": "boot"}, {"id": "reboot"}], "boot") is True
    assert _has_lifecycle_action([{"id": "boot"}], "reboot") is False
    assert _has_lifecycle_action([], "boot") is False


def test_sanitize_appium_driver_capabilities_drops_gridfleet_and_known_keys() -> None:
    raw = {
        "appium:automationName": "UiAutomator2",
        "gridfleet:run_id": "abc",
        "appium:gridfleet:deviceId": "123",
        "appium:deviceName": "Pixel",
        "platformName": "Android",
        "custom": "keep",
    }
    result = sanitize_appium_driver_capabilities(raw)
    assert result == {"appium:automationName": "UiAutomator2", "platformName": "Android", "custom": "keep"}


def test_exception_classes_exist_and_are_runtime_error() -> None:
    excs = [RuntimeNotInstalledError, PortOccupiedError, AlreadyRunningError, StartupTimeoutError, RuntimeMissingError]
    for exc_cls in excs:
        with pytest.raises(RuntimeError):
            raise exc_cls("test")


def test_appium_invocation_dataclass_defaults() -> None:
    inv = AppiumInvocation(binary="/bin/appium")
    assert inv.env_extra == {}


def test_find_java_fallback_to_java_command() -> None:
    with (
        patch("agent_app.appium.process.shutil.which", return_value=None),
        patch.dict("os.environ", {}, clear=True),
        patch("agent_app.appium.process.platform.system", return_value="Linux"),
        patch("agent_app.appium.process.os.path.isdir", return_value=False),
        patch("agent_app.appium.process.os.path.isfile", return_value=False),
    ):
        assert _find_java() == "java"


def test_find_java_darwin_usr_bin_java_stub_no_javahome() -> None:
    """Cover Darwin branch where /usr/bin/java is a stub and /usr/libexec/java_home fails."""
    with (
        patch("agent_app.appium.process.platform.system", return_value="Darwin"),
        patch("agent_app.appium.process.shutil.which", return_value="/usr/bin/java"),
        patch("agent_app.appium.process.os.path.realpath", return_value="/usr/bin/java"),
        patch.dict("os.environ", {}, clear=True),
        patch("agent_app.appium.process.subprocess.run", side_effect=FileNotFoundError),
        patch("agent_app.appium.process.os.path.isdir", return_value=False),
        patch("agent_app.appium.process.os.path.isfile", return_value=False),
    ):
        assert _find_java() == "java"


def test_find_java_darwin_usr_bin_java_stub_with_javahome() -> None:
    """Cover Darwin branch where /usr/libexec/java_home succeeds."""
    result = MagicMock(returncode=0, stdout="/Library/Java/Home\n")
    with (
        patch("agent_app.appium.process.platform.system", return_value="Darwin"),
        patch("agent_app.appium.process.shutil.which", return_value="/usr/bin/java"),
        patch("agent_app.appium.process.os.path.realpath", return_value="/usr/bin/java"),
        patch.dict("os.environ", {}, clear=True),
        patch("agent_app.appium.process.subprocess.run", return_value=result),
        patch("agent_app.appium.process.os.path.isfile", return_value=True),
        patch("agent_app.appium.process.os.access", return_value=True),
        patch("agent_app.appium.process.os.path.isdir", return_value=False),
    ):
        assert _find_java() == "/Library/Java/Home/bin/java"


def test_appium_process_info_defaults() -> None:
    info = AppiumProcessInfo(port=4723, pid=1234, connection_target="dev", platform_id="android")
    assert info.port == 4723


def test_running_info_for_target_excludes_port() -> None:
    manager = AppiumProcessManager()
    manager._info[4723] = AppiumProcessInfo(port=4723, pid=1, connection_target="dev", platform_id="android")
    manager._info[4724] = AppiumProcessInfo(port=4724, pid=2, connection_target="dev", platform_id="android")
    manager._appium_procs[4723] = cast("asyncio.subprocess.Process", FakeProcess(pid=1))
    manager._appium_procs[4724] = cast("asyncio.subprocess.Process", FakeProcess(pid=2))

    # exclude 4723
    assert (
        manager._running_info_for_target(connection_target="dev", platform_id="android", exclude_port=4723).port == 4724
    )
    # exclude 4724
    assert (
        manager._running_info_for_target(connection_target="dev", platform_id="android", exclude_port=4724).port == 4723
    )


def test_running_info_for_target_no_match() -> None:
    manager = AppiumProcessManager()
    assert manager._running_info_for_target(connection_target="x", platform_id="y") is None


def test_trim_restart_attempts_with_explicit_now() -> None:
    manager = AppiumProcessManager()
    attempts: dict[int, collections.deque[float]] = {}
    now = 1000.0
    attempts[4723] = collections.deque([now - 400, now - 10])
    history = manager._trim_restart_attempts(attempts, 4723, now=now)
    # Only entries within 300s window kept
    assert list(history) == [now - 10]


def test_next_restart_delay_bounds() -> None:
    manager = AppiumProcessManager()
    steps: dict[int, int] = {}
    assert manager._next_restart_delay(steps, 4723) == 1
    steps[4723] = 99
    assert manager._next_restart_delay(steps, 4723) == 30


def test_advance_restart_backoff_cap() -> None:
    manager = AppiumProcessManager()
    steps: dict[int, int] = {4723: 99}
    manager._advance_restart_backoff(steps, 4723)
    assert steps[4723] == len((1, 2, 4, 8, 16, 30)) - 1


async def test_watch_appium_process_ignores_if_not_current_process() -> None:
    manager = AppiumProcessManager()
    proc = FakeProcess(pid=100)
    manager._appium_procs[4723] = cast("asyncio.subprocess.Process", FakeProcess(pid=101))
    # Since _appium_procs[4723] is a different process object, watch exits early on line 503
    proc.set_exit(0)
    await manager._watch_appium_process(4723, cast("asyncio.subprocess.Process", proc))


async def test_watch_appium_process_ignores_intentional_stop() -> None:
    manager = AppiumProcessManager()
    proc = FakeProcess(pid=100)
    manager._appium_procs[4723] = cast("asyncio.subprocess.Process", proc)
    manager._intentional_stop_ports.add(4723)
    # Simulate process exit
    proc.set_exit(0)
    await manager._watch_appium_process(4723, cast("asyncio.subprocess.Process", proc))
    assert 4723 not in manager._appium_restart_tasks


async def test_watch_appium_process_skips_restart_if_already_restarting() -> None:
    manager = AppiumProcessManager()
    proc = FakeProcess(pid=100)
    manager._appium_procs[4723] = cast("asyncio.subprocess.Process", proc)
    existing_restart = asyncio.create_task(asyncio.sleep(999))
    manager._appium_restart_tasks[4723] = existing_restart
    proc.set_exit(1)
    await manager._watch_appium_process(4723, cast("asyncio.subprocess.Process", proc))
    # Should not have replaced the task
    assert manager._appium_restart_tasks[4723] is existing_restart
    existing_restart.cancel()


async def test_auto_restart_returns_when_intentional_stop_during_sleep() -> None:
    manager = AppiumProcessManager()
    manager._launch_specs[4723] = AppiumLaunchSpec(
        connection_target="dev",
        port=4723,
        plugins=None,
        extra_caps=None,
        stereotype_caps=None,
        session_override=True,
        device_type=None,
        ip_address=None,
        manage_grid_node=False,
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
    )
    manager._intentional_stop_ports.add(4723)
    await manager._auto_restart_appium(4723, exit_code=1)
    assert manager.process_snapshot()["recent_restart_events"] == []


async def test_auto_restart_returns_when_no_launch_spec() -> None:
    manager = AppiumProcessManager()
    # No launch spec present; post-sleep return on line 574
    real_sleep = asyncio.sleep

    async def fake_sleep(delay: float) -> None:
        await real_sleep(0)

    with patch("agent_app.appium.process.asyncio.sleep", side_effect=fake_sleep):
        await manager._auto_restart_appium(4723, exit_code=1)
    # Should record crash_detected, then return after sleep because launch spec gone
    events = [e["kind"] for e in manager.process_snapshot()["recent_restart_events"]]
    assert events == ["crash_detected"]


async def test_auto_restart_records_port_conflict_and_drops() -> None:
    manager = AppiumProcessManager()
    manager._launch_specs[4723] = AppiumLaunchSpec(
        connection_target="dev",
        port=4723,
        plugins=None,
        extra_caps=None,
        stereotype_caps=None,
        session_override=True,
        device_type=None,
        ip_address=None,
        manage_grid_node=False,
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
    )
    manager._info[4723] = AppiumProcessInfo(port=4723, pid=100, connection_target="dev", platform_id="android")

    async def raise_port_occupied(*_args: object, **_kwargs: object) -> AppiumProcessInfo:
        raise PortOccupiedError("taken")

    with (
        patch("agent_app.appium.process.asyncio.sleep", new_callable=AsyncMock),
        patch.object(manager, "_restart_from_launch_spec", side_effect=raise_port_occupied),
    ):
        await manager._auto_restart_appium(4723, exit_code=1)

    events = [e["kind"] for e in manager.process_snapshot()["recent_restart_events"]]
    assert events == ["crash_detected", "port_conflict"]
    assert 4723 not in manager._info


async def test_auto_restart_advances_backoff_on_generic_failure() -> None:
    manager = AppiumProcessManager()
    manager._launch_specs[4723] = AppiumLaunchSpec(
        connection_target="dev",
        port=4723,
        plugins=None,
        extra_caps=None,
        stereotype_caps=None,
        session_override=True,
        device_type=None,
        ip_address=None,
        manage_grid_node=False,
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
    )
    manager._info[4723] = AppiumProcessInfo(port=4723, pid=100, connection_target="dev", platform_id="android")

    real_sleep = asyncio.sleep

    async def fake_sleep(delay: float) -> None:
        await real_sleep(0)

    call_count = 0

    async def fail_once_then_cancel(*_args: object, **_kwargs: object) -> AppiumProcessInfo:
        nonlocal call_count
        call_count += 1
        if call_count >= 2:
            # cancel ourselves after a couple of iterations to avoid infinite loop
            for task in asyncio.all_tasks():
                if task.get_name() == "auto_restart_4723":
                    task.cancel()
            await real_sleep(0)
        raise RuntimeError("boom")

    with (
        patch("agent_app.appium.process.asyncio.sleep", side_effect=fake_sleep),
        patch.object(manager, "_restart_from_launch_spec", side_effect=fail_once_then_cancel),
    ):
        task = asyncio.create_task(manager._auto_restart_appium(4723, exit_code=1), name="auto_restart_4723")
        with contextlib.suppress(asyncio.CancelledError):
            await task

    # Backoff step should have advanced once
    assert manager._appium_restart_backoff_steps.get(4723, 0) >= 1


async def test_restart_from_launch_spec_raises_when_spec_missing() -> None:
    manager = AppiumProcessManager()
    with pytest.raises(RuntimeError, match="No launch spec found for port 9999"):
        await manager._restart_from_launch_spec(9999)


async def test_start_appium_server_raises_runtime_missing_when_binary_not_found() -> None:
    manager = AppiumProcessManager()
    spec = AppiumLaunchSpec(
        connection_target="dev",
        port=4723,
        plugins=None,
        extra_caps=None,
        stereotype_caps=None,
        session_override=True,
        device_type=None,
        ip_address=None,
        manage_grid_node=False,
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
    )

    with (
        patch("agent_app.appium.process.resolve_appium_invocation_for_pack", return_value=_STUB_INVOCATION),
        patch("agent_app.appium.process._build_env", return_value={"PATH": "/usr/bin"}),
        patch.object(manager, "_can_connect_to_appium", new_callable=AsyncMock, return_value=False),
        patch(
            "agent_app.appium.process.asyncio.create_subprocess_exec",
            side_effect=FileNotFoundError("appium"),
        ),
        pytest.raises(RuntimeMissingError, match="appium executable not found"),
    ):
        await manager._start_appium_server(spec, clear_logs_on_failure=True)


async def test_start_appium_server_clears_logs_when_clear_logs_on_failure_true() -> None:
    manager = AppiumProcessManager()
    spec = AppiumLaunchSpec(
        connection_target="dev",
        port=4723,
        plugins=None,
        extra_caps=None,
        stereotype_caps=None,
        session_override=True,
        device_type=None,
        ip_address=None,
        manage_grid_node=False,
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
    )
    # Pre-seed logs so we can assert they are cleared on failure
    manager._logs[4723] = collections.deque(["old log"], maxlen=10)

    proc = FakeProcess(pid=1234, stdout=_stream_with_lines("booting"))

    with (
        patch("agent_app.appium.process.resolve_appium_invocation_for_pack", return_value=_STUB_INVOCATION),
        patch("agent_app.appium.process._build_env", return_value={"PATH": "/usr/bin"}),
        patch.object(manager, "_can_connect_to_appium", new_callable=AsyncMock, return_value=False),
        patch.object(manager, "_wait_for_readiness", new_callable=AsyncMock, return_value=False),
        patch("agent_app.appium.process.asyncio.create_subprocess_exec", return_value=proc),
        patch("agent_app.appium.process.asyncio.sleep", new_callable=AsyncMock),
        pytest.raises(StartupTimeoutError),
    ):
        await manager._start_appium_server(spec, clear_logs_on_failure=True)

    assert 4723 not in manager._logs


async def test_reconfigure_unknown_grid_supervisor_raises() -> None:
    manager = AppiumProcessManager()
    manager._info[4723] = AppiumProcessInfo(port=4723, pid=1, connection_target="dev", platform_id="android")
    with pytest.raises(DeviceNotFoundError, match="No running grid node for Appium port 4723"):
        await manager.reconfigure(4723, accepting_new_sessions=True, stop_pending=False, grid_run_id=None)


async def test_reconfigure_stop_pending_with_active_session_spawns_stop_task() -> None:
    manager = AppiumProcessManager()
    service = ReconfigurableGridNodeService(busy=True)
    handle = ReconfigurableGridNodeHandle(service)
    manager._grid_supervisors[4723] = cast("Any", handle)
    manager._info[4723] = AppiumProcessInfo(port=4723, pid=1, connection_target="dev", platform_id="android")
    manager._launch_specs[4723] = AppiumLaunchSpec(
        connection_target="dev",
        port=4723,
        plugins=None,
        extra_caps=None,
        stereotype_caps=None,
        session_override=True,
        device_type=None,
        ip_address=None,
        manage_grid_node=False,
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
    )

    await manager.reconfigure(4723, accepting_new_sessions=False, stop_pending=True, grid_run_id=None)
    assert 4723 in manager._stop_pending_ports
    assert 4723 in manager._stop_pending_tasks
    # clean up
    manager._stop_pending_tasks[4723].cancel()


async def test_ensure_stop_when_grid_idle_task_skips_if_already_running() -> None:
    manager = AppiumProcessManager()
    existing = asyncio.create_task(asyncio.sleep(999))
    manager._stop_pending_tasks[4723] = existing
    manager._ensure_stop_when_grid_idle_task(4723)
    assert manager._stop_pending_tasks[4723] is existing
    existing.cancel()


async def test_ensure_stop_when_grid_idle_task_creates_task() -> None:
    manager = AppiumProcessManager()
    manager._ensure_stop_when_grid_idle_task(4723)
    assert 4723 in manager._stop_pending_tasks
    manager._stop_pending_tasks[4723].cancel()


async def test_stop_when_grid_idle_stops_when_no_session() -> None:
    manager = AppiumProcessManager()
    service = ReconfigurableGridNodeService(busy=False)
    handle = ReconfigurableGridNodeHandle(service)
    manager._grid_supervisors[4723] = cast("Any", handle)
    manager._appium_procs[4723] = cast("asyncio.subprocess.Process", FakeProcess(pid=1))
    manager._info[4723] = AppiumProcessInfo(port=4723, pid=1, connection_target="dev", platform_id="android")
    manager._logs[4723] = collections.deque(["line"], maxlen=10)
    manager._log_tasks[4723] = []
    manager._stop_pending_ports.add(4723)
    manager._launch_specs[4723] = AppiumLaunchSpec(
        connection_target="dev",
        port=4723,
        plugins=None,
        extra_caps=None,
        stereotype_caps=None,
        session_override=True,
        device_type=None,
        ip_address=None,
        manage_grid_node=False,
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
    )

    await manager._stop_when_grid_idle(4723)
    assert 4723 not in manager._appium_procs


async def test_stop_when_grid_idle_returns_if_not_pending() -> None:
    manager = AppiumProcessManager()
    await manager._stop_when_grid_idle(4723)


async def test_stop_when_grid_idle_loops_if_session_still_active() -> None:
    manager = AppiumProcessManager()
    service = ReconfigurableGridNodeService(busy=True)
    handle = ReconfigurableGridNodeHandle(service)
    manager._grid_supervisors[4723] = cast("Any", handle)
    manager._stop_pending_ports.add(4723)
    real_sleep = asyncio.sleep

    calls = 0

    async def fake_sleep(delay: float) -> None:
        nonlocal calls
        calls += 1
        if calls >= 2:
            manager._stop_pending_ports.discard(4723)
        await real_sleep(0)

    with patch("agent_app.appium.process.asyncio.sleep", side_effect=fake_sleep):
        await manager._stop_when_grid_idle(4723)

    assert calls >= 2


async def test_cleanup_started_appium_logs_and_suppresses_grid_stop_failure() -> None:
    manager = AppiumProcessManager()
    proc = FakeProcess(pid=1)
    manager._appium_procs[4723] = cast("asyncio.subprocess.Process", proc)
    manager._info[4723] = AppiumProcessInfo(port=4723, pid=1, connection_target="dev", platform_id="android")
    manager._launch_specs[4723] = AppiumLaunchSpec(
        connection_target="dev",
        port=4723,
        plugins=None,
        extra_caps=None,
        stereotype_caps=None,
        session_override=True,
        device_type=None,
        ip_address=None,
        manage_grid_node=False,
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
    )
    manager._logs[4723] = collections.deque(["log"], maxlen=10)
    manager._log_tasks[4723] = [asyncio.create_task(asyncio.sleep(999))]

    with patch.object(manager, "_stop_grid_node_service", side_effect=RuntimeError("grid boom")):
        await manager._cleanup_started_appium_after_grid_node_failure(4723, cast("asyncio.subprocess.Process", proc))

    assert 4723 not in manager._appium_procs
    assert 4723 in manager._intentional_stop_ports  # stays set per comment in source


async def test_cleanup_started_appium_kills_when_proc_still_running() -> None:
    manager = AppiumProcessManager()
    proc = FakeProcess(pid=1, returncode=None)
    manager._appium_procs[4723] = cast("asyncio.subprocess.Process", proc)
    manager._info[4723] = AppiumProcessInfo(port=4723, pid=1, connection_target="dev", platform_id="android")
    manager._launch_specs[4723] = AppiumLaunchSpec(
        connection_target="dev",
        port=4723,
        plugins=None,
        extra_caps=None,
        stereotype_caps=None,
        session_override=True,
        device_type=None,
        ip_address=None,
        manage_grid_node=False,
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
    )

    await manager._cleanup_started_appium_after_grid_node_failure(4723, cast("asyncio.subprocess.Process", proc))
    assert proc.sent_signals == [signal.SIGTERM]


async def test_cleanup_started_appium_escalates_kill_after_timeout() -> None:
    manager = AppiumProcessManager()
    proc = FakeProcess(pid=1, returncode=None)
    manager._appium_procs[4723] = cast("asyncio.subprocess.Process", proc)
    manager._info[4723] = AppiumProcessInfo(port=4723, pid=1, connection_target="dev", platform_id="android")
    manager._launch_specs[4723] = AppiumLaunchSpec(
        connection_target="dev",
        port=4723,
        plugins=None,
        extra_caps=None,
        stereotype_caps=None,
        session_override=True,
        device_type=None,
        ip_address=None,
        manage_grid_node=False,
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
    )

    async def wait_always_timeout(awaitable: object, *, timeout: float) -> object:
        del awaitable, timeout
        raise TimeoutError

    with patch("agent_app.appium.process.asyncio.wait_for", side_effect=wait_always_timeout):
        await manager._cleanup_started_appium_after_grid_node_failure(4723, cast("asyncio.subprocess.Process", proc))

    assert proc.killed is True


async def test_drop_failed_managed_port_suppresses_grid_stop_exception() -> None:
    manager = AppiumProcessManager()
    with patch.object(manager, "_stop_grid_node_service", side_effect=RuntimeError("boom")):
        await manager._drop_failed_managed_port(4723)
    assert 4723 not in manager._info


async def test_stop_pending_task_cancelled_when_stop_is_current_task() -> None:
    manager = AppiumProcessManager()
    appium_proc = FakeProcess(pid=1)
    handle = RecordingGridNodeHandle()
    manager._appium_procs[4723] = cast("asyncio.subprocess.Process", appium_proc)
    manager._grid_supervisors[4723] = handle
    manager._info[4723] = AppiumProcessInfo(port=4723, pid=1, connection_target="dev", platform_id="android")
    manager._logs[4723] = collections.deque(["line"], maxlen=10)
    manager._log_tasks[4723] = []

    # Create a fake stop_pending task whose done_callback might fire during stop
    fake_task = asyncio.create_task(asyncio.sleep(999))
    manager._stop_pending_tasks[4723] = fake_task
    await manager.stop(4723)
    # Should have been cancelled (allow event loop to process)
    await asyncio.sleep(0)
    assert fake_task.cancelled() or fake_task.done()


async def test_fetch_appium_status_http_error_returns_none() -> None:
    manager = AppiumProcessManager()
    with patch("agent_app.appium.process.httpx.AsyncClient") as client_cls:
        client = AsyncMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        client.get = AsyncMock(side_effect=httpx.ConnectError("down"))
        client_cls.return_value = client
        assert await manager._fetch_appium_status(4723) is None


async def test_fetch_appium_status_non_200_returns_none() -> None:
    manager = AppiumProcessManager()
    with patch("agent_app.appium.process.httpx.AsyncClient") as client_cls:
        client = AsyncMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        response = MagicMock()
        response.status_code = 503
        client.get = AsyncMock(return_value=response)
        client_cls.return_value = client
        assert await manager._fetch_appium_status(4723) is None


async def test_fetch_appium_status_malformed_json_returns_none() -> None:
    manager = AppiumProcessManager()
    with patch("agent_app.appium.process.httpx.AsyncClient") as client_cls:
        client = AsyncMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = ["not", "a", "dict"]
        client.get = AsyncMock(return_value=response)
        client_cls.return_value = client
        assert await manager._fetch_appium_status(4723) is None


async def test_wait_for_readiness_returns_false_when_process_exits() -> None:
    manager = AppiumProcessManager()
    proc = FakeProcess(pid=1)
    proc.set_exit(1)
    assert await manager._wait_for_readiness(4723, cast("asyncio.subprocess.Process", proc)) is False


async def test_start_appium_server_does_not_append_plugins_when_none() -> None:
    manager = AppiumProcessManager()
    spec = AppiumLaunchSpec(
        connection_target="dev",
        port=4723,
        plugins=None,
        extra_caps=None,
        stereotype_caps=None,
        session_override=False,
        device_type=None,
        ip_address=None,
        manage_grid_node=False,
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
    )
    proc = FakeProcess(pid=1234)
    with (
        patch("agent_app.appium.process.resolve_appium_invocation_for_pack", return_value=_STUB_INVOCATION),
        patch("agent_app.appium.process._build_env", return_value={"PATH": "/usr/bin"}),
        patch.object(manager, "_can_connect_to_appium", new_callable=AsyncMock, return_value=False),
        patch("agent_app.appium.process.asyncio.create_subprocess_exec", return_value=proc) as create_proc,
        patch.object(manager, "_wait_for_readiness", new_callable=AsyncMock, return_value=True),
    ):
        await manager._start_appium_server(spec, clear_logs_on_failure=False)
    args = create_proc.await_args_list[0].args
    assert "--use-plugins" not in args
    await manager.shutdown()
