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


def _desired(pack_id: str) -> DesiredPack:
    return DesiredPack(
        id=pack_id,
        release=RELEASE,
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
