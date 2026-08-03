"""A deferred start hands the withhold off; it does not abandon it.

``_auto_restart_appium``'s ``StartDeferredError`` branch returns because the
condition is transient and the node-state convergence loop retries ``start()``
next tick. Releasing the withhold on the way out published a bare
``crash_detected`` with ``will_retry=true`` and no successor, which the backend
folds to ``health_running=False`` / ``health_state="restarting"`` -- a false
offline for a port the agent fully intends to recover.

The successor is a later ``start()``, not a later task. So ``start()`` adopts a
withhold that no live restart task owns, and the wall-clock backstop covers the
case where no successor ever arrives.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from agent_app.appium.exceptions import StartDeferredError
from agent_app.pack.adapter_registry import AdapterRegistry
from agent_app.pack.manifest import DesiredPack, DesiredPlatform
from agent_app.pack.runtime_types import AppiumInstallable
from tests.pack.fake_worker import FakeWorkerHandle
from tests.withhold_helpers import (
    PACK_START_KWARGS,
    PORT,
    RESPAWNED_PID,
    TARGET,
    FakeProcess,
    kinds,
    manager_with_crashed_node,
    restart_task_in_backoff,
    settle,
)
from tests.withhold_helpers import (
    stub_port_probe as stub_port_probe,  # pytest fixture, used by name
)

if TYPE_CHECKING:
    from agent_app.appium.process import AppiumProcessManager

pytestmark = pytest.mark.asyncio

PACK_ID = "appium-uiautomator2"
RELEASE = "2026.07.1"
NEW_RELEASE = "2026.07.2"


class _Adapter:
    async def pre_session(self, spec: object) -> dict[str, object]:
        return {}


def _wire_packs(mgr: AppiumProcessManager, *, desired_ids: list[str]) -> None:
    """Point the manager at a real adapter registry and desired-pack list.

    An empty or non-matching ``desired_ids`` is the production trigger for
    ``StartDeferredError``: the pack was disabled, retired, or the node poll and
    the pack poll have not converged yet.
    """
    registry = AdapterRegistry()
    registry.set(PACK_ID, RELEASE, FakeWorkerHandle(_Adapter(), pack_id=PACK_ID, release=RELEASE))  # type: ignore[arg-type]
    mgr.set_adapter_registry(registry)
    mgr.set_desired_packs_provider(lambda: [_desired(pack_id) for pack_id in desired_ids])


def _desired(pack_id: str, *, release: str = RELEASE) -> DesiredPack:
    return DesiredPack(
        id=pack_id,
        release=release,
        appium_server=AppiumInstallable(
            source="npm", package="appium", version="==3.3.1", recommended=None, known_bad=[]
        ),
        appium_driver=AppiumInstallable(
            source="npm", package="appium-uiautomator2-driver", version="==5.0.0", recommended=None, known_bad=[]
        ),
        platforms=[
            DesiredPlatform(
                id="android_mobile",
                automation_name="UiAutomator2",
                device_types=["real_device", "emulator"],
                connection_types=["usb", "network"],
                identity_scheme="adb_serial",
                identity_scope="host",
                stereotype={},
                lifecycle_actions=[],
            )
        ],
        tarball_sha256="a" * 64,
    )


async def _instant(_delay: float) -> None:
    await asyncio.sleep(0)


async def _spawn(*_args: object, **_kwargs: object) -> FakeProcess:
    return FakeProcess(RESPAWNED_PID)


async def _ready(_port: int, _proc: object) -> bool:
    return True


async def test_a_deferred_start_keeps_the_withhold(stub_port_probe: None) -> None:
    """The defect. The branch's own comment says the convergence loop will
    retry, so the crash must stay coalesced until it does."""
    mgr = manager_with_crashed_node()
    _wire_packs(mgr, desired_ids=[])  # pack absent from the desired list -> deferral

    task = await restart_task_in_backoff(mgr)
    with patch("agent_app.appium.process.asyncio.sleep", new=_instant):
        await asyncio.wait_for(task, timeout=2)

    assert set(mgr._withheld_restart_by_port) == {PORT}, (
        "the deferred branch released the withhold; the crash is publishable "
        "while the convergence loop is still going to retry"
    )
    snapshot = await mgr.process_snapshot()
    assert kinds(snapshot) == [], f"a node-down observation escaped a deferral: {kinds(snapshot)}"


async def test_a_later_start_adopts_the_withhold_no_task_owns(stub_port_probe: None) -> None:
    """The convergence loop's retry is the successor the deferred branch names.
    It must record the resolving event, not merely stop suppressing."""
    mgr = manager_with_crashed_node()
    _wire_packs(mgr, desired_ids=[])

    task = await restart_task_in_backoff(mgr)
    with patch("agent_app.appium.process.asyncio.sleep", new=_instant):
        await asyncio.wait_for(task, timeout=2)
    assert PORT not in mgr._appium_restart_tasks, "the deferred task should have finished and deregistered"

    _wire_packs(mgr, desired_ids=[PACK_ID])  # the pack list converged

    with (
        patch.object(mgr, "_start_appium_server", side_effect=_spawn),
        patch.object(mgr, "_wait_for_readiness", new=_ready),
    ):
        info = await asyncio.wait_for(mgr.start(connection_target=TARGET, port=PORT, **PACK_START_KWARGS), timeout=2)
    await settle()

    assert info.pid == RESPAWNED_PID
    assert mgr._withheld_restart_by_port == {}, "the adopting start did not discharge the withhold"
    with patch.object(mgr, "_node_has_active_session", return_value=False):
        snapshot = await mgr.process_snapshot()
    assert kinds(snapshot) == ["crash_detected", "restart_succeeded"], (
        f"the deferred events were not released as a resolved pair: {kinds(snapshot)}"
    )


class _FlippingPreSessionAdapter:
    """Flips the desired release out from under the start it is serving.

    ``pre_session`` runs before the start lock is taken, so mutating the
    desired-packs provider here lands exactly between the pre-lock resolve and
    the locked revalidation -- the same race a pack-loop reconcile produces in
    production.
    """

    def __init__(self, mgr: AppiumProcessManager) -> None:
        self._mgr = mgr

    async def pre_session(self, spec: object) -> dict[str, object]:
        self._mgr.set_desired_packs_provider(lambda: [_desired(PACK_ID, release=NEW_RELEASE)])
        return {}


async def test_start_deferred_at_locked_revalidation_keeps_an_adopted_withhold(stub_port_probe: None) -> None:
    """The LOCKED revalidation's ``StartDeferredError`` must not discharge a
    withhold this same ``start()`` adopted.

    Setup mirrors ``test_a_later_start_adopts_the_withhold_no_task_owns``: a
    withhold armed and handed off by a deferred auto-restart attempt, with no
    live task left owning it. The difference is what happens next -- instead
    of a clean respawn, this adopting ``start()`` itself defers, at the locked
    revalidation specifically (the pre-lock resolve raises before
    ``_cancel_task`` runs, so nothing is adopted yet at that point). Releasing
    the withhold anyway publishes a bare ``crash_detected`` with no successor,
    which is exactly the false-offline payload this whole mechanism exists to
    suppress.
    """
    mgr = manager_with_crashed_node()
    _wire_packs(mgr, desired_ids=[])  # pack absent -> the auto-restart task's own start() defers

    task = await restart_task_in_backoff(mgr)
    with patch("agent_app.appium.process.asyncio.sleep", new=_instant):
        await asyncio.wait_for(task, timeout=2)
    assert set(mgr._withheld_restart_by_port) == {PORT}, "setup: withhold must still be armed"
    assert PORT not in mgr._appium_restart_tasks, "setup: no task may own the withhold"

    # Converge the pack list so a direct start() adopts the withhold, but
    # register both releases so the locked revalidation resolves cleanly
    # (rather than raising from inside _resolve_pack_worker) and the explicit
    # release-mismatch check is what fires.
    registry = AdapterRegistry()
    flipping_handle = FakeWorkerHandle(_FlippingPreSessionAdapter(mgr), pack_id=PACK_ID, release=RELEASE)
    new_release_handle = FakeWorkerHandle(_Adapter(), pack_id=PACK_ID, release=NEW_RELEASE)
    registry.set(PACK_ID, RELEASE, flipping_handle)  # type: ignore[arg-type]
    registry.set(PACK_ID, NEW_RELEASE, new_release_handle)  # type: ignore[arg-type]
    mgr.set_adapter_registry(registry)
    mgr.set_desired_packs_provider(lambda: [_desired(PACK_ID)])

    with pytest.raises(StartDeferredError):
        await asyncio.wait_for(mgr.start(connection_target=TARGET, port=PORT, **PACK_START_KWARGS), timeout=2)

    assert set(mgr._withheld_restart_by_port) == {PORT}, (
        "the locked-revalidation deferral discharged an adopted withhold with no resolving event recorded"
    )
    snapshot = await mgr.process_snapshot()
    assert kinds(snapshot) == [], f"a node-down observation escaped a locked-revalidation deferral: {kinds(snapshot)}"


async def test_adoption_does_not_charge_the_attempt_twice(stub_port_probe: None) -> None:
    """The deferred path already charged the attempt before calling start().

    Charging again on adoption would trip AUTO_RESTART_MAX_ATTEMPTS an attempt
    early inside the 300 s window, so a port that defers and recovers a few
    times would exhaust its retries without ever having failed one.
    """
    mgr = manager_with_crashed_node()
    _wire_packs(mgr, desired_ids=[])

    task = await restart_task_in_backoff(mgr)
    with patch("agent_app.appium.process.asyncio.sleep", new=_instant):
        await asyncio.wait_for(task, timeout=2)
    assert len(mgr._appium_restart_attempts[PORT]) == 1, "the deferred attempt was not charged"

    _wire_packs(mgr, desired_ids=[PACK_ID])
    with (
        patch.object(mgr, "_start_appium_server", side_effect=_spawn),
        patch.object(mgr, "_wait_for_readiness", new=_ready),
    ):
        await asyncio.wait_for(mgr.start(connection_target=TARGET, port=PORT, **PACK_START_KWARGS), timeout=2)
    await settle()

    assert len(mgr._appium_restart_attempts[PORT]) == 1, "one restart was charged as two attempts"


async def test_a_deferred_exit_records_its_reason(caplog: pytest.LogCaptureFixture) -> None:
    """Every exit from an auto-restart attempt names itself and says what
    happened to the withhold, so a release is never again attributable only by
    absence of evidence."""
    mgr = manager_with_crashed_node()
    _wire_packs(mgr, desired_ids=[])

    with caplog.at_level(logging.INFO, logger="agent_app.appium.process"):
        task = await restart_task_in_backoff(mgr)
        with patch("agent_app.appium.process.asyncio.sleep", new=_instant):
            await asyncio.wait_for(task, timeout=2)

    ended = [record.getMessage() for record in caplog.records if "auto-restart attempt ended" in record.getMessage()]
    assert len(ended) == 1, f"expected one exit-reason line, got {ended}"
    assert "reason=deferred" in ended[0]
    assert "withhold=retained" in ended[0]


async def test_an_operator_stop_during_backoff_records_its_reason(caplog: pytest.LogCaptureFixture) -> None:
    """``stop()`` is fully silent today, and it is the one alternative the
    original diagnosis could only eliminate by reasoning about side effects."""
    mgr = manager_with_crashed_node()
    await restart_task_in_backoff(mgr)

    with caplog.at_level(logging.INFO, logger="agent_app.appium.process"):
        await asyncio.wait_for(mgr.stop(PORT, reason="needs_restart"), timeout=2)

    stops = [record.getMessage() for record in caplog.records if "Stopping Appium node" in record.getMessage()]
    assert len(stops) == 1, f"expected one stop line, got {stops}"
    assert "reason=needs_restart" in stops[0]


async def test_shutdown_stops_every_port_with_its_own_reason(caplog: pytest.LogCaptureFixture) -> None:
    """``shutdown()`` drains every managed port through the same ``stop()``
    path; the reason it passes must say "shutdown", not silently default,
    or a fleet-wide teardown reads identically to an individual operator
    stop in the forensic trail."""
    mgr = manager_with_crashed_node()

    with caplog.at_level(logging.INFO, logger="agent_app.appium.process"):
        await asyncio.wait_for(mgr.shutdown(), timeout=2)

    stops = [record.getMessage() for record in caplog.records if "Stopping Appium node" in record.getMessage()]
    assert len(stops) == 1, f"expected one stop line, got {stops}"
    assert "reason=shutdown" in stops[0]


async def test_shutdown_discharges_a_withhold_it_cannot_stop() -> None:
    """``shutdown()`` cancels restart tasks directly rather than through
    ``_forget_port``. Its ``stop()`` sweep covers every port it knows about, so
    a withhold whose port is in neither map is the gap. Nothing is pushed after
    a shutdown, so this is structural: no path may cancel a restart task
    without discharging the withhold that task owned."""
    mgr = manager_with_crashed_node()
    restart_task = await restart_task_in_backoff(mgr)

    # A port the stop sweep cannot reach: bookkeeping already dropped, withhold
    # and task still live.
    mgr._appium_procs.pop(PORT)
    mgr._launch_specs.pop(PORT)

    await asyncio.wait_for(mgr.shutdown(), timeout=2)
    await settle()

    assert restart_task.cancelled()
    assert mgr._withheld_restart_by_port == {}, "shutdown cancelled a restart task and left its withhold armed"
