"""Phase 10: cleanup batches and capacity snapshots stay on a fixed statement budget.

Two properties, both invisible to the behavioral cleanup/capacity tests:

* ``collect_capacity_snapshot_once`` costs the same six statements (five
  aggregate reads plus the insert) no matter how large the fleet is — nothing
  in the five ``_count_*`` readers scans per-row or issues one query per
  device/host; and
* ``_delete_in_batches`` drains a table that is one row over the batch size
  with two set-based batch deletes, not one row-delete per record, and still
  leaves the session clean after the loop's mandated terminal empty probe.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from app.appium_nodes.models import AppiumDesiredState, AppiumNode
from app.devices.models import DeviceOperationalState
from app.devices.services import data_cleanup
from app.devices.services.fleet_capacity import FleetCapacityService
from app.sessions.models import Session, SessionStatus
from tests.concurrency.group_lock_helpers import capture_statements
from tests.helpers import create_device_record

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
    from sqlalchemy.orm import InstrumentedAttribute
    from sqlalchemy.sql.elements import ColumnElement

    from app.devices.services.data_cleanup import CleanupModel
    from app.hosts.models import Host

pytestmark = [pytest.mark.asyncio, pytest.mark.db]


async def _seed_devices(db: AsyncSession, host_id: uuid.UUID, *, already: int = 0, target: int) -> None:
    """Grow the seeded fleet from ``already`` to ``target`` verified, schedulable devices."""
    for index in range(already, target):
        identity = f"budget-device-{index:04d}"
        device = await create_device_record(
            db,
            host_id=host_id,
            identity_value=identity,
            name=f"Budget Device {index}",
            operational_state=DeviceOperationalState.available,
        )
        db.add(
            AppiumNode(
                device_id=device.id,
                port=4723 + index,
                desired_state=AppiumDesiredState.running,
                desired_port=4723 + index,
                pid=100 + index,
                active_connection_target="usb",
            )
        )
    await db.commit()


async def _measure_capacity(db_session_maker: async_sessionmaker[AsyncSession], *, captured_at: datetime) -> list[str]:
    """Statements issued by one capacity collection, on a session of its own.

    A fresh session keeps the measurement free of the seeding transaction and
    lets ``capture_statements`` pin its listener the way it requires.
    """
    async with db_session_maker() as reader, capture_statements(reader) as statements:
        await FleetCapacityService().collect_capacity_snapshot_once(
            reader,
            offline_after_sec=45.0,
            captured_at=captured_at,
        )
    return statements


async def test_capacity_collection_statement_count_is_flat_across_fleet_sizes(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    db_host: Host,
) -> None:
    counts: dict[int, int] = {}
    seeded = 0
    for size in (1, 10, 50):
        await _seed_devices(db_session, db_host.id, already=seeded, target=size)
        seeded = size
        statements = await _measure_capacity(
            db_session_maker,
            captured_at=datetime(2026, 5, 1, tzinfo=UTC) + timedelta(seconds=size),
        )
        counts[size] = len(statements)

    assert counts[1] == counts[10] == counts[50] == 6, (
        "capacity collection must cost exactly six statements (five reads + one insert) "
        f"at every fleet size; got {counts} (a per-device or per-host query is the usual cause)"
    )


async def test_cleanup_deletes_large_table_in_two_set_based_batches(db_session: AsyncSession) -> None:
    """``DELETE_BATCH_SIZE + 1`` old rows must drain in two set-based batches, not 1001
    individual row deletes — and the batch loop must still leave the session clean
    after its mandated terminal (zero-row) probe.

    Counting raw DELETE statements cannot tell "set-based" apart from "row-by-row
    deletion capped at a suspiciously round number", and it cannot tell a committed
    terminal probe apart from one left dangling on the session — both implementations
    would issue the same SQL text either way. Spying on ``_delete_one_batch``'s
    per-call rowcount, and checking transaction state after, tests the actual claims.
    """
    old_time = datetime.now(UTC) - timedelta(days=100)
    row_count = data_cleanup.DELETE_BATCH_SIZE + 1
    db_session.add_all(
        [
            Session(
                session_id=f"budget-session-{index:05d}",
                status=SessionStatus.passed,
                started_at=old_time - timedelta(seconds=index),
                ended_at=old_time - timedelta(seconds=index - 1),
            )
            for index in range(row_count)
        ]
    )
    await db_session.commit()

    real_delete_one_batch = data_cleanup._delete_one_batch
    batch_sizes: list[int] = []

    async def _recording_delete_one_batch(
        db: AsyncSession,
        *,
        model: CleanupModel,
        timestamp_column: InstrumentedAttribute[datetime],
        cutoff: datetime,
        extra_predicates: tuple[ColumnElement[bool], ...] = (),
    ) -> int:
        deleted = await real_delete_one_batch(
            db,
            model=model,
            timestamp_column=timestamp_column,
            cutoff=cutoff,
            extra_predicates=extra_predicates,
        )
        batch_sizes.append(deleted)
        return deleted

    with patch("app.devices.services.data_cleanup._delete_one_batch", _recording_delete_one_batch):
        deleted_total = await data_cleanup._delete_in_batches(
            db_session,
            model=Session,
            timestamp_column=Session.started_at,
            cutoff=old_time + timedelta(days=1),
            # As every production caller does, and as the device-lock guard
            # requires of any Session delete from this module; every row seeded
            # above is ended, so the batch sizes are unchanged.
            extra_predicates=(Session.ended_at.is_not(None),),
        )

    assert deleted_total == row_count
    non_empty_batches = [size for size in batch_sizes if size > 0]
    assert len(non_empty_batches) == 2, f"expected two set-based batches of real rows, got {batch_sizes}"
    assert batch_sizes[-1] == 0, "the loop must run its mandated terminal empty probe"
    assert not db_session.in_transaction(), "the terminal empty probe must commit, not dangle"
