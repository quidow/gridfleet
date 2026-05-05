"""Concurrency test for R4: lifecycle_policy_state read-modify-write race.

The race:
  ``handle_health_failure`` reads the device's lifecycle_policy_state JSON column
  at the start of the function, performs several awaits (DB queries, node-stop
  calls), then writes the column back via ``write_state``.  Without a row-level
  lock covering that entire window, a second concurrent writer can commit its own
  values for (last_failure_source, last_failure_reason) between the first writer's
  read and write.  The first writer then commits a stale dict that overwrites the
  second writer's update.

  This is a "stale overwrite" race (not a torn triple within one writer): every
  individual call to write_state produces a coherent dict, but the inter-writer
  ordering is wrong — the later-committing writer wins even when a more-recent
  writer has already committed.

Test strategy (deterministic):
  Writer A is paused inside handle_health_failure after reading current_state but
  before calling write_state.  The pause is achieved by patching
  ``lifecycle_policy_actions.record_auto_stopped_incident`` (the async function
  called at the end of complete_auto_stop, right before the final write_state
  commit) so that writer A waits for writer B to commit before proceeding.
  This ensures the ordering:
    1. Writer A reads current_state (source="src-a", reason="reason-a")
    2. Writer A awaits the patched record_auto_stopped_incident (yields)
    3. Writer B reads current_state (sees initial empty state or writer A's partial
       in-memory change — both are fine for the test)
    4. Writer B calls complete_auto_stop normally, writes and commits src-b/reason-b
    5. Writer A resumes, calls the original record_auto_stopped_incident with its
       stale current_state dict (still src-a/reason-a), writes and commits

  Expected result WITHOUT row lock: writer A's commit overwrites writer B's src-b/
  reason-b with src-a/reason-a.  The final DB row contains src-a/reason-a even
  though src-b/reason-b was committed more recently.

  Expected result WITH row lock (after Task 9): writer A acquires SELECT FOR UPDATE
  before reading current_state; writer B's attempt to read the locked row blocks
  until writer A commits.  The writes are serialized and the final state is coherent.

Module structure:
  1. test_concurrent_health_failure_does_not_tear_lifecycle_state — reference test
     that checks triple coherence (source/reason pair must be from same writer).
     Passes today; must continue to pass after Task 9.
  2. test_concurrent_health_failure_stale_overwrite — deterministic red test.
     Fails today (writer A's stale overwrite clobbers writer B's committed state).
     Must pass after Task 9 adds row locking.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest
from sqlalchemy import select

from app.models.device import Device, DeviceOperationalState
from app.services import lifecycle_policy
from tests.helpers import create_device

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.models.host import Host
    from app.models.test_run import TestRun

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Reference test (non-deterministic — passes today; must pass after Task 9)
# ---------------------------------------------------------------------------


async def test_concurrent_health_failure_does_not_tear_lifecycle_state(
    db_session_maker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    """Three concurrent writers each call handle_health_failure with distinct triples.

    Each writer uses its own isolated DB session.  write_state produces a complete
    coherent dict per writer, so the final committed row must contain a coherent
    (last_failure_source, last_failure_reason) pair from exactly ONE writer.

    This test documents the invariant that must hold with or without row locks.
    """
    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="rmw-target",
        operational_state=DeviceOperationalState.offline,
    )
    await db_session.commit()
    device_id = device.id

    inputs = [
        ("src-a", "reason-a"),
        ("src-b", "reason-b"),
        ("src-c", "reason-c"),
    ]

    async def writer(source: str, reason: str) -> None:
        async with db_session_maker() as session:
            stmt = select(Device).where(Device.id == device_id)
            device_obj = (await session.execute(stmt)).scalar_one()
            await lifecycle_policy.handle_health_failure(
                session,
                device_obj,
                source=source,
                reason=reason,
            )

    await asyncio.gather(*[writer(s, r) for s, r in inputs])

    async with db_session_maker() as verify:
        device_row = (await verify.execute(select(Device).where(Device.id == device_id))).scalar_one()

    state = device_row.lifecycle_policy_state
    assert state is not None, "lifecycle_policy_state is None after concurrent writes"
    assert state["last_failure_source"] in {s for s, _ in inputs}
    assert state["last_failure_reason"] in {r for _, r in inputs}

    expected_reason = next(r for s, r in inputs if s == state["last_failure_source"])
    assert state["last_failure_reason"] == expected_reason, (
        f"Torn write: source={state['last_failure_source']!r}, reason={state['last_failure_reason']!r}, state={state}"
    )


# ---------------------------------------------------------------------------
# Deterministic reproducer (stale-overwrite race — should FAIL today)
# ---------------------------------------------------------------------------


async def test_concurrent_health_failure_stale_overwrite(
    db_session_maker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    """Deterministic stale-overwrite reproducer for the R4 race.

    Without a row-level lock in record_control_action / write_state the RMW
    window inside handle_health_failure spans from the initial
    ``policy_state(device)`` read to the final ``write_state`` commit.

    This test widens that window deterministically by patching
    ``lifecycle_policy_actions.record_auto_stopped_incident`` so that writer A
    sleeps until writer B has fully committed.  Writer A then commits its stale
    dict — overwriting writer B's more recent values.

    Expected outcome WITHOUT row lock (today):
      Writer A's commit clobbers writer B's committed src-b/reason-b with
      writer A's stale src-a/reason-a.  The assertion catches this.

    Expected outcome WITH row lock (after Task 9):
      Writer A holds a SELECT FOR UPDATE from its read to its commit.
      Writer B's read blocks at the DB level until A commits.  The two writes
      are serialized; the final state is whichever writer commits last, but
      both source/reason pairs reflect a single writer's intent.

    Implementation note:
      We patch ``lifecycle_policy_actions.record_auto_stopped_incident`` via a
      wrapper that — for the FIRST call only — signals the barrier and awaits
      writer B.  Since asyncio is single-threaded, the await inside the wrapper
      genuinely yields control to writer B.  The patch is applied at the
      callsite in writer A's context.
    """
    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="rmw-stale-target",
        operational_state=DeviceOperationalState.offline,
    )
    await db_session.commit()
    device_id = device.id

    # Synchronisation events between the two writers.
    a_about_to_write = asyncio.Event()
    b_has_committed = asyncio.Event()

    # Track whether the barrier was actually reached.
    barrier_entered = False

    # Import at point-of-use so we can patch correctly.
    from app.services import lifecycle_policy_actions as lpa

    # We need to pause writer A between its current_state construction and its
    # write_state call.  The most natural injection point is to wrap
    # ``record_auto_stopped_incident`` (called at the end of complete_auto_stop)
    # since it runs right before the final commit.
    #
    # record_auto_stopped_incident is async, so we can patch it
    # with an async wrapper that inserts the barrier for the first call.
    original_record_auto_stopped = lpa.record_auto_stopped_incident
    first_call_done = False

    async def barrier_record_auto_stopped(
        db: AsyncSession,
        device_arg: Device,
        next_state: dict[str, Any],
        *,
        run: TestRun | None,
        reason: str,
        source: str,
        detail: str,
    ) -> None:
        nonlocal barrier_entered, first_call_done
        if not first_call_done:
            first_call_done = True
            # Signal writer A is about to write its state.
            a_about_to_write.set()
            # Wait for writer B to fully commit before proceeding.
            await b_has_committed.wait()
            barrier_entered = True
        await original_record_auto_stopped(
            db, device_arg, next_state, run=run, reason=reason, source=source, detail=detail
        )

    async def writer_a() -> None:
        async with db_session_maker() as session:
            stmt = select(Device).where(Device.id == device_id)
            device_obj = (await session.execute(stmt)).scalar_one()
            with patch.object(lpa, "record_auto_stopped_incident", barrier_record_auto_stopped):
                await lifecycle_policy.handle_health_failure(
                    session,
                    device_obj,
                    source="src-a",
                    reason="reason-a",
                )

    async def writer_b() -> None:
        # Wait until writer A is paused at the barrier.
        await a_about_to_write.wait()
        async with db_session_maker() as session:
            stmt = select(Device).where(Device.id == device_id)
            device_obj = (await session.execute(stmt)).scalar_one()
            # Writer B runs without any patching; commits normally.
            await lifecycle_policy.handle_health_failure(
                session,
                device_obj,
                source="src-b",
                reason="reason-b",
            )
        # Signal writer A that B has committed.
        b_has_committed.set()

    await asyncio.gather(writer_a(), writer_b())

    assert barrier_entered, (
        "The barrier inside barrier_record_auto_stopped was never reached — "
        "complete_auto_stop was refactored and the injection point is no longer valid."
    )

    async with db_session_maker() as verify:
        device_row = (await verify.execute(select(Device).where(Device.id == device_id))).scalar_one()

    state = device_row.lifecycle_policy_state
    assert state is not None, "lifecycle_policy_state is None after concurrent writes"

    # Writer B committed src-b/reason-b while A was sleeping.
    # Without a row lock, writer A then commits src-a/reason-a on top of B's
    # already-committed state — a stale overwrite.
    #
    # The correct behaviour (after Task 9) is that A's transaction is
    # serialized with B's via SELECT FOR UPDATE, so the final state reflects
    # the true last-writer-wins ordering (src-a, since A commits after B).
    # Either way the source and reason must match within a single writer.
    #
    # TODAY: writer A overwrites B, so state shows src-a/reason-a even though
    # B committed src-b/reason-b after A's read but before A's write.
    # The assertion below treats src-b as the "expected" last state (since B
    # committed more recently from wall-clock perspective), catching the stale
    # overwrite that happens when A clobbers B.
    assert state["last_failure_source"] == "src-b", (
        f"Stale overwrite detected: writer A (src-a) overwrote writer B's "
        f"committed values (src-b/reason-b) because A held a stale in-memory "
        f"dict and no row lock prevented the clobber.  "
        f"Got last_failure_source={state['last_failure_source']!r}, "
        f"last_failure_reason={state['last_failure_reason']!r}.  "
        f"Full state: {state}"
    )
    assert state["last_failure_reason"] == "reason-b", (
        f"Stale overwrite: expected reason-b but got {state['last_failure_reason']!r}"
    )
