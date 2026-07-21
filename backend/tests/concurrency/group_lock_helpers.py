"""Shared synchronisation and service-construction helpers for the
group-mutation-lock concurrency tests.

Both ``test_concurrency_group_create_vs_delete_member_of.py`` and
``test_concurrency_group_delete_vs_first_member_of.py`` pit two concurrent
transactions against each other and need the first to have acquired the
group-mutation advisory lock before releasing the second. That handshake,
plus the plain ``DeviceGroupsService`` construction both files need, lives
here so the two test modules stay in lockstep instead of drifting apart.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy import event, select

from app.devices.models.group import DeviceGroup
from app.devices.services.groups import DeviceGroupsService
from app.devices.services.identity_conflicts import DeviceIdentityConflictService
from app.devices.services.service import DeviceCrudService
from tests.helpers import test_event_bus as event_bus

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

# Long enough for the peer's blocked advisory-lock acquire to reach the lock
# manager before the holder commits. Only widens the race window; correctness
# does not depend on the exact value.
HANDOFF_SEC = 0.5

# A safety net, not a race parameter: comfortably above HANDOFF_SEC so it
# never trips under normal timing, but bounds the wait if the
# "pg_advisory_xact_lock" substring match in ``signal_after_group_lock``
# ever stops matching the real acquire statement — turning a wedged test
# into a legible TimeoutError instead of a hung CI job.
EVENT_WAIT_TIMEOUT_SEC = 5.0


def build_groups_service() -> DeviceGroupsService:
    return DeviceGroupsService(
        publisher=event_bus,
        crud=DeviceCrudService(identity=DeviceIdentityConflictService(), publisher=event_bus),
    )


def signal_after_group_lock(session: AsyncSession, locked: asyncio.Event) -> None:
    """Set *locked* once *session* holds the group-mutation advisory lock.

    Then hold inside the interception for ``HANDOFF_SEC`` so the peer
    transaction reaches its own ``pg_advisory_xact_lock`` and blocks there.
    """
    original_execute = session.execute
    fired = False

    async def _intercepted(stmt: Any, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        nonlocal fired
        result = await original_execute(stmt, *args, **kwargs)
        if not fired and "pg_advisory_xact_lock" in str(stmt).lower():
            fired = True
            locked.set()
            await asyncio.sleep(HANDOFF_SEC)
        return result

    session.execute = _intercepted  # type: ignore[assignment, method-assign]


async def wait_for_group_lock(locked: asyncio.Event, *, label: str) -> None:
    """Await *locked* with a bounded timeout instead of hanging forever.

    ``locked`` is only ever set by ``signal_after_group_lock``'s textual
    match on ``pg_advisory_xact_lock``. If the real acquire statement is ever
    reworded, wrapped differently, or renamed, that match silently stops
    firing and a bare ``await locked.wait()`` would hang the test run
    forever (``pytest-timeout`` is deliberately not a dependency here). Fail
    fast with a message that names the likely cause instead.
    """
    try:
        await asyncio.wait_for(locked.wait(), timeout=EVENT_WAIT_TIMEOUT_SEC)
    except TimeoutError:
        pytest.fail(
            f"{label}: never observed the peer acquire the group-mutation advisory lock within "
            f"{EVENT_WAIT_TIMEOUT_SEC}s. The 'pg_advisory_xact_lock' substring match in "
            "signal_after_group_lock() likely no longer matches the real acquire statement."
        )


async def fetch_group_rows(
    db_session_maker: async_sessionmaker[AsyncSession],
    *,
    static_key: str,
    dynamic_key: str,
) -> tuple[DeviceGroup | None, DeviceGroup | None]:
    """Fetch both the static and dynamic group rows by their keys.

    Returns a tuple of (static_row, dynamic_row), either of which may be None.
    """
    async with db_session_maker() as verify:
        static_row = (
            await verify.execute(select(DeviceGroup).where(DeviceGroup.key == static_key))
        ).scalar_one_or_none()
        dynamic_row = (
            await verify.execute(select(DeviceGroup).where(DeviceGroup.key == dynamic_key))
        ).scalar_one_or_none()
        return static_row, dynamic_row


async def assert_no_dangling_reference(
    db_session_maker: async_sessionmaker[AsyncSession],
    *,
    static_key: str,
    dynamic_key: str,
) -> None:
    """Assert the dynamic group never ends up referencing a deleted static group.

    Meaningful only where the dynamic row can exist independently of whether
    the static row survives (e.g. the delete-vs-first-member_of file, where
    an ``update_group`` can add the reference). Do not reuse this for
    interleavings where one of the two outcomes always makes the guard
    vacuous — pin the exact expected end state there instead.
    """
    static_row, dynamic_row = await fetch_group_rows(db_session_maker, static_key=static_key, dynamic_key=dynamic_key)
    assert dynamic_row is not None
    if static_row is None:
        member_of = (dynamic_row.filters or {}).get("member_of", [])
        assert static_key not in member_of, f"dynamic group {dynamic_key} references deleted static group {static_key}"


@contextmanager
def capture_statements(session: AsyncSession) -> Iterator[list[str]]:
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
