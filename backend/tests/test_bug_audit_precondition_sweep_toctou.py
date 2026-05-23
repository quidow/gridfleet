"""Bug 5: Precondition sweep deletes intents whose precondition was updated concurrently.

See ``docs/superpowers/specs/2026-05-20-backend-bug-audit.md#bug-5``.

``reconcile_unsatisfied_preconditions`` reads all intents with a
precondition at ``intent_preconditions.py:40`` without a row lock,
checks ``is_satisfied`` against the in-memory snapshot, and deletes
the row if unsatisfied. Between the snapshot read and the delete, a
concurrent producer can upsert the same intent (same ``(device_id,
source)`` unique key) with a new, satisfied precondition — but the
sweep still uses the stale precondition and deletes the row, losing
the freshly registered intent.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.devices.models import DeviceOperationalState
from app.devices.services import intent_preconditions
from app.devices.services.intent_preconditions import reconcile_unsatisfied_preconditions
from app.devices.services.intent_types import NODE_PROCESS
from tests.helpers import create_device, create_host

if TYPE_CHECKING:
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@pytest.mark.db
@pytest.mark.asyncio
async def test_precondition_sweep_deletes_concurrently_reregistered_intent(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    client: AsyncClient,
) -> None:
    from app.devices.models import (
        DeviceHold,
        DeviceIntent,
    )

    host = await create_host(client)
    device = await create_device(
        db_session,
        host_id=uuid.UUID(host["id"]),
        name="precondition-toctou",
        operational_state=DeviceOperationalState.available,
        hold=DeviceHold.maintenance,
        verified=True,
    )
    device_id = device.id

    # Seed an intent whose precondition targets a non-existent device — so
    # ``_eval_device_hold`` returns False and the sweep would delete the row.
    intent_source = f"test_precondition:{device.id}"
    nonexistent_device_id = uuid.uuid4()
    seed_intent = DeviceIntent(
        device_id=device.id,
        source=intent_source,
        axis=NODE_PROCESS,
        payload={"action": "start", "priority": 10},
        precondition={
            "kind": "device_hold",
            "device_id": str(nonexistent_device_id),
            "hold": "maintenance",
        },
        expires_at=None,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    db_session.add(seed_intent)
    await db_session.commit()

    original_is_satisfied = intent_preconditions.is_satisfied
    triggered = False

    async def _check_then_race(db: AsyncSession, intent_row: DeviceIntent) -> bool:
        nonlocal triggered
        # Snapshot precondition still points to nonexistent_device_id, so the
        # first call (on the unlocked sweep snapshot) returns False. Drive the
        # concurrent producer right after that first evaluation so the
        # fixed sweep's locked re-fetch + second is_satisfied call observes
        # the upserted precondition that does hold.
        result = await original_is_satisfied(db, intent_row)
        if not triggered and not result:
            triggered = True
            async with db_session_maker() as side:
                stmt = (
                    pg_insert(DeviceIntent)
                    .values(
                        device_id=device_id,
                        source=intent_source,
                        axis=NODE_PROCESS,
                        payload={"action": "start", "priority": 10, "regenerated": True},
                        precondition={
                            "kind": "device_hold",
                            "device_id": str(device_id),
                            "hold": "maintenance",
                        },
                        created_at=datetime.now(UTC),
                        updated_at=datetime.now(UTC),
                    )
                    .on_conflict_do_update(
                        index_elements=[DeviceIntent.device_id, DeviceIntent.source],
                        set_={
                            "axis": NODE_PROCESS,
                            "payload": {"action": "start", "priority": 10, "regenerated": True},
                            "precondition": {
                                "kind": "device_hold",
                                "device_id": str(device_id),
                                "hold": "maintenance",
                            },
                            "updated_at": datetime.now(UTC),
                        },
                    )
                )
                await side.execute(stmt)
                await side.commit()
        return result

    intent_preconditions.is_satisfied = _check_then_race  # type: ignore[assignment]
    try:
        await reconcile_unsatisfied_preconditions(db_session)
        await db_session.commit()
    finally:
        intent_preconditions.is_satisfied = original_is_satisfied

    # Re-read the intent on a fresh session. Fixed behavior: the sweep
    # would re-validate the precondition under a row lock (or use a
    # WHERE clause guarding on the snapshot's updated_at) and see the
    # row was re-registered with a satisfied precondition, so it would
    # not delete. Current behavior (bug): the sweep deletes regardless.
    async with db_session_maker() as side:
        result = await side.execute(
            select(DeviceIntent).where(
                DeviceIntent.device_id == device_id,
                DeviceIntent.source == intent_source,
            )
        )
        row = result.scalar_one_or_none()
        assert row is not None, (
            "Precondition sweep deleted an intent that was concurrently re-registered with a satisfied precondition"
        )
