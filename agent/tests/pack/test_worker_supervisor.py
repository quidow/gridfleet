from __future__ import annotations

import asyncio
import time
from itertools import pairwise
from typing import TYPE_CHECKING

import pytest

from agent_app.pack.adapter_dispatch import AdapterContractError, AdapterHookExecutionError, AdapterHookTimeoutError
from agent_app.pack.adapter_types import DiscoveryCandidate, FieldError
from agent_app.pack.worker_supervisor import WorkerSupervisor

if TYPE_CHECKING:
    from pathlib import Path


def _write_adapter(site: Path, source: str) -> None:
    package = site / "adapter"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(source)


@pytest.mark.asyncio
async def test_call_returns_typed_result(tmp_path: Path) -> None:
    _write_adapter(tmp_path, "class Adapter:\n    async def discover(self, ctx):\n        return []\n")
    supervisor = WorkerSupervisor(hook_timeout=1, kill_grace_sec=0.1, restart_delays=(0.01,))
    handle = await supervisor.start("pack", "1", tmp_path)
    try:
        result = await handle.call("discover", {"ctx": {"host_id": "h", "platform_id": "p"}})
        assert result == []
        assert handle.alive
    finally:
        await supervisor.shutdown_all()


@pytest.mark.asyncio
async def test_discover_roundtrips_nested_field_errors(tmp_path: Path) -> None:
    # The one non-trivial decoder path: DiscoveryCandidate nests list[FieldError],
    # so the wire result must reconstruct typed FieldError objects, not dicts.
    _write_adapter(
        tmp_path,
        """
from agent_app.pack.adapter_types import DiscoveryCandidate, FieldError

class Adapter:
    async def discover(self, ctx):
        return [DiscoveryCandidate(
            identity_scheme="serial", identity_value="dev-1", suggested_name="Device 1",
            detected_properties={"model": "x"}, runnable=False,
            missing_requirements=["driver"],
            field_errors=[FieldError(field_id="serial", message="bad")],
        )]
""",
    )
    supervisor = WorkerSupervisor(hook_timeout=1, kill_grace_sec=0.1)
    handle = await supervisor.start("pack", "1", tmp_path)
    try:
        result = await handle.call("discover", {"ctx": {"host_id": "h", "platform_id": "p"}})
        assert result == [
            DiscoveryCandidate(
                identity_scheme="serial",
                identity_value="dev-1",
                suggested_name="Device 1",
                detected_properties={"model": "x"},
                runnable=False,
                missing_requirements=["driver"],
                field_errors=[FieldError(field_id="serial", message="bad")],
            )
        ]
        assert isinstance(result[0].field_errors[0], FieldError)
    finally:
        await supervisor.shutdown_all()


@pytest.mark.asyncio
async def test_blocking_hook_kills_and_restarts(tmp_path: Path) -> None:
    _write_adapter(
        tmp_path,
        """
import time
from pathlib import Path

class Adapter:
    async def health_check(self, ctx):
        marker = Path(__file__).with_name("blocked")
        if not marker.exists():
            marker.touch()
            time.sleep(60)
        return []
""",
    )
    supervisor = WorkerSupervisor(hook_timeout=0.2, kill_grace_sec=0.1, restart_delays=(0.01,))
    handle = await supervisor.start("pack", "1", tmp_path)
    try:
        with pytest.raises(AdapterHookTimeoutError):
            await asyncio.wait_for(
                handle.call(
                    "health_check",
                    {"ctx": {"device_identity_value": "d", "allow_boot": False}},
                ),
                timeout=2,
            )
        for _ in range(100):
            if handle.alive:
                break
            await asyncio.sleep(0.01)
        assert handle.alive
        assert await handle.call("health_check", {"ctx": {"device_identity_value": "d", "allow_boot": False}}) == []
    finally:
        await supervisor.shutdown_all()


@pytest.mark.asyncio
async def test_worker_crash_fails_pending_and_restarts(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    _write_adapter(
        first,
        """
import os
from pathlib import Path

class Adapter:
    async def discover(self, ctx):
        marker = Path(__file__).with_name("crashed")
        if not marker.exists():
            marker.touch()
            os._exit(3)
        return []
""",
    )
    _write_adapter(second, "class Adapter:\n    async def discover(self, ctx):\n        return []\n")
    supervisor = WorkerSupervisor(hook_timeout=1, kill_grace_sec=0.1, restart_delays=(0.01,))
    crashing = await supervisor.start("crash", "1", first)
    healthy = await supervisor.start("healthy", "1", second)
    try:
        with pytest.raises(AdapterHookExecutionError):
            await crashing.call("discover", {"ctx": {"host_id": "h", "platform_id": "p"}})
        assert await healthy.call("discover", {"ctx": {"host_id": "h", "platform_id": "p"}}) == []
        for _ in range(100):
            if crashing.alive:
                break
            await asyncio.sleep(0.01)
        assert crashing.alive
        assert await crashing.call("discover", {"ctx": {"host_id": "h", "platform_id": "p"}}) == []
    finally:
        await supervisor.shutdown_all()


@pytest.mark.asyncio
async def test_event_loop_not_blocked_during_hang(tmp_path: Path) -> None:
    _write_adapter(
        tmp_path, "import time\nclass Adapter:\n    async def health_check(self, ctx):\n        time.sleep(60)\n"
    )
    supervisor = WorkerSupervisor(hook_timeout=0.2, kill_grace_sec=0.1, restart_delays=(0.01,))
    handle = await supervisor.start("pack", "1", tmp_path)
    ticks: list[float] = []
    stop = asyncio.Event()

    async def ticker() -> None:
        while not stop.is_set():
            ticks.append(time.monotonic())
            await asyncio.sleep(0.05)

    ticker_task = asyncio.create_task(ticker())
    try:
        with pytest.raises(AdapterHookTimeoutError):
            await handle.call("health_check", {"ctx": {"device_identity_value": "d", "allow_boot": False}})
    finally:
        stop.set()
        await ticker_task
        await supervisor.shutdown_all()
    assert max((b - a for a, b in pairwise(ticks)), default=0) < 2


@pytest.mark.asyncio
async def test_contract_violation_raises(tmp_path: Path) -> None:
    _write_adapter(tmp_path, "class Adapter:\n    async def discover(self, ctx):\n        return 'wrong'\n")
    supervisor = WorkerSupervisor(hook_timeout=1, kill_grace_sec=0.1)
    handle = await supervisor.start("pack", "1", tmp_path)
    try:
        with pytest.raises(AdapterContractError):
            await handle.call("discover", {"ctx": {"host_id": "h", "platform_id": "p"}})
        assert handle.alive
    finally:
        await supervisor.shutdown_all()


@pytest.mark.asyncio
async def test_shutdown_all_reaps_processes(tmp_path: Path) -> None:
    _write_adapter(tmp_path, "class Adapter:\n    async def discover(self, ctx):\n        return []\n")
    supervisor = WorkerSupervisor(hook_timeout=1, kill_grace_sec=0.1)
    handle = await supervisor.start("pack", "1", tmp_path)
    proc = handle._process
    assert proc is not None
    await supervisor.shutdown_all()
    assert not handle.alive
    assert proc.returncode is not None
