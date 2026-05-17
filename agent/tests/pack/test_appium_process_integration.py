import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from agent_app.appium.exceptions import RuntimeNotInstalledError
from agent_app.appium.process import (
    AppiumInvocation,
    AppiumLaunchSpec,
    AppiumProcessManager,
    _build_env,
    resolve_appium_invocation_for_pack,
)
from agent_app.pack.adapter_registry import AdapterRegistry
from agent_app.pack.adapter_types import LifecycleActionResult
from agent_app.pack.runtime import RuntimeEnv
from agent_app.pack.runtime_registry import RuntimeRegistry


def test_resolve_uses_runtime_env_when_registered() -> None:
    registry = RuntimeRegistry()
    registry.set_for_pack(
        "appium-uiautomator2",
        RuntimeEnv(
            runtime_id="abc123",
            appium_home="/var/lib/gridfleet-agent/runtimes/abc123",
            appium_bin="/var/lib/gridfleet-agent/runtimes/abc123/node_modules/.bin/appium",
            server_package="appium",
            server_version="2.11.5",
        ),
    )
    resolved = resolve_appium_invocation_for_pack(pack_id="appium-uiautomator2", registry=registry)
    assert isinstance(resolved, AppiumInvocation)
    assert resolved.binary.endswith("appium")
    assert resolved.env_extra["APPIUM_HOME"] == "/var/lib/gridfleet-agent/runtimes/abc123"


def test_resolve_raises_when_no_pack_id() -> None:
    registry = RuntimeRegistry()
    with pytest.raises(RuntimeNotInstalledError, match="No runtime installed for pack"):
        resolve_appium_invocation_for_pack(pack_id=None, registry=registry)


def test_resolve_raises_when_pack_not_installed() -> None:
    registry = RuntimeRegistry()
    with pytest.raises(RuntimeNotInstalledError, match="appium-uiautomator2"):
        resolve_appium_invocation_for_pack(pack_id="appium-uiautomator2", registry=registry)


def test_build_env_prepends_pack_runtime_bin_dir_and_sets_appium_home() -> None:
    env = _build_env(
        platform_name="android_mobile",
        device_type="real_device",
        appium_bin="/var/lib/gridfleet-agent/runtimes/abc123/node_modules/.bin/appium",
        appium_home="/var/lib/gridfleet-agent/runtimes/abc123",
    )
    assert env["APPIUM_HOME"] == "/var/lib/gridfleet-agent/runtimes/abc123"
    path = env.get("PATH", "")
    first = path.split(":", 1)[0]
    assert first == "/var/lib/gridfleet-agent/runtimes/abc123/node_modules/.bin"


def test_build_env_without_appium_bin_still_produces_valid_env() -> None:
    # When no appium_bin is supplied, _build_env still runs (no appium dir is added to PATH).
    # The caller (resolve_appium_invocation_for_pack) is responsible for providing the binary.
    env = _build_env(platform_name="android_mobile", device_type="real_device")
    assert "PATH" in env


@pytest.mark.asyncio
async def test_start_routes_pack_id_through_launch_spec(monkeypatch: pytest.MonkeyPatch) -> None:
    """Proves pack_id flows request → start() → AppiumLaunchSpec."""
    captured: dict[str, AppiumLaunchSpec] = {}

    async def _fake_start_appium_server(
        self: AppiumProcessManager,
        spec: AppiumLaunchSpec,
        *,
        clear_logs_on_failure: bool,
    ) -> object:
        captured["spec"] = spec

        class _P:
            pid = 12345
            returncode = None

        return _P()

    async def _fake_start_grid_node_service(
        self: AppiumProcessManager,
        spec: AppiumLaunchSpec,
    ) -> None:
        return None

    mgr = AppiumProcessManager()
    registry = RuntimeRegistry()
    registry.set_for_pack(
        "appium-uiautomator2",
        RuntimeEnv(
            runtime_id="abc123",
            appium_home="/var/lib/gridfleet-agent/runtimes/abc123",
            appium_bin="/var/lib/gridfleet-agent/runtimes/abc123/node_modules/.bin/appium",
            server_package="appium",
            server_version="2.11.5",
        ),
    )
    mgr.set_runtime_registry(registry)
    monkeypatch.setattr(AppiumProcessManager, "_start_appium_server", _fake_start_appium_server)
    monkeypatch.setattr(AppiumProcessManager, "_start_grid_node_service", _fake_start_grid_node_service)

    await mgr.start(
        connection_target="ABCD1234",
        port=4723,
        grid_url="http://hub:4444",
        plugins=None,
        extra_caps=None,
        stereotype_caps={"platformName": "Android"},
        device_type="real_device",
        ip_address=None,
        headless=False,
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
    )

    spec = captured["spec"]
    assert spec.pack_id == "appium-uiautomator2"
    assert spec.platform_id == "android_mobile"


