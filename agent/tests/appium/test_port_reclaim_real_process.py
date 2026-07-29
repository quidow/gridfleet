"""D2 against real processes: psutil sees what this code thinks it sees, an
agent-owned child is found, killed, and — the product claim — its port comes
back free, while a look-alike is left running.

The fake-based tests in test_port_reclaim.py pin the predicates. These pin the
contracts with the operating system that fakes cannot: that a foreign process
is still alive after a scan, that a real listener's port is bindable again
after reclaim, that a SIGTERM-ignoring process dies to SIGKILL, and that a
killed-but-unreaped child has released its socket (the assumption ``_is_gone``
encodes when it treats a zombie as gone).
"""

from __future__ import annotations

import signal
import socket
import subprocess
import sys
import time
from typing import TYPE_CHECKING

import pytest

from agent_app.appium import port_reclaim
from agent_app.appium.process import AppiumProcessManager
from agent_app.config import agent_settings

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

pytestmark = pytest.mark.asyncio

# Argv-only tests need a number, not a socket: deliberately outside
# AGENT_APPIUM_PORT_RANGE_* so a real lab Appium on a dev host can never be the
# process they find. The binding tests below allocate a real free port instead.
PORT = 44723
_IDLE_SCRIPT = "import time\nfor _ in range(600):\n    time.sleep(0.1)\n"
# Binds the port for real, exactly as Appium does (0.0.0.0, so the agent's own
# bind probe sees it). ``--ignore-term`` makes it survive SIGTERM so the kill
# escalation has something real to escalate against.
_BIND_SCRIPT = """\
import signal, socket, sys, time
if "--ignore-term" in sys.argv:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
port = int(sys.argv[sys.argv.index("--port") + 1])
srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
srv.bind(("0.0.0.0", port))
srv.listen(8)
for _ in range(600):
    time.sleep(0.1)
"""


def _fake_appium(bin_dir: Path, body: str = _IDLE_SCRIPT) -> Path:
    """A stand-in Appium binary at *bin_dir*/appium, argv-shaped like the real one."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    script = bin_dir / "appium"
    script.write_text(body)
    return script


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("0.0.0.0", 0))
        return int(probe.getsockname()[1])


def _port_free(port: int) -> bool:
    """Ask the same probe production asks — this is the answer reclaim acts on."""
    return AppiumProcessManager()._is_appium_port_bindable(port)


@pytest.fixture
def spawned() -> Iterator[list[subprocess.Popen[bytes]]]:
    """Every process a test starts, killed on the way out however the test ends."""
    procs: list[subprocess.Popen[bytes]] = []
    yield procs
    for proc in procs:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=10)


def _spawn(
    procs: list[subprocess.Popen[bytes]],
    script: Path,
    port: int = PORT,
    *,
    ignore_term: bool = False,
) -> subprocess.Popen[bytes]:
    # argv mirrors the shebang-rewritten shape psutil reports for the real
    # spawn: [interpreter, <path to appium>, "server", "--port", "<port>"].
    argv = [sys.executable, str(script), "server", "--port", str(port)]
    if ignore_term:
        argv.append("--ignore-term")
    proc = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    procs.append(proc)
    # psutil reads /proc-equivalent state; give the child a moment to be visible.
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if port_reclaim.psutil.pid_exists(proc.pid):
            return proc
        time.sleep(0.05)
    raise AssertionError(f"spawned pid {proc.pid} never became visible to psutil")


def _spawn_listener(
    procs: list[subprocess.Popen[bytes]], script: Path, port: int, *, ignore_term: bool = False
) -> subprocess.Popen[bytes]:
    """Spawn a child and wait until it really holds *port*."""
    proc = _spawn(procs, script, port, ignore_term=ignore_term)
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if not _port_free(port):
            return proc
        if proc.poll() is not None:
            raise AssertionError(f"listener exited early with {proc.returncode}")
        time.sleep(0.05)
    raise AssertionError(f"listener never bound port {port}")


async def test_a_real_agent_owned_appium_is_found_and_reclaimed(
    tmp_path: Path, spawned: list[subprocess.Popen[bytes]]
) -> None:
    runtime_root = tmp_path / "runtimes"
    proc = _spawn(spawned, _fake_appium(runtime_root / "pack" / "1.0.0" / "bin"))

    found = port_reclaim.find_agent_owned_appium(port=PORT, runtime_root=str(runtime_root), exclude_pids=set())
    assert found is not None, "psutil did not surface the spawned child as agent-owned"
    assert found.pid == proc.pid

    await port_reclaim.terminate_process(found)
    assert proc.wait(timeout=5) == -signal.SIGTERM, "the graceful signal was not what killed it"


async def test_reclaiming_a_real_listener_actually_frees_the_port(
    tmp_path: Path, spawned: list[subprocess.Popen[bytes]]
) -> None:
    """The product claim. A killed process that has not released its socket is a
    port still occupied, and every later start on it fails exactly as before."""
    port = _free_port()
    runtime_root = tmp_path / "runtimes"
    proc = _spawn_listener(
        spawned,
        _fake_appium(runtime_root / "pack" / "1.0.0" / "bin", _BIND_SCRIPT),
        port,
    )
    assert not _port_free(port), "precondition: the listener must hold the port"

    found = port_reclaim.find_agent_owned_appium(port=port, runtime_root=str(runtime_root), exclude_pids=set())
    assert found is not None
    await port_reclaim.terminate_process(found)

    assert proc.wait(timeout=5) is not None
    assert _port_free(port), "the process died but the port never came free"


async def test_a_real_listener_that_ignores_sigterm_is_killed_and_releases_the_port(
    tmp_path: Path, spawned: list[subprocess.Popen[bytes]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Escalation against a process that really ignores SIGTERM — the fake-based
    twin only proves the branch is taken, not that the signal lands."""
    monkeypatch.setattr(port_reclaim, "TERMINATE_GRACE_SEC", 0.5)
    port = _free_port()
    runtime_root = tmp_path / "runtimes"
    proc = _spawn_listener(
        spawned,
        _fake_appium(runtime_root / "pack" / "1.0.0" / "bin", _BIND_SCRIPT),
        port,
        ignore_term=True,
    )

    found = port_reclaim.find_agent_owned_appium(port=port, runtime_root=str(runtime_root), exclude_pids=set())
    assert found is not None
    await port_reclaim.terminate_process(found)

    assert proc.wait(timeout=5) == -signal.SIGKILL, "SIGTERM-ignoring process was not escalated to SIGKILL"
    assert _port_free(port), "the killed process left its port bound"


