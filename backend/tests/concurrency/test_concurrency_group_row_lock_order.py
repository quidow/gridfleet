"""``delete_group`` and ``update_group`` must take ``device_groups`` row locks
in one global order.

``delete_group`` used to lock the target static group S and *then* every dynamic
group; ``update_group`` locked the dynamic group D and *then* the statics D
referenced, including S. Concurrent operator actions on the Groups page could
therefore reach T1(S, waiting on D) / T2(D, waiting on S) and Postgres would
abort one with a deadlock — surfacing as an unhandled 500.

The fix is one global lock order: every ``device_groups`` acquisition is a
single ascending-key ``FOR UPDATE``. These tests pin that structurally (a lock
cycle is impossible when no transaction holds one group row while requesting
another in a separate statement) and functionally.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import event, select

from app.devices.models.group import DeviceGroup, GroupType
from app.devices.schemas.group import DeviceGroupUpdate
from app.devices.services.groups import DeviceGroupsService
from app.devices.services.identity_conflicts import DeviceIdentityConflictService
from app.devices.services.service import DeviceCrudService
from tests.helpers import test_event_bus as event_bus

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

pytestmark = [pytest.mark.db, pytest.mark.asyncio]


def _service() -> DeviceGroupsService:
    return DeviceGroupsService(
        publisher=event_bus,
        crud=DeviceCrudService(identity=DeviceIdentityConflictService(), publisher=event_bus),
    )


@contextlib.contextmanager
def _capture_statements(session: AsyncSession) -> Iterator[list[str]]:
    statements: list[str] = []

    def listener(
        conn: object,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        statements.append(statement)

    bind = session.bind
    assert bind is not None
    sync_engine = bind.sync_engine if hasattr(bind, "sync_engine") else bind
    event.listen(sync_engine, "before_cursor_execute", listener)
    try:
        yield statements
    finally:
        event.remove(sync_engine, "before_cursor_execute", listener)


def _group_lock_statements(statements: list[str]) -> list[str]:
    return [
        stmt
        for stmt in statements
        if "for update" in stmt.lower()
        and "device_groups" in stmt.lower()
        and stmt.lstrip().lower().startswith("select")
    ]


async def _seed_static_and_dynamic(db_session: AsyncSession) -> tuple[str, str]:
    """A static group and a dynamic group whose ``member_of`` references it.

    Keys are chosen so the dynamic key sorts *before* the static key; a lock
    order that followed reference direction rather than key order would show up
    as a second statement.
    """
    suffix = uuid.uuid4().hex[:8]
    static_key = f"z-static-{suffix}"
    dynamic_key = f"a-dynamic-{suffix}"
    db_session.add(DeviceGroup(key=static_key, name=static_key, group_type=GroupType.static))
    db_session.add(
        DeviceGroup(
            key=dynamic_key,
            name=dynamic_key,
            group_type=GroupType.dynamic,
            filters={"member_of": [static_key], "device_type": "real_device"},
        )
    )
    await db_session.commit()
    return static_key, dynamic_key


async def test_delete_group_locks_device_groups_in_one_statement(db_session: AsyncSession) -> None:
    """Deleting a referenced static group must not pre-lock the target and then
    request the referrers in a second statement — that is the ordering that
    inverts against ``update_group``."""
    static_key, _dynamic_key = await _seed_static_and_dynamic(db_session)

    with _capture_statements(db_session) as statements:
        from app.devices.services.groups import GroupReferencedError

        with pytest.raises(GroupReferencedError):
            await _service().delete_group(db_session, static_key)

    locks = _group_lock_statements(statements)
    assert len(locks) == 1, f"delete_group must acquire device_groups rows in one statement, got: {locks}"
    assert "order by" in locks[0].lower(), locks[0]


async def test_update_group_locks_device_groups_in_one_statement(db_session: AsyncSession) -> None:
    """Updating a dynamic group must lock itself and its ``member_of`` targets
    together, in key order, not itself first and the targets second."""
    _static_key, dynamic_key = await _seed_static_and_dynamic(db_session)

    with _capture_statements(db_session) as statements:
        updated = await _service().update_group(
            db_session,
            dynamic_key,
            DeviceGroupUpdate(description="relabelled"),
        )
    assert updated is not None

    locks = _group_lock_statements(statements)
    assert len(locks) == 1, f"update_group must acquire device_groups rows in one statement, got: {locks}"
    assert "order by" in locks[0].lower(), locks[0]


async def test_concurrent_delete_and_update_do_not_deadlock(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """The two operator actions run concurrently against the same pair of rows
    without either transaction being aborted by the deadlock detector."""
    static_key, dynamic_key = await _seed_static_and_dynamic(db_session)
    service = _service()

    async def delete_static() -> bool:
        async with db_session_maker() as session:
            return await service.delete_group(session, static_key)

    async def update_dynamic() -> DeviceGroup | None:
        async with db_session_maker() as session:
            return await service.update_group(
                session,
                dynamic_key,
                DeviceGroupUpdate(description=f"touched-{uuid.uuid4().hex[:6]}"),
            )

    results = await asyncio.gather(delete_static(), update_dynamic(), return_exceptions=True)

    for result in results:
        if isinstance(result, Exception):
            # GroupReferencedError is a legitimate outcome (the delete lost the
            # race to the still-referencing update). A deadlock is not.
            assert "deadlock" not in str(result).lower(), f"group edits deadlocked: {result!r}"

    # Whatever the interleaving, the dynamic group survives and the static group
    # is either intact (delete rejected) or gone with no dangling reference.
    async with db_session_maker() as verify:
        dynamic_row = (
            await verify.execute(select(DeviceGroup).where(DeviceGroup.key == dynamic_key))
        ).scalar_one_or_none()
        assert dynamic_row is not None
        static_row = (
            await verify.execute(select(DeviceGroup).where(DeviceGroup.key == static_key))
        ).scalar_one_or_none()
        if static_row is None:
            filters = dynamic_row.filters or {}
            assert static_key not in filters.get("member_of", []), "deleted static group left a dangling reference"
