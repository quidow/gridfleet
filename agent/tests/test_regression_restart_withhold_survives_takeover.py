"""S04c: the first-restart withhold must survive a superseding ``start()``.

The node-state convergence loop sees a crashed port missing from
``list_running()`` and calls ``start()`` for it. ``start()`` unconditionally
cancels that port's auto-restart task, and the cancel lands on the task's
backoff ``asyncio.sleep``. ``CancelledError`` is a ``BaseException``, so it
escapes every ``except`` clause silently while still running the ``finally``
that released the withheld first-attempt observation. The next status push then
carried a lone ``crash_detected`` with ``will_retry=true`` and no successor,
which the backend folds to ``health_running=False`` / ``health_state
="restarting"`` — the device left ``available`` even though Appium was already
coming back up on the same port.

The cancel is a handoff, not an abandonment: the ``start()`` that cancelled the
task finishes the respawn, so it also adopts the withhold and discharges it.
The counterpart hazard is a withhold nobody discharges — ``process_snapshot``
truncates the emitted restart-event stream at the lowest withheld sequence and
that cursor is host-wide, so a leaked withhold would mute restart events for
every port on the host. These tests pin both halves.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import patch

import pytest

from agent_app.appium.exceptions import StartupTimeoutError
from agent_app.appium.process import (
    AppiumInvocation,
    AppiumLaunchSpec,
    AppiumProcessInfo,
    AppiumProcessManager,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

pytestmark = pytest.mark.asyncio

PORT = 4723
TARGET = "device-001"
PACK_START_KWARGS = {"pack_id": "appium-uiautomator2", "platform_id": "android_mobile"}
_STUB_INVOCATION = AppiumInvocation(binary="/usr/local/bin/appium")
CRASHED_PID = 9001
RESPAWNED_PID = 9002


class _FakeProcess:
    """A child that stays alive until the test says otherwise."""

    def __init__(self, pid: int, returncode: int | None = None) -> None:
        self.pid = pid
        self.returncode = returncode
        self._exited = asyncio.Event()

    async def wait(self) -> int:
        if self.returncode is not None:
            return self.returncode
        await self._exited.wait()
        return self.returncode if self.returncode is not None else 0


@pytest.fixture
def stub_port_probe() -> Iterator[None]:
    with (
        patch("agent_app.appium.process.AppiumProcessManager._can_connect_to_appium", new=_never_connects),
        patch("agent_app.appium.process.AppiumProcessManager._is_appium_port_bindable", return_value=True),
    ):
        yield


async def _never_connects(_self: object, _port: int) -> bool:
    return False


def _manager_with_crashed_node() -> AppiumProcessManager:
    """A manager whose Appium on PORT has just exited, bookkeeping intact."""
    mgr = AppiumProcessManager()
    mgr._appium_procs[PORT] = cast("asyncio.subprocess.Process", _FakeProcess(CRASHED_PID, returncode=1))
    mgr._launch_specs[PORT] = AppiumLaunchSpec(
        connection_target=TARGET,
        port=PORT,
        extra_caps=None,
        session_override=False,
        device_type="real_device",
        ip_address="10.0.0.1",
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        appium_platform_name=None,
        appium_env=None,
        insecure_features=[],
        lifecycle_actions=[],
        connection_behavior={},
    )
    mgr._info[PORT] = AppiumProcessInfo(
        port=PORT,
        pid=CRASHED_PID,
        connection_target=TARGET,
        platform_id="android_mobile",
    )
    return mgr


async def _restart_task_in_backoff(mgr: AppiumProcessManager) -> asyncio.Task[None]:
    """Drive the real auto-restart task to its first-attempt backoff sleep."""
    task = asyncio.create_task(mgr._auto_restart_appium(PORT, 1))
    mgr._register_port_task(mgr._appium_restart_tasks, PORT, task)
    await asyncio.sleep(0)
    assert mgr._first_restart_observation_ports == {PORT}, "the first attempt did not enter withholding"
    assert not task.done()
    return task


async def _settle(times: int = 5) -> None:
    for _ in range(times):
        await asyncio.sleep(0)


def _kinds(snapshot: dict[str, Any]) -> list[str]:
    return [event["kind"] for event in snapshot["recent_restart_events"]]


async def test_superseding_start_does_not_release_the_withhold_while_it_respawns(
    stub_port_probe: None,
) -> None:
    """The defect: the convergence loop's ``start()`` cancels the sleeping
    restart task, and the withheld crash must not become publishable while that
    start is still bringing the port back up."""
    mgr = _manager_with_crashed_node()
    restart_task = await _restart_task_in_backoff(mgr)

    readiness_entered = asyncio.Event()
    release_readiness = asyncio.Event()

    async def gated_readiness(_port: int, _proc: object) -> bool:
        readiness_entered.set()
        await release_readiness.wait()
        return True

    async def fake_spawn(*_args: object, **_kwargs: object) -> _FakeProcess:
        return _FakeProcess(RESPAWNED_PID)

    with (
        patch("agent_app.appium.process.resolve_appium_invocation_for_pack", return_value=_STUB_INVOCATION),
        patch("agent_app.appium.process.build_env", return_value={"PATH": "/usr/bin"}),
        patch.object(mgr, "_wait_for_readiness", new=gated_readiness),
        patch("agent_app.appium.process.asyncio.create_subprocess_exec", side_effect=fake_spawn),
    ):
        takeover = asyncio.create_task(mgr.start(connection_target=TARGET, port=PORT, **PACK_START_KWARGS))
        await asyncio.wait_for(readiness_entered.wait(), timeout=2)
        await _settle()

        assert restart_task.cancelled(), "the superseding start did not cancel the sleeping restart task"
        assert mgr._first_restart_observation_ports == {PORT}, (
            "the cancelled restart task released the withhold; the crash is publishable "
            "while the takeover start is still respawning the port"
        )

        snapshot = await mgr.process_snapshot()
        assert _kinds(snapshot) == [], f"a node-down observation escaped mid-takeover: {_kinds(snapshot)}"
        coalesced = [node for node in snapshot["running_nodes"] if node["port"] == PORT]
        assert len(coalesced) == 1
        assert coalesced[0]["observation_coalesced"] is True

        release_readiness.set()
        await asyncio.wait_for(takeover, timeout=2)


async def test_takeover_start_discharges_the_withhold_once_the_respawn_completes(
    stub_port_probe: None,
) -> None:
    """No permanent leak: the adopting ``start()`` releases the withhold on
    completion, and the deferred crash is emitted then — paired with the
    resolving event, so the pair folds to a no-op instead of an offline edge."""
    mgr = _manager_with_crashed_node()
    await _restart_task_in_backoff(mgr)

    async def fake_spawn(*_args: object, **_kwargs: object) -> _FakeProcess:
        return _FakeProcess(RESPAWNED_PID)

    async def ready(_port: int, _proc: object) -> bool:
        return True

    with (
        patch("agent_app.appium.process.resolve_appium_invocation_for_pack", return_value=_STUB_INVOCATION),
        patch("agent_app.appium.process.build_env", return_value={"PATH": "/usr/bin"}),
        patch.object(mgr, "_wait_for_readiness", new=ready),
        patch("agent_app.appium.process.asyncio.create_subprocess_exec", side_effect=fake_spawn),
    ):
        info = await asyncio.wait_for(mgr.start(connection_target=TARGET, port=PORT, **PACK_START_KWARGS), timeout=2)
        await _settle()

    assert info.pid == RESPAWNED_PID
    assert mgr._first_restart_observation_ports == set(), "the withhold outlived the start that adopted it"
    assert mgr._withheld_restart_sequence_by_port == {}

    with patch.object(mgr, "_node_has_active_session", return_value=False):
        snapshot = await mgr.process_snapshot()
    assert _kinds(snapshot) == ["crash_detected", "restart_succeeded"], (
        f"the deferred events were not released as a resolved pair: {_kinds(snapshot)}"
    )
    nodes = [node for node in snapshot["running_nodes"] if node["port"] == PORT]
    assert len(nodes) == 1
    assert nodes[0]["pid"] == RESPAWNED_PID
    assert not nodes[0].get("observation_coalesced")

    # The adopted attempt is charged to the port's restart history, so a port
    # that crashes again does not re-enter attempt 1 and coalesce forever.
    assert len(mgr._appium_restart_attempts[PORT]) == 1


async def test_failed_takeover_start_releases_and_leaves_the_crash_auditable() -> None:
    """A genuinely dead Appium still reports: when the adopting start fails,
    the withhold is discharged with no resolving event, so the crash publishes
    and the device goes offline as it should."""
    mgr = _manager_with_crashed_node()
    await _restart_task_in_backoff(mgr)

    async def failing_spawn(*_args: object, **_kwargs: object) -> object:
        raise StartupTimeoutError(f"Appium on port {PORT} did not become ready")

    with patch.object(mgr, "_start_appium_server", new=failing_spawn), pytest.raises(StartupTimeoutError):
        await asyncio.wait_for(mgr.start(connection_target=TARGET, port=PORT, **PACK_START_KWARGS), timeout=2)
    await _settle()

    assert mgr._first_restart_observation_ports == set(), "a failed takeover leaked the withhold host-wide"
    assert mgr._withheld_restart_sequence_by_port == {}

    snapshot = await mgr.process_snapshot()
    assert _kinds(snapshot) == ["crash_detected"], f"the crash was not auditable: {_kinds(snapshot)}"
    assert snapshot["recent_restart_events"][0]["will_retry"] is True
    assert snapshot["running_nodes"] == [], "a dead port must not be retained once the withhold is discharged"


async def test_the_restart_tasks_own_start_does_not_adopt_its_own_withhold(
    stub_port_probe: None,
) -> None:
    """``start()`` adopts only what it actually superseded.

    The auto-restart task reaches ``start()`` through its own call chain, where
    ``_cancel_task`` is self-cancel-exempt: nothing was handed over, and the
    task resolves its own withhold on the way out. If ``start()`` adopted on the
    withhold alone -- without requiring that a foreign task was really cancelled
    -- it would emit a second ``restart_succeeded`` and charge the attempt twice.
    """
    mgr = _manager_with_crashed_node()

    original_sleep = asyncio.sleep

    async def instant_backoff(_delay: float) -> None:
        await original_sleep(0)

    async def fake_spawn(*_args: object, **_kwargs: object) -> _FakeProcess:
        return _FakeProcess(RESPAWNED_PID)

    async def ready(_port: int, _proc: object) -> bool:
        return True

    with (
        pytest.MonkeyPatch.context() as mp,
        patch("agent_app.appium.process.resolve_appium_invocation_for_pack", return_value=_STUB_INVOCATION),
        patch("agent_app.appium.process.build_env", return_value={"PATH": "/usr/bin"}),
        patch.object(mgr, "_wait_for_readiness", new=ready),
        patch("agent_app.appium.process.asyncio.create_subprocess_exec", side_effect=fake_spawn),
    ):
        mp.setattr(asyncio, "sleep", instant_backoff)
        # Registered exactly as production does, so ``_cancel_task``'s identity
        # check really sees the running task as the current one.
        task = asyncio.create_task(mgr._auto_restart_appium(PORT, 1))
        mgr._register_port_task(mgr._appium_restart_tasks, PORT, task)
        await asyncio.wait_for(task, timeout=2)

    assert mgr._first_restart_observation_ports == set()
    with patch.object(mgr, "_node_has_active_session", return_value=False):
        snapshot = await mgr.process_snapshot()
    assert _kinds(snapshot) == ["crash_detected", "restart_succeeded"], (
        f"the restart task's own start() adopted a withhold it never superseded: {_kinds(snapshot)}"
    )
    assert len(mgr._appium_restart_attempts[PORT]) == 1, "the attempt was charged twice for one restart"


async def test_discharge_releases_the_withhold_even_if_recording_the_pair_fails() -> None:
    """The release must not sit behind bookkeeping that could raise.

    A skipped release strands ``process_snapshot``'s host-wide ``truncate_at``
    cursor and mutes restart events for every port on the host -- far worse than
    the single false offline a missing resolving event costs.
    """
    mgr = _manager_with_crashed_node()
    await _restart_task_in_backoff(mgr)

    async def fake_spawn(*_args: object, **_kwargs: object) -> _FakeProcess:
        return _FakeProcess(RESPAWNED_PID)

    def exploding_record(**_kwargs: object) -> int:
        raise RuntimeError("restart-event ring is wedged")

    with (
        patch.object(mgr, "_start_appium_server", side_effect=fake_spawn),
        patch.object(mgr, "_record_restart_event", side_effect=exploding_record),
        pytest.raises(RuntimeError),
    ):
        await asyncio.wait_for(mgr.start(connection_target=TARGET, port=PORT, **PACK_START_KWARGS), timeout=2)
    await _settle()

    assert mgr._first_restart_observation_ports == set(), (
        "a raising resolving-event record stranded the host-wide truncation cursor"
    )
    assert mgr._withheld_restart_sequence_by_port == {}


async def test_stop_during_backoff_releases_the_withhold_before_cancelling() -> None:
    """The abandonment half of the invariant: ``stop()`` cancels the same task
    but nothing takes the respawn over, so it must lift the withhold itself."""
    mgr = _manager_with_crashed_node()
    restart_task = await _restart_task_in_backoff(mgr)

    await asyncio.wait_for(mgr.stop(PORT), timeout=2)
    await _settle()

    assert restart_task.cancelled()
    assert mgr._first_restart_observation_ports == set(), "an abandoned restart left its withhold in place"
    assert mgr._withheld_restart_sequence_by_port == {}

    snapshot = await mgr.process_snapshot()
    assert _kinds(snapshot) == ["crash_detected"]
