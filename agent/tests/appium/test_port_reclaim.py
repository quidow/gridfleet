"""D2: only an Appium this host started may be reclaimed from a desired port."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from agent_app.appium import port_reclaim

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

RUNTIME_ROOT = "/opt/gridfleet-agent/runtimes"
OWNED_CMDLINE = [
    "/usr/local/bin/node",
    f"{RUNTIME_ROOT}/appium-uiautomator2/1.2.3/node_modules/.bin/appium",
    "server",
    "--port",
    "4723",
    "--log-no-colors",
]
FOREIGN_CMDLINE = ["/usr/local/bin/node", "/usr/local/lib/appium/appium", "server", "--port", "4723"]


class _FakeProc:
    """Stands in for a psutil.Process — the module only uses this surface."""

    def __init__(
        self,
        pid: int,
        cmdline: list[str],
        *,
        ignores_term: bool = False,
        environ: dict | None = None,
    ) -> None:
        self.pid = pid
        self.info = {"pid": pid, "cmdline": cmdline}
        self.terminated = False
        self.killed = False
        self._alive = True
        self._ignores_term = ignores_term
        self._environ = environ or {}

    def terminate(self) -> None:
        self.terminated = True
        if not self._ignores_term:
            self._alive = False

    def kill(self) -> None:
        self.killed = True
        self._alive = False

    def is_running(self) -> bool:
        return self._alive

    def status(self) -> str:
        return "running" if self._alive else "zombie"

    def environ(self) -> dict:
        return self._environ


def test_serves_port_matches_the_agents_spawn_shape() -> None:
    assert port_reclaim.serves_port(OWNED_CMDLINE, 4723)
    assert not port_reclaim.serves_port(OWNED_CMDLINE, 4724)
    assert not port_reclaim.serves_port(["node", "/x/appium", "--port", "4723"], 4723)  # no "server"
    assert not port_reclaim.serves_port(["node", "/x/appium", "server", "--port"], 4723)  # truncated
    assert not port_reclaim.serves_port([], 4723)


def test_runs_from_runtime_root_requires_a_path_under_the_root() -> None:
    assert port_reclaim.runs_from_runtime_root(OWNED_CMDLINE, RUNTIME_ROOT)
    assert not port_reclaim.runs_from_runtime_root(FOREIGN_CMDLINE, RUNTIME_ROOT)


def test_runs_from_runtime_root_rejects_a_sibling_that_shares_the_prefix() -> None:
    """``<root>-evil`` starts with ``<root>`` as a string and is not under it.
    A prefix match here means SIGKILL on someone else's process."""
    sibling = ["node", f"{RUNTIME_ROOT}-evil/bin/appium", "server", "--port", "4723"]
    assert not port_reclaim.runs_from_runtime_root(sibling, RUNTIME_ROOT)


def test_runs_from_runtime_root_rejects_a_traversal_out_of_the_root() -> None:
    traversal = ["node", f"{RUNTIME_ROOT}/../elsewhere/bin/appium", "server", "--port", "4723"]
    assert not port_reclaim.runs_from_runtime_root(traversal, RUNTIME_ROOT)


def test_runs_from_runtime_root_accepts_a_non_normalised_path_inside_the_root() -> None:
    """Canonicalisation cuts both ways: a path that detours and comes back is ours."""
    inside = ["node", f"{RUNTIME_ROOT}/pack/../pack/bin/appium", "server", "--port", "4723"]
    assert port_reclaim.runs_from_runtime_root(inside, RUNTIME_ROOT)


