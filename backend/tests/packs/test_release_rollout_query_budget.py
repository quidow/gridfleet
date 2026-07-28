"""Phase 10 task 6: release-rollout stage driver-pack query budget.

Before this change, ``run_release_rollout_stage`` resolved the selected
release per DISTINCT pack via ``selected_release_id`` -- one full statement
(plus its ``selectinload`` follow-up) per pack, called from a dict
comprehension over every pack id the fleet referenced. A fleet where every
device runs its own pack made that scale with the number of distinct packs.
The fix batches the read into one ``DriverPack.id.in_(...)`` query (plus one
``selectinload`` follow-up for releases) -- constant regardless of how many
distinct packs are in play.

The measurement is scoped to the *inventory* read (everything before the
first per-candidate Device lock): the per-candidate reconcile path
(``IntentService`` -> the intent reconciler) does its own, separate,
pre-existing driver-pack catalog fallback read per device when no catalog is
threaded in -- that is unrelated to this task's slice (only
``release_rollout.py`` is in scope here) and would otherwise swamp the signal
this test exists to catch.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import event

from app.appium_nodes.models import AppiumDesiredState, AppiumNode
from app.packs.models import DriverPack, DriverPackRelease
from app.packs.services.release_rollout import run_release_rollout_stage
from tests.helpers import create_device
from tests.helpers import test_event_bus as event_bus

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.hosts.models import Host

FLEET_SIZES = (1, 10, 50)


async def _seed_one_pack_per_device(db_session: AsyncSession, host: Host, n: int, generation: int) -> None:
    """*n* devices, each on its own never-before-seen pack with one release.

    One pack per device is deliberate: it is what turns "one query per
    DISTINCT pack" into "one query per device" for the purpose of this
    measurement, without which the old and new implementations would look
    identical at every fleet size (both already dedupe repeated pack ids).
    """
    for i in range(n):
        pack_id = f"budget-pack-{generation}-{i}"
        db_session.add(DriverPack(id=pack_id, display_name=pack_id, current_release=None))
        db_session.add(DriverPackRelease(pack_id=pack_id, release="2.0.0", manifest_json={}))
        await db_session.flush()
        device = await create_device(
            db_session,
            host_id=host.id,
            name=f"budget-device-{generation}-{i}",
            pack_id=pack_id,
            platform_id="ios",
        )
        db_session.add(
            AppiumNode(
                device_id=device.id,
                port=4723,
                pid=100 + i,
                active_connection_target=device.connection_target,
                observed_pack_release="1.0.0",
                desired_state=AppiumDesiredState.running,
                desired_port=4723,
            )
        )
    await db_session.commit()


def _is_device_lock(statement: str) -> bool:
    """The per-candidate ``SELECT devices ... FOR UPDATE`` -- never the plain
    (unlocked) inventory join, which shares the same table but no lock clause."""
    return "FROM devices" in statement and "FOR UPDATE" in statement


def _is_driver_pack_read(statement: str) -> bool:
    return any(f"FROM {table}" in statement for table in ("driver_packs", "driver_pack_releases"))


async def test_pack_release_inventory_query_count_is_constant_in_fleet_size(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    assert db_session.bind is not None
    engine = db_session.bind.sync_engine

    pack_reads: dict[int, int] = {}
    device_locks: dict[int, int] = {}
    totals: dict[int, int] = {}
    for generation, n in enumerate(FLEET_SIZES):
        await _seed_one_pack_per_device(db_session, db_host, n, generation)
        entries: list[str] = []

        def _listener(
            conn: object,
            cursor: object,
            statement: str,
            parameters: object,
            context: object,
            executemany: bool,
            *,
            _entries: list[str] = entries,
        ) -> None:
            _entries.append(" ".join(statement.split()))

        # Engine-scoped on purpose: this measures a loop that drives many sessions
        # out of a factory AND counts engine-level commits, neither of which a
        # per-session pin can see. The listeners are attached only around the
        # measured call, so no seeding or teardown traffic is counted. See
        # tests/concurrency/group_lock_helpers.capture_statements for the pinned
        # form the session-scoped budget tests use.
        event.listen(engine, "before_cursor_execute", _listener)
        try:
            await run_release_rollout_stage(db_session, publisher=event_bus)
        finally:
            event.remove(engine, "before_cursor_execute", _listener)

        # Scope to the inventory phase: everything issued before the first
        # per-candidate Device lock. Per-candidate reconcile work (a separate,
        # pre-existing per-device driver-pack fallback read, out of this
        # task's scope) comes after that marker and must not be counted here.
        first_lock = next((i for i, statement in enumerate(entries) if _is_device_lock(statement)), len(entries))
        inventory_reads = entries[:first_lock]
        pack_reads[n] = sum(1 for statement in inventory_reads if _is_driver_pack_read(statement))
        totals[n] = len(entries)
        device_locks[n] = sum(1 for statement in entries if _is_device_lock(statement))

    assert pack_reads[1] == pack_reads[10] == pack_reads[50], (
        f"driver-pack inventory reads must stay constant across fleet size, got {pack_reads}"
    )
    assert pack_reads[1] <= 2, (
        f"expected one batched driver_packs read plus one selectinload follow-up, got {pack_reads[1]}"
    )

    # Exactly one Device lock per changed candidate -- never more than one
    # candidate's worth of locks for the same device, and never fewer.
    assert device_locks == {n: n for n in FLEET_SIZES}

    # Bounded per-candidate term: total statement growth between tiers must
    # not outpace candidate-count growth by more than a generous constant
    # factor (guards against an accidental O(n^2) reintroduced anywhere in
    # the per-candidate path, without hand-deriving an exact coefficient).
    per_candidate_10 = (totals[10] - totals[1]) / (10 - 1)
    per_candidate_50 = (totals[50] - totals[10]) / (50 - 10)
    assert per_candidate_50 <= per_candidate_10 * 1.5, (
        f"per-candidate statement cost grew from {per_candidate_10} to {per_candidate_50} between "
        f"fleet sizes 10 and 50 -- expected roughly constant, got totals={totals}"
    )


def _release_rollout_source() -> str:
    return (Path(__file__).resolve().parents[2] / "app" / "packs" / "services" / "release_rollout.py").read_text(
        encoding="utf-8"
    )


def test_release_rollout_never_calls_selected_release_id_per_device() -> None:
    """``selected_release_id`` (one query per pack, the defect this task fixes)
    must not appear in the production module at all -- the batched inventory
    read replaces it entirely, it does not merely move the call out of the
    per-candidate loop."""
    assert "selected_release_id" not in _release_rollout_source()


def test_release_rollout_owns_no_direct_commit_or_rollback() -> None:
    source = _release_rollout_source()
    assert ".commit()" not in source, "release_rollout.py must not own a commit"
    assert ".rollback()" not in source, "release_rollout.py must not own a rollback"
