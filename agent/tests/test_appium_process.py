import asyncio
import json
from collections import deque
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from agent_app.appium_process import (
    AppiumInvocation,
    AppiumLaunchSpec,
    AppiumProcessInfo,
    AppiumProcessManager,
    DeviceNotFoundError,
    InvalidStartPayloadError,
    _build_env,
    _find_java,
)
from agent_app.grid_node.config import GridNodeConfig
from agent_app.pack.adapter_registry import AdapterRegistry
from agent_app.pack.adapter_types import LifecycleActionResult
from agent_app.tool_paths import _parse_node_version

_STUB_INVOCATION = AppiumInvocation(binary="/usr/local/bin/appium")
PACK_START_KWARGS = {"pack_id": "appium-uiautomator2", "platform_id": "android_mobile"}


@pytest.fixture(autouse=True)
def stub_port_probe() -> object:
    with (
        patch(
            "agent_app.appium_process.AppiumProcessManager._can_connect_to_appium",
            new_callable=AsyncMock,
            return_value=False,
        ),
        patch("agent_app.appium_process.start_grid_node_supervisor", return_value=RecordingGridNodeHandle()),
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


def test_parse_node_version_prefers_version_tuple() -> None:
    assert _parse_node_version("/Users/me/.nvm/versions/node/v24.12.0/bin/appium") == (24, 12, 0)
    assert _parse_node_version("/usr/local/bin/appium") == (0,)


def test_build_env_adds_paths() -> None:
    with (
        patch("agent_app.appium_process._find_java", return_value="/usr/bin/java"),
        patch("agent_app.appium_process.os.path.realpath", return_value="/usr/lib/jvm/java-21/bin/java"),
        patch("agent_app.appium_process.os.path.isfile", return_value=True),
        patch("agent_app.appium_process.os.access", return_value=True),
        patch("agent_app.appium_process._find_adb", return_value="/opt/android/platform-tools/adb"),
        patch("agent_app.appium_process.find_android_home", return_value="/opt/android"),
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
        patch("agent_app.appium_process._find_java", return_value="/usr/bin/java"),
        patch("agent_app.appium_process.os.path.realpath", return_value="/usr/lib/jvm/java-21/bin/java"),
        patch("agent_app.appium_process.os.path.isfile", return_value=True),
        patch("agent_app.appium_process.os.access", return_value=True),
        patch("agent_app.appium_process._find_adb", return_value="/opt/android/platform-tools/adb"),
        patch("agent_app.appium_process.find_android_home", return_value="/opt/android"),
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
        patch("agent_app.appium_process._find_java", return_value="/usr/bin/java"),
        patch("agent_app.appium_process.os.path.realpath", return_value="/usr/lib/jvm/java-21/bin/java"),
        patch("agent_app.appium_process.os.path.isfile", return_value=True),
        patch("agent_app.appium_process.os.access", return_value=True),
        patch("agent_app.appium_process._find_adb", return_value="/opt/android/platform-tools/adb"),
        patch("agent_app.appium_process.find_android_home", return_value="/opt/android"),
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
        patch("agent_app.appium_process._find_java", return_value="java"),
        patch("agent_app.appium_process._find_adb", return_value="/opt/android/platform-tools/adb"),
        patch("agent_app.appium_process.find_android_home", return_value="/opt/android"),
        patch.dict("os.environ", {"PATH": "/usr/local/bin"}, clear=True),
    ):
        env = _build_env(appium_bin="/usr/local/bin/appium")

    assert "JAVA_HOME" not in env


def test_find_java_prefers_macos_java_home_over_usr_bin_stub() -> None:
    java_home_result = MagicMock(returncode=0, stdout="/Library/Java/JavaVirtualMachines/jdk-21.jdk/Contents/Home\n")
    with (
        patch("agent_app.appium_process.platform.system", return_value="Darwin"),
        patch("agent_app.appium_process.shutil.which", return_value="/usr/bin/java"),
        patch("agent_app.appium_process.os.path.realpath", return_value="/usr/bin/java"),
        patch("agent_app.appium_process.os.path.isdir", return_value=False),
        patch("agent_app.appium_process.os.path.isfile", return_value=True),
        patch("agent_app.appium_process.os.access", return_value=True),
        patch("agent_app.appium_process.subprocess.run", return_value=java_home_result),
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
        patch("agent_app.appium_process.resolve_appium_invocation_for_pack", return_value=_STUB_INVOCATION),
        patch("agent_app.appium_process._build_env", return_value={"PATH": "/usr/bin"}),
        patch.object(manager, "_wait_for_readiness", new_callable=AsyncMock, return_value=True) as wait_ready,
        patch(
            "agent_app.appium_process.asyncio.create_subprocess_exec",
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
        patch("agent_app.appium_process.resolve_appium_invocation_for_pack", return_value=_STUB_INVOCATION),
        patch("agent_app.appium_process._build_env", return_value={"PATH": "/usr/bin"}),
        patch.object(manager, "_wait_for_readiness", new_callable=AsyncMock, return_value=True),
        patch("agent_app.appium_process.asyncio.create_subprocess_exec", return_value=appium_proc) as create_proc,
        patch("agent_app.appium_process.start_grid_node_supervisor", side_effect=start_supervisor),
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
        patch("agent_app.appium_process.resolve_appium_invocation_for_pack", return_value=_STUB_INVOCATION),
        patch("agent_app.appium_process._build_env", return_value={"PATH": "/usr/bin"}),
        patch.object(manager, "_wait_for_readiness", new_callable=AsyncMock, return_value=True),
        patch("agent_app.appium_process.asyncio.create_subprocess_exec", return_value=appium_proc),
        patch("agent_app.appium_process.start_grid_node_supervisor", return_value=handle),
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
        patch("agent_app.appium_process.resolve_appium_invocation_for_pack", return_value=_STUB_INVOCATION),
        patch("agent_app.appium_process._build_env", return_value={"PATH": "/usr/bin"}),
        patch.object(manager, "_wait_for_readiness", new_callable=AsyncMock, return_value=True),
        patch(
            "agent_app.appium_process.asyncio.create_subprocess_exec",
            return_value=appium_proc,
        ) as create_proc,
        patch(
            "agent_app.appium_process.start_grid_node_supervisor",
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
    }
    await manager.shutdown()


async def test_start_can_disable_session_override() -> None:
    manager = AppiumProcessManager()
    appium_proc = FakeProcess(pid=1234)

    with (
        patch("agent_app.appium_process.resolve_appium_invocation_for_pack", return_value=_STUB_INVOCATION),
        patch("agent_app.appium_process._build_env", return_value={"PATH": "/usr/bin"}),
        patch("agent_app.appium_process.os.path.isfile", return_value=False),
        patch.object(manager, "_wait_for_readiness", new_callable=AsyncMock, return_value=True),
        patch("agent_app.appium_process.asyncio.create_subprocess_exec", return_value=appium_proc) as create_proc,
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
        patch("agent_app.appium_process.resolve_appium_invocation_for_pack", return_value=_STUB_INVOCATION),
        patch("agent_app.appium_process._build_env", return_value={"PATH": "/usr/bin"}),
        patch.object(manager, "_wait_for_readiness", new_callable=AsyncMock, return_value=False),
        patch("agent_app.appium_process.asyncio.create_subprocess_exec", return_value=appium_proc),
        patch("agent_app.appium_process.asyncio.sleep", new_callable=AsyncMock),
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

    with patch("agent_app.appium_process.asyncio.wait_for", side_effect=wait_for_side_effect):
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

    with patch("agent_app.appium_process.httpx.AsyncClient", return_value=client):
        running = await manager.status(4723)

    assert running["running"] is True
    assert running["pid"] == 1234
    assert running["appium_status"] == {"value": {"ready": True}}

    proc.set_exit(1)
    client.get = AsyncMock(side_effect=httpx.ConnectError("down"))
    with patch("agent_app.appium_process.httpx.AsyncClient", return_value=client):
        stopped = await manager.status(4723)

    assert stopped == {"running": False, "port": 4723}


async def test_start_fails_fast_when_port_has_unmanaged_listener() -> None:
    manager = AppiumProcessManager()
    with (
        patch("agent_app.appium_process.resolve_appium_invocation_for_pack", return_value=_STUB_INVOCATION),
        patch("agent_app.appium_process._build_env", return_value={"PATH": "/usr/bin"}),
        patch.object(manager, "_can_connect_to_appium", new_callable=AsyncMock, return_value=True),
        patch("agent_app.appium_process.asyncio.create_subprocess_exec", new_callable=AsyncMock) as create_proc,
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
    monkeypatch.setattr("agent_app.appium_process.agent_settings.appium_port_range_start", 4723)
    monkeypatch.setattr("agent_app.appium_process.agent_settings.appium_port_range_end", 4823)

    with (
        patch.object(manager, "_can_connect_to_appium", new_callable=AsyncMock) as can_connect,
        patch("agent_app.appium_process.asyncio.create_subprocess_exec", new_callable=AsyncMock) as create_proc,
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
        patch("agent_app.appium_process.resolve_appium_invocation_for_pack", return_value=_STUB_INVOCATION),
        patch("agent_app.appium_process._build_env", return_value={"PATH": "/usr/bin"}),
        patch("agent_app.appium_process.os.path.isfile", return_value=False),
        patch.object(manager, "_wait_for_readiness", new_callable=AsyncMock, side_effect=[True, True]),
        patch(
            "agent_app.appium_process.asyncio.create_subprocess_exec",
            side_effect=[first_proc, restarted_proc],
        ),
        patch("agent_app.appium_process.asyncio.sleep", side_effect=fake_sleep),
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
        patch("agent_app.appium_process.resolve_appium_invocation_for_pack", return_value=_STUB_INVOCATION),
        patch("agent_app.appium_process._build_env", return_value={"PATH": "/usr/bin"}),
        patch.object(manager, "_can_connect_to_appium", new_callable=AsyncMock, return_value=True),
        patch("agent_app.appium_process.asyncio.create_subprocess_exec", new_callable=AsyncMock) as create_proc,
        patch("agent_app.appium_process.asyncio.sleep", side_effect=fake_sleep),
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
        patch("agent_app.appium_process.resolve_appium_invocation_for_pack", return_value=_STUB_INVOCATION),
        patch("agent_app.appium_process._build_env", return_value={"PATH": "/usr/bin"}),
        patch("agent_app.appium_process.os.path.isfile", return_value=False),
        patch.object(manager, "_wait_for_readiness", new_callable=AsyncMock, side_effect=[True, True, True]),
        patch(
            "agent_app.appium_process.asyncio.create_subprocess_exec",
            side_effect=[first_proc, second_proc, third_proc],
        ),
        patch("agent_app.appium_process.asyncio.sleep", side_effect=fake_sleep),
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
        patch("agent_app.appium_process.resolve_appium_invocation_for_pack", return_value=_STUB_INVOCATION),
        patch("agent_app.appium_process._build_env", return_value={"PATH": "/usr/bin"}),
        patch.object(manager, "_wait_for_readiness", new_callable=AsyncMock, side_effect=[True, True]),
        patch(
            "agent_app.appium_process.asyncio.create_subprocess_exec",
            side_effect=[first_appium_proc, restarted_appium_proc],
        ),
        patch("agent_app.appium_process.asyncio.sleep", side_effect=fake_sleep),
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
        patch("agent_app.appium_process.resolve_appium_invocation_for_pack", return_value=_STUB_INVOCATION),
        patch("agent_app.appium_process._build_env", return_value={"PATH": "/usr/bin"}),
        patch("agent_app.appium_process.os.path.isfile", return_value=False),
        patch.object(manager, "_wait_for_readiness", new_callable=AsyncMock, return_value=True),
        patch(
            "agent_app.appium_process.asyncio.create_subprocess_exec",
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
        patch("agent_app.appium_process.resolve_appium_invocation_for_pack", return_value=_STUB_INVOCATION),
        patch("agent_app.appium_process._build_env", return_value={"PATH": "/usr/bin"}),
        patch("agent_app.appium_process.os.path.isfile", return_value=False),
        patch.object(manager, "_wait_for_readiness", new_callable=AsyncMock, return_value=True),
        patch(
            "agent_app.appium_process.asyncio.create_subprocess_exec",
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
        patch("agent_app.appium_process.resolve_appium_invocation_for_pack", return_value=_STUB_INVOCATION),
        patch("agent_app.appium_process._build_env", return_value={"PATH": "/usr/bin"}),
        patch("agent_app.appium_process.os.path.isfile", return_value=False),
        patch.object(manager, "_wait_for_readiness", new_callable=AsyncMock, return_value=True),
        patch(
            "agent_app.appium_process.asyncio.create_subprocess_exec",
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
        patch("agent_app.appium_process.resolve_appium_invocation_for_pack", return_value=_STUB_INVOCATION),
        patch("agent_app.appium_process._build_env", return_value={"PATH": "/usr/bin"}),
        patch("agent_app.appium_process.os.path.isfile", return_value=False),
        patch.object(manager, "_wait_for_readiness", new_callable=AsyncMock, return_value=True),
        patch(
            "agent_app.appium_process.asyncio.create_subprocess_exec",
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
        patch("agent_app.appium_process.resolve_appium_invocation_for_pack", return_value=_STUB_INVOCATION),
        patch("agent_app.appium_process._build_env", return_value={"PATH": "/usr/bin"}),
        patch("agent_app.appium_process.os.path.isfile", return_value=False),
        patch.object(manager, "_wait_for_readiness", new_callable=AsyncMock, return_value=True),
        patch(
            "agent_app.appium_process.asyncio.create_subprocess_exec",
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
        patch("agent_app.appium_process.resolve_appium_invocation_for_pack", return_value=_STUB_INVOCATION),
        patch("agent_app.appium_process._build_env", return_value={"PATH": "/usr/bin"}),
        patch("agent_app.appium_process.os.path.isfile", return_value=False),
        patch.object(manager, "_wait_for_readiness", new_callable=AsyncMock, return_value=True),
        patch(
            "agent_app.appium_process.asyncio.create_subprocess_exec",
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
