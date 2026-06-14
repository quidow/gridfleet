"""Bug 6: ``add_members`` raises IntegrityError on concurrent same-(group, device) insert.

See ``docs/superpowers/specs/2026-05-20-backend-bug-audit.md#bug-6``.

``add_members`` at ``backend/app/devices/services/groups.py:109-124``
issues an unlocked ``SELECT`` for membership existence, then plain
``db.add`` + commit. Two concurrent operator calls adding the same
device to the same group both pass the exists check (their snapshots
predate each other's insert) and both attempt to ``INSERT``. The
unique constraint on ``(group_id, device_id)`` then makes the second
commit raise ``IntegrityError`` — the API surfaces a 500 for a
legitimate "device is already in the group" condition that should be
a benign no-op.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy.exc import IntegrityError

from app.devices.models import DeviceOperationalState
from app.devices.models.group import DeviceGroup, DeviceGroupMembership, GroupType
from app.devices.services.groups import DeviceGroupsService
from app.devices.services.identity_conflicts import DeviceIdentityConflictService
from app.devices.services.service import DeviceCrudService
from tests.fakes import FakeSettingsReader
from tests.helpers import create_device, create_host
from tests.helpers import test_event_bus as event_bus

if TYPE_CHECKING:
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@pytest.mark.db
@pytest.mark.asyncio
async def test_add_members_races_concurrent_duplicate_insert(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    client: AsyncClient,
) -> None:
    host = await create_host(client)
    device = await create_device(
        db_session,
        host_id=uuid.UUID(host["id"]),
        name="group-race",
        operational_state=DeviceOperationalState.available,
        verified=True,
    )
    group = DeviceGroup(name=f"race-{uuid.uuid4().hex[:8]}", group_type=GroupType.static)
    db_session.add(group)
    await db_session.commit()

    group_id = group.id
    device_id = device.id

    original_execute = db_session.execute
    triggered = False

    async def _race_after_select(stmt: Any, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        nonlocal triggered
        result = await original_execute(stmt, *args, **kwargs)
        stmt_text = str(stmt).lower()
        # After ``add_members`` runs its membership exists check (a SELECT
        # against device_group_memberships filtered by group_id+device_id),
        # commit a side-channel INSERT to simulate a concurrent peer adding
        # the same membership. The unlocked SELECT cannot see this insert in
        # its own snapshot, but the subsequent main-session INSERT + commit
        # will collide on the unique constraint.
        if not triggered and "device_group_memberships" in stmt_text and "select" in stmt_text and "where" in stmt_text:
            triggered = True
            async with db_session_maker() as side:
                side.add(DeviceGroupMembership(group_id=group_id, device_id=device_id))
                await side.commit()
        return result

    db_session.execute = _race_after_select  # type: ignore[assignment, method-assign]
    try:
        # Fixed behavior: add_members would use ``INSERT ... ON CONFLICT DO
        # NOTHING`` (or a per-row try/except IntegrityError) and treat the
        # concurrent duplicate as a benign no-op. Current behavior (bug):
        # the plain ``db.add`` + ``db.commit`` raises IntegrityError.
        try:
            _settings = FakeSettingsReader({})
            await DeviceGroupsService(
                publisher=event_bus,
                settings=_settings,
                crud=DeviceCrudService(
                    settings=_settings, identity=DeviceIdentityConflictService(), publisher=event_bus
                ),
            ).add_members(db_session, group_id, [device_id])
        except IntegrityError as exc:
            pytest.fail(f"add_members raised IntegrityError on concurrent duplicate insert: {exc}")
    finally:
        db_session.execute = original_execute  # type: ignore[method-assign]
