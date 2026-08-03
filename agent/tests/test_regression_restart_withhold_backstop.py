"""The first-restart withhold is bounded by wall clock, not only by its owner.

``process_snapshot`` truncates the emitted restart-event stream at the lowest
withheld sequence and that cursor is host-wide, so a withhold nobody discharges
mutes restart events for every port on the host, forever. Task ownership alone
cannot bound it: a path that hands the withhold off to an actor that never
arrives leaves no owner at all.

The bound is absolute from arming and never re-armed. Expiry governs
*publication* only — the record survives, so a recovery that completes late
still records its resolving event and closes the audit trail.
"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import patch

import pytest

from agent_app.appium.process import FIRST_RESTART_WITHHOLD_MAX_SEC
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

pytestmark = pytest.mark.asyncio


async def test_arming_applies_the_backstop_constant() -> None:
    """Pins the constant to the arming site, so the two cannot drift apart —
    every other test here reaches expiry by moving ``expires_at`` directly."""
    mgr = manager_with_crashed_node()
    await restart_task_in_backoff(mgr)

    armed_at = asyncio.get_running_loop().time()
    record = mgr._withheld_restart_by_port[PORT]
    assert record.expires_at == pytest.approx(armed_at + FIRST_RESTART_WITHHOLD_MAX_SEC, abs=1.0)


async def test_an_expired_withhold_stops_suppressing_the_crash() -> None:
    """The backstop's whole purpose: a recovery that never completes must not
    keep the host's restart-event stream truncated indefinitely."""
    mgr = manager_with_crashed_node()
    await restart_task_in_backoff(mgr)

    snapshot = await mgr.process_snapshot()
    assert kinds(snapshot) == [], "the withhold was not suppressing the crash to begin with"

    mgr._withheld_restart_by_port[PORT].expires_at -= FIRST_RESTART_WITHHOLD_MAX_SEC + 1

    snapshot = await mgr.process_snapshot()
    assert kinds(snapshot) == ["crash_detected"], (
        f"an expired withhold still muted the host's restart events: {kinds(snapshot)}"
    )
    assert snapshot["running_nodes"] == [], "an expired withhold still retained a dead node as coalesced"


async def test_expiry_is_logged_once(caplog: pytest.LogCaptureFixture) -> None:
    """Forensics: the expiry boundary must be timestampable in a log bundle,
    and must not then flood every subsequent push."""
    mgr = manager_with_crashed_node()
    await restart_task_in_backoff(mgr)
    mgr._withheld_restart_by_port[PORT].expires_at -= FIRST_RESTART_WITHHOLD_MAX_SEC + 1

    with caplog.at_level(logging.WARNING, logger="agent_app.appium.process"):
        await mgr.process_snapshot()
        await mgr.process_snapshot()

    expiries = [record for record in caplog.records if "withhold expired" in record.getMessage()]
    assert len(expiries) == 1, f"expected exactly one expiry log line, got {len(expiries)}"


@pytest.mark.xfail(reason="start() adoption widening lands in Task 3", strict=True)
async def test_expiry_is_not_disownership(stub_port_probe: None) -> None:
    """Expiry bounds publication, not ownership. A deferral that clears after
    the deadline must still pair its crash with a resolving event rather than
    leaving a dangling ``crash_detected`` the backend folds to offline."""
    mgr = manager_with_crashed_node()
    task = await restart_task_in_backoff(mgr)
    task.cancel()
    await settle()
    mgr._appium_restart_tasks.pop(PORT, None)
    mgr._withheld_restart_by_port[PORT].expires_at -= FIRST_RESTART_WITHHOLD_MAX_SEC + 1

    async def fake_spawn(*_args: object, **_kwargs: object) -> FakeProcess:
        return FakeProcess(RESPAWNED_PID)

    async def ready(_port: int, _proc: object) -> bool:
        return True

    with (
        patch.object(mgr, "_start_appium_server", side_effect=fake_spawn),
        patch.object(mgr, "_wait_for_readiness", new=ready),
    ):
        await mgr.start(connection_target=TARGET, port=PORT, **PACK_START_KWARGS)
    await settle()

    assert mgr._withheld_restart_by_port == {}
    with patch.object(mgr, "_node_has_active_session", return_value=False):
        snapshot = await mgr.process_snapshot()
    assert kinds(snapshot) == ["crash_detected", "restart_succeeded"], (
        f"an expired withhold was treated as disowned, so the crash never got its pair: {kinds(snapshot)}"
    )
