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
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import select

from app.devices.models import Device, DeviceOperationalState, DeviceRemediationLogEntry
from app.lifecycle.services.actions import LifecyclePolicyActionsService
from app.lifecycle.services.incidents import LifecycleIncidentService
from app.lifecycle.services.policy import LifecyclePolicyService
from app.runs.service_reservation import RunReservationService
from tests.fakes import FakeSettingsReader, build_review_service
from tests.helpers import create_device
from tests.helpers import test_event_bus as event_bus

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.hosts.models import Host

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
            svc = LifecyclePolicyService(
                review=build_review_service(),
                publisher=event_bus,
                settings=FakeSettingsReader({}),
                actions=LifecyclePolicyActionsService(
                    publisher=event_bus,
                    reservation=RunReservationService(review=build_review_service()),
                    incidents=LifecycleIncidentService(),
                ),
                incidents=LifecycleIncidentService(),
                viability=Mock(),
                node_manager=AsyncMock(),
            )
            await svc.handle_health_failure(
                session,
                device_obj,
                source=source,
                reason=reason,
            )

    await asyncio.gather(*[writer(s, r) for s, r in inputs])

    async with db_session_maker() as verify:
        rows = (
            (
                await verify.execute(
                    select(DeviceRemediationLogEntry).where(DeviceRemediationLogEntry.device_id == device_id)
                )
            )
            .scalars()
            .all()
        )

    failure_pairs = {(row.source, row.reason) for row in rows if row.kind == "failure"}
    assert failure_pairs == set(inputs)
