"""The one-commit outbox budget, in the ordinary suite.

Phase 6's guarantee is that an event row rides the transaction that caused it.
The assertion that catches a regression is a commit count, and it used to live
in ``tests/test_bench_folds.py`` behind ``FOLD_BENCH=1`` -- a gate nothing under
``.github/`` sets, so it never ran in CI. It is a single-device fold; the
benchmark harness was never what made it valuable.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import event, func, select

from app.core.observation_revision import next_observation_revision
from app.events.models import SystemEvent
from tests.bench_instrumentation import CommitTap, QueryTap, install_async_session_callsite_profiler
from tests.fold_fixtures import (
    MIXED_FLEET,
    build_real_lifecycle_connectivity_service,
    device_health_loop_section,
    seed_fleet,
)
from tests.helpers import dispatch_committed_events

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = [pytest.mark.db, pytest.mark.usefixtures("seeded_driver_packs")]


async def test_event_bearing_fold_performs_one_commit(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An event-bearing fold does one source commit; the event row rides that transaction.

    Phase 6 stages the event row (``publisher.queue_for_session``) on the same source
    transaction as the device mutation that caused it, and a database trigger
    (``notify_system_event_insert``) does the ``pg_notify`` at commit time -- there is
    no second, event-only commit. Before this phase, the source transaction committed
    and then the now-deleted ``EventBus._persist_system_event`` opened a *second*
    transaction to insert the ``system_events`` row and issued an application-side
    ``SELECT pg_notify(...)``. Both symbols were deleted in Task 2. The pre-phase
    baseline for the second-commit half is recorded here rather than re-derived --
    the same underlying invariant (a reintroduced event-only commit) was guarded by
    the old ``CommitTap.deferred_count > 0`` assertion at the now-deleted
    ``tests/test_bench_folds.py:1115``, though that assertion lived inside
    ``test_bench_device_health_loop_fold``'s env-parameterized multi-scenario sweep
    (gated on ``effective_unhealthy``/``reseed_per_iteration``), not this fixture.
    The application-side-notify half is guarded separately and statically, not here:
    see ``tests/events/test_event_bus_publish_allowlist.py::test_no_unexpected_pg_notify_sites``.
    A tap-based ``pg_notify`` assertion was tried here first and removed --
    ``statement_signature()`` collapses any statement to verb+table, so
    ``SELECT pg_notify('system_events', '999')`` collapses to ``'SELECT ?'`` under
    bind-param, positional-param, and literal forms alike, indistinguishable from any
    other argument-less SELECT. The assertion could never fail against this tap and was
    vacuous.

    This one-device first-unhealthy transition genuinely stages three events, not one:
    ``device.operational_state_changed`` (available -> offline), ``device.health_changed``
    (overall: failed), and ``device.lifecycle_incident`` (lifecycle_auto_stopped) -- the
    real lifecycle stack's full escalation for a device's first observed failure. Those
    three rows land in two ``INSERT INTO system_events`` statements, not one: an unrelated
    write elsewhere in the same fold (``app.core.leader.state_store.set_value``) triggers
    an autoflush that happens to catch the first two events batched together, and the
    commit-time flush (attributed to
    ``app.devices.services.connectivity.fold_host_devices``) picks up the third alone.
    That split is an artifact of *when* an unrelated autoflush fires mid-fold, not a
    property of the outbox itself, so the statement count is deliberately not asserted --
    pinning it would couple this test to autoflush timing having nothing to do with
    events.

    The two assertions below cover different halves of the outbox invariant and are NOT
    interchangeable:

    - ``commits.count == 1`` is what actually catches a reintroduced separate event
      transaction. ``CommitTap`` hooks the engine-level ``"commit"`` event, so it fires
      no matter which session API issued the commit. Do not weaken or remove it on the
      theory that the attribution check below already covers separate-transaction
      regressions -- it does not (see next bullet).
    - the callsite-attribution check only covers an INSERT reached through the
      instrumented ``AsyncSession`` methods (``install_async_session_callsite_profiler``).
      It is blind to ``EventBus.publish()``'s standalone
      ``async with self._session_factory.begin() as db`` path: an INSERT reaching the
      database through that path attributes to ``"unattributed"``, which trivially
      satisfies ``not callsite.startswith("app.events.")`` and would NOT be caught here.

    Relocated from ``tests/test_bench_folds.py`` (as
    ``test_bench_event_fold_commit_and_notify_budget``) so this assertion runs in the
    ordinary suite instead of behind ``FOLD_BENCH=1``, which nothing under ``.github/``
    ever set. The fleet-seeding fixtures it depends on moved to ``tests/fold_fixtures.py``
    alongside it; the benchmark keeps its own env-driven knobs and timing/statement-shape
    reporting untouched.
    """
    install_async_session_callsite_profiler(monkeypatch)
    service = build_real_lifecycle_connectivity_service()
    tap = QueryTap()
    commits = CommitTap()
    engine = db_session.bind.sync_engine
    event.listen(engine, "before_cursor_execute", tap)
    event.listen(engine, "commit", commits)
    tap.armed = False
    commits.armed = False
    try:
        host, devices = await seed_fleet(db_session, MIXED_FLEET, 1, generation=0)
        revision = await next_observation_revision(db_session)
        section = device_health_loop_section(devices, unhealthy_count=1, revision=revision, section_sequence=1)
        tap.armed = True
        commits.armed = True
        settled = await service.fold_host_devices(db_session, host.id, section, boot_id=uuid.uuid4())
        tap.armed = False
        commits.armed = False
        await dispatch_committed_events()
        assert settled is True
    finally:
        event.remove(engine, "before_cursor_execute", tap)
        event.remove(engine, "commit", commits)

    inserts = [callsite for (callsite, signature) in tap.callsite_counter if signature == "INSERT system_events"]
    assert inserts, "fold emitted no outbox row"
    assert not any(callsite.startswith("app.events.") for callsite in inserts), (
        f"outbox INSERT ran outside the source transaction: {inserts}"
    )
    assert commits.count == 1
    event_row_count = await db_session.scalar(select(func.count()).select_from(SystemEvent))
    assert event_row_count is not None
    assert event_row_count >= 1