async def test_a_killed_but_unreaped_child_counts_as_gone_and_has_released_its_port(
    tmp_path: Path, spawned: list[subprocess.Popen[bytes]]
) -> None:
    """``_is_gone`` treats a zombie as gone because a zombie has released its
    sockets. In production the reclaimed process is the agent's own child, so
    that state is the normal one — assert the assumption instead of documenting it.
    """
    port = _free_port()
    runtime_root = tmp_path / "runtimes"
    proc = _spawn_listener(
        spawned,
        _fake_appium(runtime_root / "pack" / "1.0.0" / "bin", _BIND_SCRIPT),
        port,
    )
    handle = port_reclaim.psutil.Process(proc.pid)

    proc.kill()  # deliberately NOT reaped: no poll(), no wait()
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and not port_reclaim._is_gone(handle):
        time.sleep(0.05)

    assert port_reclaim._is_gone(handle), "an unreaped dead child was still reported as running"
    assert handle.status() == port_reclaim.psutil.STATUS_ZOMBIE, "precondition: the child must be unreaped"
    assert _port_free(port), "a zombie was holding its port — _is_gone's assumption is wrong"


async def test_the_manager_reclaims_a_real_listener_end_to_end(
    tmp_path: Path, spawned: list[subprocess.Popen[bytes]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """``_reclaim_unmanaged_port`` returns True only when the port is genuinely
    free again — the real method, a real listener, the real bind probe.

    ``_can_connect_to_appium`` is stubbed False (a raw listener speaks no HTTP,
    and waiting out its 2s timeout would buy nothing): the occupancy signal
    under test here is the bind probe, which is what decides 'free'.
    """
    port = _free_port()
    runtime_root = tmp_path / "runtimes"
    # Object form, matching conftest's isolated_runtime_root fixture: a dotted
    # string target would make pytest try to import ``…process.agent_settings``.
    monkeypatch.setattr(agent_settings.runtime, "runtime_root", str(runtime_root))

    async def no_appium_http(_self: object, _port: int) -> bool:
        return False

    monkeypatch.setattr(AppiumProcessManager, "_can_connect_to_appium", no_appium_http)
    proc = _spawn_listener(
        spawned,
        _fake_appium(runtime_root / "pack" / "1.0.0" / "bin", _BIND_SCRIPT),
        port,
    )
    mgr = AppiumProcessManager()
    assert (await mgr._port_occupied_detail(port)) is not None, "precondition: the port must read as occupied"

    reclaimed = await mgr._reclaim_unmanaged_port(port)

    assert reclaimed is True
    assert proc.wait(timeout=5) is not None
    assert (await mgr._port_occupied_detail(port)) is None


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