@pytest.mark.asyncio
async def test_pack_start_default_caps_use_appium_platform_name(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured: dict[str, object] = {}

    class FakeProcess:
        pid = 1234
        returncode = None
        stdout = None
        stderr = None

        async def wait(self) -> int:
            return 0

    async def fake_create_subprocess_exec(*cmd: str, **kwargs: object) -> FakeProcess:
        captured["cmd"] = list(cmd)
        captured["env"] = kwargs.get("env")
        return FakeProcess()

    mgr = AppiumProcessManager()
    registry = RuntimeRegistry()
    registry.set_for_pack(
        "appium-uiautomator2",
        RuntimeEnv(
            runtime_id="runtime-android",
            appium_home=str(tmp_path / "appium-home"),
            appium_bin="/tmp/fake-appium",
            server_package="appium",
            server_version="2.11.5",
        ),
    )
    mgr.set_runtime_registry(registry)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr(mgr, "_can_connect_to_appium", AsyncMock(return_value=False))
    monkeypatch.setattr(mgr, "_is_appium_port_bindable", lambda port: True)
    monkeypatch.setattr(mgr, "_wait_for_readiness", AsyncMock(return_value=True))
    monkeypatch.setattr(mgr, "_start_grid_node_service", AsyncMock(return_value=None))

    await mgr.start(
        connection_target="SERIAL1",
        appium_platform_name="Android",
        port=4723,
        grid_url="http://grid:4444",
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        manage_grid_node=False,
    )

    cmd = captured["cmd"]
    assert isinstance(cmd, list)
    caps = json.loads(cmd[cmd.index("--default-capabilities") + 1])
    assert caps["platformName"] == "Android"


@pytest.mark.asyncio
async def test_pack_emulator_start_uses_adapter_lifecycle_boot(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    captured: dict[str, object] = {}
    lifecycle_action = AsyncMock(return_value=LifecycleActionResult(ok=True, state="emulator-5554"))

    class FakeAdapter:
        pack_id = "appium-uiautomator2"
        pack_release = "2026.04.0"

        async def lifecycle_action(self, *args: object, **kwargs: object) -> LifecycleActionResult:
            return await lifecycle_action(*args, **kwargs)

        async def pre_session(self, spec: object) -> dict[str, object]:
            return {}

    class FakeProcess:
        pid = 1234
        returncode = None
        stdout = None
        stderr = None

        async def wait(self) -> int:
            return 0

    async def fake_create_subprocess_exec(*cmd: str, **kwargs: object) -> FakeProcess:
        captured["cmd"] = list(cmd)
        captured["env"] = kwargs.get("env")
        return FakeProcess()

    mgr = AppiumProcessManager()
    registry = RuntimeRegistry()
    registry.set_for_pack(
        "appium-uiautomator2",
        RuntimeEnv(
            runtime_id="runtime-android",
            appium_home=str(tmp_path / "appium-home"),
            appium_bin="/tmp/fake-appium",
            server_package="appium",
            server_version="2.11.5",
        ),
    )
    mgr.set_runtime_registry(registry)
    adapter_registry = AdapterRegistry()
    adapter_registry.set("appium-uiautomator2", "2026.04.0", FakeAdapter())  # type: ignore[arg-type]
    mgr.set_adapter_registry(adapter_registry)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr(mgr, "_can_connect_to_appium", AsyncMock(return_value=False))
    monkeypatch.setattr(mgr, "_is_appium_port_bindable", lambda port: True)
    monkeypatch.setattr(mgr, "_wait_for_readiness", AsyncMock(return_value=True))
    monkeypatch.setattr(mgr, "_start_grid_node_service", AsyncMock(return_value=None))

    info = await mgr.start(
        connection_target="Pixel_8_API_35",
        appium_platform_name="Android",
        port=4723,
        grid_url="http://grid:4444",
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        lifecycle_actions=[{"id": "boot"}],
        device_type="emulator",
        manage_grid_node=False,
    )

    args, _kwargs = lifecycle_action.await_args
    assert args[0] == "boot"
    assert args[1] == {"headless": True}
    assert info.connection_target == "emulator-5554"
    cmd = captured["cmd"]
    assert isinstance(cmd, list)
    caps = json.loads(cmd[cmd.index("--default-capabilities") + 1])
    assert caps["appium:udid"] == "emulator-5554"
