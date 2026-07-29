"""A spawn-and-die loop must not accumulate per-spawn log files.

Each spawn gets its own log file. The success path prunes the port down to
``LOG_FILES_PER_PORT``; the failure path used to delete only the first failure's
file on a port (``clear_logs_on_failure``), so a node that started once and then
fell into a restart loop grew one file per retry with nothing ever reclaiming
them — the convergence loop retries every few seconds, forever.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest

from agent_app.appium.exceptions import AppiumExitedError
from agent_app.appium.log_files import LOG_FILES_PER_PORT, port_log_paths
from agent_app.appium.process import AppiumInvocation, AppiumProcessManager

if TYPE_CHECKING:
    from collections.abc import Iterator

_STUB_INVOCATION = AppiumInvocation(binary="/usr/local/bin/appium")
PACK_START_KWARGS = {"pack_id": "appium-uiautomator2", "platform_id": "android_mobile"}
PORT = 4723
FAILED_SPAWNS = 6


class _FakeProcess:
    """A child that is already gone by the time readiness is checked."""

    def __init__(self, pid: int, returncode: int | None) -> None:
        self.pid = pid
        self.returncode = returncode

    def kill(self) -> None:
        self.returncode = -9

    async def wait(self) -> int:
        return self.returncode or 0


@pytest.fixture
def spawned() -> Iterator[list[_FakeProcess]]:
    """Every spawn returns a fresh dead child; the first one starts alive."""
    processes: list[_FakeProcess] = []

    async def fake_spawn(*_args: object, **_kwargs: object) -> _FakeProcess:
        proc = _FakeProcess(pid=1000 + len(processes), returncode=None if not processes else 0)
        processes.append(proc)
        return proc

    with (
        patch("agent_app.appium.process.resolve_appium_invocation_for_pack", return_value=_STUB_INVOCATION),
        patch("agent_app.appium.process.build_env", return_value={"PATH": "/usr/bin"}),
        patch("agent_app.appium.process.asyncio.create_subprocess_exec", new=fake_spawn),
        # The watchdog would race the fake child's instant exit into the
        # auto-restart ladder; this test drives the retries itself.
        patch("agent_app.appium.process.AppiumProcessManager._watch_appium_process", new=AsyncMock()),
        patch("agent_app.appium.process.AppiumProcessManager._port_occupied_detail", new=AsyncMock(return_value=None)),
    ):
        yield processes


async def test_failed_spawn_loop_after_a_successful_start_bounds_the_log_count(
    spawned: list[_FakeProcess],
) -> None:
    mgr = AppiumProcessManager()
    readiness = AsyncMock(return_value=True)
    with patch.object(mgr, "_wait_for_readiness", new=readiness):
        await mgr.start(connection_target="device-001", port=PORT, **PACK_START_KWARGS)
    assert len(port_log_paths(PORT)) == 1

    # The node dies, and every retry dies on boot before becoming ready.
    spawned[0].returncode = 0
    readiness.return_value = False
    for _ in range(FAILED_SPAWNS):
        with (
            patch.object(mgr, "_wait_for_readiness", new=readiness),
            pytest.raises(AppiumExitedError),
        ):
            await mgr.start(connection_target="device-001", port=PORT, **PACK_START_KWARGS)

    assert len(spawned) == 1 + FAILED_SPAWNS, "each retry must really have spawned"
    assert len(port_log_paths(PORT)) <= LOG_FILES_PER_PORT, (
        f"a repeated failed-spawn loop accumulated {len(port_log_paths(PORT))} log files for port {PORT}"
    )