def test_runs_from_runtime_root_ignores_relative_arguments(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A relative token resolves against *this* process's cwd, not the target's.
    An agent whose cwd sits inside the runtime root must not read a bare
    ``appium`` token as ownership evidence."""
    monkeypatch.chdir(tmp_path)
    assert not port_reclaim.runs_from_runtime_root(["appium", "server", "--port", "4723"], str(tmp_path))


def test_runs_from_runtime_root_ignores_a_root_shaped_argument_that_is_not_a_path() -> None:
    """Only real path arguments count; a capability string that happens to embed
    the root must not confer ownership."""
    noise = [
        "node",
        "/usr/local/bin/appium",
        "server",
        "--port",
        "4723",
        "--default-capabilities",
        f'{{"appium:app": "{RUNTIME_ROOT}/x.apk"}}',
    ]
    assert not port_reclaim.runs_from_runtime_root(noise, RUNTIME_ROOT)


def test_find_rejects_a_sibling_prefix_occupant(monkeypatch: pytest.MonkeyPatch) -> None:
    sibling = _FakeProc(4242, ["node", f"{RUNTIME_ROOT}-evil/bin/appium", "server", "--port", "4723"])
    monkeypatch.setattr(port_reclaim.psutil, "process_iter", lambda _attrs: iter([sibling]))
    assert port_reclaim.find_agent_owned_appium(port=4723, runtime_root=RUNTIME_ROOT, exclude_pids=set()) is None


def test_find_rejects_an_appium_home_sibling_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    proc = _FakeProc(4242, FOREIGN_CMDLINE, environ={"APPIUM_HOME": f"{RUNTIME_ROOT}-evil/home"})
    monkeypatch.setattr(port_reclaim.psutil, "process_iter", lambda _attrs: iter([proc]))
    assert port_reclaim.find_agent_owned_appium(port=4723, runtime_root=RUNTIME_ROOT, exclude_pids=set()) is None


def test_find_returns_the_agent_owned_occupant(monkeypatch: pytest.MonkeyPatch) -> None:
    owned = _FakeProc(4242, OWNED_CMDLINE)
    monkeypatch.setattr(port_reclaim.psutil, "process_iter", lambda _attrs: iter([_FakeProc(1, []), owned]))
    found = port_reclaim.find_agent_owned_appium(port=4723, runtime_root=RUNTIME_ROOT, exclude_pids=set())
    assert found is owned


def test_find_ignores_a_foreign_occupant(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(port_reclaim.psutil, "process_iter", lambda _attrs: iter([_FakeProc(4242, FOREIGN_CMDLINE)]))
    assert port_reclaim.find_agent_owned_appium(port=4723, runtime_root=RUNTIME_ROOT, exclude_pids=set()) is None


def test_find_accepts_appium_home_under_the_runtime_root(monkeypatch: pytest.MonkeyPatch) -> None:
    """A relocated binary is still ours when APPIUM_HOME points into the runtime root."""
    proc = _FakeProc(4242, FOREIGN_CMDLINE, environ={"APPIUM_HOME": f"{RUNTIME_ROOT}/appium-uiautomator2/1.2.3"})
    monkeypatch.setattr(port_reclaim.psutil, "process_iter", lambda _attrs: iter([proc]))
    assert port_reclaim.find_agent_owned_appium(port=4723, runtime_root=RUNTIME_ROOT, exclude_pids=set()) is proc


def test_find_never_returns_a_process_the_agent_tracks(monkeypatch: pytest.MonkeyPatch) -> None:
    owned = _FakeProc(4242, OWNED_CMDLINE)
    monkeypatch.setattr(port_reclaim.psutil, "process_iter", lambda _attrs: iter([owned]))
    assert port_reclaim.find_agent_owned_appium(port=4723, runtime_root=RUNTIME_ROOT, exclude_pids={4242}) is None


async def test_terminate_process_stops_at_sigterm() -> None:
    proc = _FakeProc(4242, OWNED_CMDLINE)
    await port_reclaim.terminate_process(proc)
    assert proc.terminated and not proc.killed


async def test_terminate_process_escalates_to_kill(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(port_reclaim, "TERMINATE_GRACE_SEC", 0.1)
    monkeypatch.setattr(port_reclaim, "KILL_GRACE_SEC", 0.1)
    proc = _FakeProc(4242, OWNED_CMDLINE, ignores_term=True)
    await asyncio.wait_for(port_reclaim.terminate_process(proc), timeout=2)
    assert proc.terminated and proc.killed
