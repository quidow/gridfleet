"""D2 at the manager boundary: an agent-owned occupant is reclaimed once; a
foreign one still raises PortOccupiedError."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest

from agent_app.appium.exceptions import PortOccupiedError
from agent_app.appium.process import AppiumInvocation, AppiumProcessManager

if TYPE_CHECKING:
    from collections.abc import Iterator

pytestmark = pytest.mark.asyncio

_STUB_INVOCATION = AppiumInvocation(binary="/usr/local/bin/appium")
PACK_START_KWARGS = {"pack_id": "appium-uiautomator2", "platform_id": "android_mobile"}
PORT = 4723


class _FakeVictim:
    def __init__(self) -> None:
        self.pid = 4242
        self.terminated = False

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None: ...

    def is_running(self) -> bool:
        return not self.terminated

    def status(self) -> str:
        return "zombie" if self.terminated else "running"


class _FakeProcess:
    def __init__(self, pid: int = 999) -> None:
        self.pid = pid
        self.returncode: int | None = None

    def kill(self) -> None:
        self.returncode = -9

    async def wait(self) -> int:
        return 0


@pytest.fixture
def spawn_stubs() -> Iterator[None]:
    with (
        patch("agent_app.appium.process.resolve_appium_invocation_for_pack", return_value=_STUB_INVOCATION),
        patch("agent_app.appium.process.build_env", return_value={"PATH": "/usr/bin"}),
        patch(
            "agent_app.appium.process.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=_FakeProcess()),
        ),
    ):
        yield


async def test_start_reclaims_an_agent_owned_appium_then_starts(spawn_stubs: None) -> None:
    mgr = AppiumProcessManager()
    victim = _FakeVictim()
    # Occupied on the first probe, free after the reclaim.
    connect_results = iter([True, False])

    async def fake_connect(_self: object, _port: int) -> bool:
        return next(connect_results, False)

    with (
        patch("agent_app.appium.process.AppiumProcessManager._can_connect_to_appium", new=fake_connect),
        patch("agent_app.appium.process.AppiumProcessManager._is_appium_port_bindable", return_value=True),
        patch.object(mgr, "_wait_for_readiness", new=AsyncMock(return_value=True)),
        patch("agent_app.appium.port_reclaim.find_agent_owned_appium", return_value=victim) as find,
    ):
        info = await mgr.start(connection_target="device-001", port=PORT, **PACK_START_KWARGS)

    assert victim.terminated, "the agent-owned occupant was not reclaimed"
    assert info.port == PORT
    assert find.call_args.kwargs["port"] == PORT
    await mgr.shutdown()


async def test_start_still_raises_for_a_foreign_occupant(spawn_stubs: None) -> None:
    mgr = AppiumProcessManager()

    async def always_connects(_self: object, _port: int) -> bool:
        return True

    with (
        patch("agent_app.appium.process.AppiumProcessManager._can_connect_to_appium", new=always_connects),
        patch("agent_app.appium.process.AppiumProcessManager._is_appium_port_bindable", return_value=True),
        patch("agent_app.appium.port_reclaim.find_agent_owned_appium", return_value=None),
        pytest.raises(PortOccupiedError),
    ):
        await mgr.start(connection_target="device-001", port=PORT, **PACK_START_KWARGS)


async def test_start_raises_when_the_port_is_still_held_after_one_reclaim(spawn_stubs: None) -> None:
    """Reclaim is once, not a loop: a still-occupied port reports the failure."""
    mgr = AppiumProcessManager()
    victim = _FakeVictim()

    async def always_connects(_self: object, _port: int) -> bool:
        return True

    with (
        patch("agent_app.appium.process.AppiumProcessManager._can_connect_to_appium", new=always_connects),
        patch("agent_app.appium.process.AppiumProcessManager._is_appium_port_bindable", return_value=True),
        patch("agent_app.appium.port_reclaim.find_agent_owned_appium", return_value=victim) as find,
        pytest.raises(PortOccupiedError),
    ):
        await mgr.start(connection_target="device-001", port=PORT, **PACK_START_KWARGS)
    assert find.call_count == 1


async def test_reclaim_excludes_processes_the_manager_tracks(spawn_stubs: None) -> None:
    mgr = AppiumProcessManager()
    tracked = _FakeProcess(pid=777)
    mgr._appium_procs[4800] = tracked  # type: ignore[assignment]

    async def always_connects(_self: object, _port: int) -> bool:
        return True

    with (
        patch("agent_app.appium.process.AppiumProcessManager._can_connect_to_appium", new=always_connects),
        patch("agent_app.appium.process.AppiumProcessManager._is_appium_port_bindable", return_value=True),
        patch("agent_app.appium.port_reclaim.find_agent_owned_appium", return_value=None) as find,
        pytest.raises(PortOccupiedError),
    ):
        await mgr.start(connection_target="device-001", port=PORT, **PACK_START_KWARGS)
    assert find.call_args.kwargs["exclude_pids"] == {777}
