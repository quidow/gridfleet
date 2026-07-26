"""Statement, commit, and lock-order budgets for one reconciler convergence cycle.

Phase 8 moved every Appium observation write into its own short Device-locked
transaction. This module pins what ``converge_pushed_host`` actually executes at
1, 10, and 50 devices when every device needs a real ``db_mark_running``
settlement, so a re-introduced N+1 or an extra transaction boundary fails here.

The pinned constants are MEASURED, not derived. ``FORMULA_MAX`` is the Phase 8
Global-Constraints ceiling (``8 + 8n``), asserted in full: the cycle fits inside
it with no exclusion and no netting.

The driver-pack catalog is read ONCE per host, before convergence, and carried
into every per-device settlement as a value projection
(``app.packs.services.catalog_view``). ``PACK_CATALOG_READS_PER_HOST`` pins that
read as constant in fleet size — it is the entire reason the per-device cost is
six statements and not nine.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import TYPE_CHECKING, Any
from unittest.mock import Mock

import pytest
from sqlalchemy import event

from app.appium_nodes.services.reconciler import ReconcilerService, converge_pushed_host
from app.core.timeutil import now_utc
from tests.bench_instrumentation import CommitTap, QueryTap, install_async_session_callsite_profiler
from tests.fakes import FakeSettingsReader
from tests.fold_fixtures import HOMOGENEOUS_FLEET, seed_fleet
from tests.helpers import test_event_bus as event_bus

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from tests.fold_fixtures import SeededDevice

pytestmark = pytest.mark.usefixtures("seeded_driver_packs")

FLEET_SIZES = (1, 10, 50)

# MEASURED on this branch, not derived. Captured inventory per cycle (n = fleet
# size, every device taking the db_mark_running settlement):
#   constant (6): 1 SELECT devices          (fetch_desired_rows_for_host)
#                 1 SELECT device_remediation_log (load_ladders)
#                 2 SELECT device_remediation_log (load_active_backoffs)
#                 1 SELECT driver_packs     (converge_pushed_host's per-host
#                                            catalog read, joinedload chain)
#                 1 UPDATE appium_nodes     (the batched last_observed_at touch)
#   per device (6): 1 SELECT devices FOR UPDATE       (lock_device_handle)
#                   1 SELECT device_intents           (snapshot claims)
#                   1 SELECT device_remediation_log   (snapshot ladder)
#                   1 SELECT appium_nodes FOR UPDATE  (lock_appium_node_for_device)
#                   1 UPDATE appium_nodes             (mark_node_started)
#                   1 INSERT system_events            (node.state_changed)
# => 6 + 6n statements and 1 + n commits. Lower these when a reduction lands;
# never raise one without attaching the inventory that explains the new statement.
RECONCILER_MAX = {1: 12, 10: 66, 50: 306}
RECONCILER_COMMITS = {1: 2, 10: 11, 50: 51}

# Phase 8 Global Constraints ceiling, asserted against the measurement in full.
# A count above it is a convergence defect, not a reason to raise the formula.
FORMULA_MAX = {n: 8 + 8 * n for n in FLEET_SIZES}
PACK_CATALOG_SIGNATURES = ("SELECT driver_packs", "SELECT driver_pack_releases", "SELECT driver_pack_platforms")
# The batched read: one statement for the whole host, at every fleet size. This
# is the assertion that fails if anyone re-introduces a per-device catalog
# resolve, whichever call site issues it.
PACK_CATALOG_READS_PER_HOST = 1

_WS = re.compile(r"\s+")


class _StatementLog:
    """Ordered ``before_cursor_execute`` recorder.

    ``QueryTap`` collapses statements to a verb+table signature, which drops the
    ``FOR UPDATE`` clause and the ordering this module asserts on. Recording the
    normalized statement plus its parameters keeps both.
    """

    def __init__(self) -> None:
        self.entries: list[tuple[str, object]] = []

    def __call__(
        self,
        conn: object,
        cursor: object,
        statement: str,
        parameters: object = None,
        context: object = None,
        executemany: bool = False,
    ) -> None:
        self.entries.append((_WS.sub(" ", statement.strip()), parameters))

    def _locks(self, table: str) -> list[tuple[int, object]]:
        return [
            (index, parameters)
            for index, (statement, parameters) in enumerate(self.entries)
            if f" FROM {table}" in statement and "FOR UPDATE" in statement
        ]

    def device_locks(self) -> list[tuple[int, object]]:
        return self._locks("devices")

    def node_locks(self) -> list[tuple[int, object]]:
        return self._locks("appium_nodes")


def _parameter_values(parameters: object) -> set[str]:
    if isinstance(parameters, dict):
        return {str(value) for value in parameters.values()}
    if isinstance(parameters, (list, tuple)):
        return {str(value) for value in parameters}
    return {str(parameters)}


def _payload(devices: list[SeededDevice]) -> dict[str, Any]:
    """Observed nodes that carry a ``started_at`` the seeded rows lack.

    That difference is what makes ``decide_convergence_action`` return
    ``db_mark_running`` for every device, so each one takes the per-device
    Device-locked settlement transaction this budget measures. Without it the
    cycle short-circuits at ``confirm_running`` and measures nothing per device.
    """
    started_at = now_utc().isoformat()
    return {
        "appium_processes": {
            "running_nodes": [
                {
                    "port": device.port,
                    "pid": device.pid,
                    "connection_target": device.identity,
                    "platform_id": device.spec.platform_id,
                    "started_at": started_at,
                }
                for device in devices
            ],
            "recent_restart_events": [],
            "start_failures": [],
        }
    }


def _reconciler(session_factory: async_sessionmaker[AsyncSession]) -> ReconcilerService:
    return ReconcilerService(
        publisher=event_bus,
        settings=FakeSettingsReader({}),
        pool=None,
        circuit_breaker=Mock(),
        session_factory=session_factory,
    )


async def _measure_cycle(
    session_factory: async_sessionmaker[AsyncSession],
    engine: Any,  # noqa: ANN401 - SQLAlchemy sync Engine, only ever handed to event.listen
    host_id: uuid.UUID,
    devices: list[SeededDevice],
) -> tuple[QueryTap, CommitTap, _StatementLog]:
    """Run one convergence cycle with the engine tap armed only around it.

    The listeners see every connection in the pool, so they go on after the
    fleet seed has committed and come off the moment the cycle returns.
    """
    tap = QueryTap()
    commits = CommitTap()
    log = _StatementLog()
    event.listen(engine, "before_cursor_execute", tap)
    event.listen(engine, "before_cursor_execute", log)
    event.listen(engine, "commit", commits)
    try:
        await converge_pushed_host(
            session_factory=session_factory,
            reconciler=_reconciler(session_factory),
            host_id=host_id,
            host_ip="10.0.0.10",
            agent_port=5100,
            payload=_payload(devices),
        )
    finally:
        event.remove(engine, "before_cursor_execute", tap)
        event.remove(engine, "before_cursor_execute", log)
        event.remove(engine, "commit", commits)
    return tap, commits, log


def _by_verb(tap: QueryTap) -> Counter[str]:
    grouped: Counter[str] = Counter()
    for signature, count in tap.counter.items():
        grouped[signature.split(maxsplit=1)[0]] += count
    return grouped


async def test_reconciler_cycle_statement_commit_and_lock_budget(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_async_session_callsite_profiler(monkeypatch)
    assert db_session.bind is not None
    engine = db_session.bind.sync_engine

    counts: dict[int, int] = {}
    commits: dict[int, int] = {}
    verbs: dict[int, Counter[str]] = {}
    inventory: dict[int, dict[str, int]] = {}
    for generation, size in enumerate(FLEET_SIZES):
        host, devices = await seed_fleet(db_session, HOMOGENEOUS_FLEET, size, generation=generation)
        tap, commit_tap, log = await _measure_cycle(db_session_maker, engine, host.id, devices)
        counts[size] = tap.total
        catalog_reads = sum(
            tap.callsite_counter[("app.packs.services.catalog_view.load_pack_catalog", signature)]
            for signature in PACK_CATALOG_SIGNATURES
        )
        # Pins the driver-pack catalog read as ONE constant statement for the
        # whole host cycle, not one per device: converge_pushed_host reads it
        # once and publishes it on the preloaded_pack_catalog ContextVar, so a
        # regression to the per-device fallback fails this at n=10 (10 reads)
        # and n=50 (50 reads), not just a raw-count creep.
        assert catalog_reads == PACK_CATALOG_READS_PER_HOST, (
            f"unexpected driver-pack catalog reads at {size} devices: {catalog_reads}"
        )
        commits[size] = commit_tap.count
        verbs[size] = _by_verb(tap)
        inventory[size] = {
            "desired_rows": tap.callsite_counter[
                ("app.appium_nodes.services.reconciler.fetch_desired_rows_for_host", "SELECT devices")
            ],
            "host_ladders": tap.callsite_counter[
                ("app.lifecycle.services.remediation_log.load_ladders", "SELECT device_remediation_log")
            ],
            "active_backoffs": tap.callsite_counter[
                ("app.lifecycle.services.remediation_log.load_active_backoffs", "SELECT device_remediation_log")
            ],
        }
        print(
            f"\nreconciler n={size}: statements={tap.total} (ceiling {FORMULA_MAX[size]}) "
            f"commits={commit_tap.count} verbs={dict(verbs[size])}"
        )
        print(f"    inventory={inventory[size]}")
        for signature, count in tap.counter.most_common():
            print(f"    {count:5d}  {signature}")

        device_locks = log.device_locks()
        node_locks = log.node_locks()
        assert len(device_locks) == size, (
            f"{len(device_locks)} Device FOR UPDATE statements for {size} changed devices: exactly one each"
        )
        assert len(node_locks) == size, "every changed device must settle its own AppiumNode row"
        # Children never precede their parent: at no point in the cycle has a
        # node lock been taken that its device lock did not already cover.
        device_lock_positions = {
            value: index for index, parameters in device_locks for value in _parameter_values(parameters)
        }
        for index, parameters in node_locks:
            owners = _parameter_values(parameters) & set(device_lock_positions)
            assert owners, f"AppiumNode lock at statement {index} has no matching Device lock"
            for owner in owners:
                assert device_lock_positions[owner] < index, f"AppiumNode lock for {owner} preceded its Device lock"

    # The harness must be measuring something: a mis-shaped payload that decided
    # confirm_running for every row would pin a meaningless constant.
    assert counts[1] > 0, "the tap counted no statements at all"
    assert counts[50] > counts[1], "the cycle must do more work for 50 devices than for 1"

    # Host-level inventory reads do not grow with fleet size.
    assert (
        inventory[1]
        == inventory[10]
        == inventory[50]
        == {
            "desired_rows": 1,
            "host_ladders": 1,
            "active_backoffs": 2,
        }
    )

    for size in FLEET_SIZES:
        assert counts[size] <= RECONCILER_MAX[size], (
            f"reconciler cycle at {size} devices issued {counts[size]} statements, above the pinned "
            f"{RECONCILER_MAX[size]}: attach a captured statement inventory before raising this"
        )
        assert counts[size] <= FORMULA_MAX[size], (
            f"reconciler cycle at {size} devices issued {counts[size]} statements, above the Phase 8 "
            f"ceiling {FORMULA_MAX[size]} — fix the implementation, do not raise the formula"
        )
    assert counts[10] - counts[1] <= 9 * 8
    assert counts[50] - counts[10] <= 40 * 8

    # One batched last_observed_at touch plus one per-device command boundary.
    assert commits == RECONCILER_COMMITS
    for size in FLEET_SIZES:
        assert commits[size] == 1 + size

    # No statement category may grow faster than the eight-per-device term.
    for verb in set(verbs[1]) | set(verbs[10]) | set(verbs[50]):
        assert verbs[10][verb] - verbs[1][verb] <= 9 * 8, f"{verb} grew faster than 8/device between 1 and 10 devices"
        assert verbs[50][verb] - verbs[10][verb] <= 40 * 8, (
            f"{verb} grew faster than 8/device between 10 and 50 devices"
        )
