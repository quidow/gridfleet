"""D2 against real processes: psutil sees what this code thinks it sees, an
agent-owned child is found and killed, and a look-alike is left running.

The fake-based tests in test_port_reclaim.py pin the predicates. These pin the
contract with the operating system — including the one that matters when the
match is wrong: a foreign process must still be alive afterwards.
"""

from __future__ import annotations

import subprocess
import sys
import time
from typing import TYPE_CHECKING

import pytest

from agent_app.appium import port_reclaim

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

pytestmark = pytest.mark.asyncio

# Deliberately outside AGENT_APPIUM_PORT_RANGE_* so a real lab Appium on a dev
# host can never be the process these tests find (nothing binds here — the port
# is an argv token, not a socket).
PORT = 44723
_SCRIPT = "import sys, time\nfor _ in range(600):\n    time.sleep(0.1)\n"


def _fake_appium(bin_dir: Path) -> Path:
    """A stand-in Appium binary at *bin_dir*/appium, argv-shaped like the real one."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    script = bin_dir / "appium"
    script.write_text(_SCRIPT)
    return script


@pytest.fixture
def spawned() -> Iterator[list[subprocess.Popen[bytes]]]:
    """Every process a test starts, killed on the way out however the test ends."""
    procs: list[subprocess.Popen[bytes]] = []
    yield procs
    for proc in procs:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=10)


def _spawn(procs: list[subprocess.Popen[bytes]], script: Path) -> subprocess.Popen[bytes]:
    # argv mirrors the shebang-rewritten shape psutil reports for the real
    # spawn: [interpreter, <path to appium>, "server", "--port", "<port>"].
    proc = subprocess.Popen(
        [sys.executable, str(script), "server", "--port", str(PORT)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    procs.append(proc)
    # psutil reads /proc-equivalent state; give the child a moment to be visible.
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if port_reclaim.psutil.pid_exists(proc.pid):
            return proc
        time.sleep(0.05)
    raise AssertionError(f"spawned pid {proc.pid} never became visible to psutil")


async def test_a_real_agent_owned_appium_is_found_and_reclaimed(
    tmp_path: Path, spawned: list[subprocess.Popen[bytes]]
) -> None:
    runtime_root = tmp_path / "runtimes"
    proc = _spawn(spawned, _fake_appium(runtime_root / "pack" / "1.0.0" / "bin"))

    found = port_reclaim.find_agent_owned_appium(port=PORT, runtime_root=str(runtime_root), exclude_pids=set())
    assert found is not None, "psutil did not surface the spawned child as agent-owned"
    assert found.pid == proc.pid

    await port_reclaim.terminate_process(found)
    assert proc.poll() is not None or proc.wait(timeout=5) is not None


async def test_a_real_sibling_prefix_process_is_neither_matched_nor_killed(
    tmp_path: Path, spawned: list[subprocess.Popen[bytes]]
) -> None:
    """``<root>-evil`` shares the string prefix. The defect this guards would
    have terminated it."""
    runtime_root = tmp_path / "runtimes"
    runtime_root.mkdir(parents=True, exist_ok=True)
    proc = _spawn(spawned, _fake_appium(tmp_path / "runtimes-evil" / "bin"))

    found = port_reclaim.find_agent_owned_appium(port=PORT, runtime_root=str(runtime_root), exclude_pids=set())

    assert found is None, "a sibling directory sharing the prefix was claimed as agent-owned"
    assert proc.poll() is None, "a foreign process was terminated"


async def test_a_real_foreign_appium_outside_the_root_is_left_running(
    tmp_path: Path, spawned: list[subprocess.Popen[bytes]]
) -> None:
    runtime_root = tmp_path / "runtimes"
    runtime_root.mkdir(parents=True, exist_ok=True)
    proc = _spawn(spawned, _fake_appium(tmp_path / "usr" / "local" / "bin"))

    found = port_reclaim.find_agent_owned_appium(port=PORT, runtime_root=str(runtime_root), exclude_pids=set())

    assert found is None
    assert proc.poll() is None, "a foreign process was terminated"


async def test_a_real_tracked_process_is_never_reclaimed(
    tmp_path: Path, spawned: list[subprocess.Popen[bytes]]
) -> None:
    """The manager's own live children are excluded by pid, so reclaim can only
    ever reach something nothing tracks."""
    runtime_root = tmp_path / "runtimes"
    proc = _spawn(spawned, _fake_appium(runtime_root / "pack" / "1.0.0" / "bin"))

    found = port_reclaim.find_agent_owned_appium(port=PORT, runtime_root=str(runtime_root), exclude_pids={proc.pid})

    assert found is None
    assert proc.poll() is None
