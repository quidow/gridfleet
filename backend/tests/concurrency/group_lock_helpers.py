"""Shared service construction, relation reads, and statement capture for the
device-group concurrency tests.

The module is named for a lock that no longer exists. Group-definition writers
are serialised by ``device_group_member_of``'s two composite foreign keys —
``fk_device_group_member_of_dynamic_group`` (CASCADE) and
``fk_device_group_member_of_static_group`` (RESTRICT) — not by a process-local
advisory lock, so nothing here intercepts or waits on lock acquisition any
more. What survives is the plumbing several unrelated suites still need:
constructing a bare ``DeviceGroupsService``, reading the relation back out, and
capturing exactly one session's SQL.

``capture_statements`` in particular is imported by six modules that have
nothing to do with group locking (``tests/lifecycle/test_escalation.py``,
``tests/devices/test_device_group_service_more.py``,
``tests/devices/test_decision_snapshot.py``,
``tests/devices/test_intent_service.py``,
``tests/devices/test_devices_import_commit.py``, and
``tests/appium_nodes/test_node_health.py``), which is why this file is kept
rather than folded into a caller.
"""

from __future__ import annotations

from contextlib import asynccontextmanager, contextmanager
from typing import TYPE_CHECKING

from sqlalchemy import event, select
from sqlalchemy.orm import aliased

from app.devices.models.group import DeviceGroup, DeviceGroupMemberOf
from app.devices.services.groups import DeviceGroupsService
from app.devices.services.identity_conflicts import DeviceIdentityConflictService
from app.devices.services.service import DeviceCrudService
from tests.helpers import test_event_bus as event_bus

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Iterator

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def build_groups_service() -> DeviceGroupsService:
    return DeviceGroupsService(
        publisher=event_bus,
        crud=DeviceCrudService(identity=DeviceIdentityConflictService(), publisher=event_bus),
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


async def fetch_member_of_keys(
    db_session_maker: async_sessionmaker[AsyncSession],
    *,
    dynamic_key: str,
) -> list[str]:
    """The static-group keys a dynamic group's ``device_group_member_of`` rows name.

    The relation, never ``filters['member_of']``: a stored JSON key is inert from
    the member-of-FK phase on, so asserting on it would pass over a writer that
    stopped persisting the reference at all.
    """
    source = aliased(DeviceGroup, name="source")
    stmt = (
        select(DeviceGroup.key)
        .join(DeviceGroupMemberOf, DeviceGroupMemberOf.static_group_id == DeviceGroup.id)
        .join(source, source.id == DeviceGroupMemberOf.dynamic_group_id)
        .where(source.key == dynamic_key)
    )
    async with db_session_maker() as verify:
        return sorted((await verify.execute(stmt)).scalars().all())


async def fetch_orphan_reference_ids(
    db_session_maker: async_sessionmaker[AsyncSession],
    *,
    dynamic_key: str,
) -> list[str]:
    """The ``static_group_id``s the dynamic group references that no longer resolve.

    An outer join, deliberately. Every key-based read (including
    :func:`fetch_member_of_keys`) resolves the target *through* ``device_groups``,
    so a row pointing at a deleted id simply drops out of the result and the
    caller sees "no reference" rather than "broken reference" — the two states a
    dangling-reference guard exists to tell apart.
    """
    source = aliased(DeviceGroup, name="source")
    target = aliased(DeviceGroup, name="target")
    stmt = (
        select(DeviceGroupMemberOf.static_group_id)
        .join(source, source.id == DeviceGroupMemberOf.dynamic_group_id)
        .outerjoin(target, target.id == DeviceGroupMemberOf.static_group_id)
        .where(source.key == dynamic_key, target.id.is_(None))
    )
    async with db_session_maker() as verify:
        return sorted(str(row) for row in (await verify.execute(stmt)).scalars().all())


async def assert_no_dangling_reference(
    db_session_maker: async_sessionmaker[AsyncSession],
    *,
    static_key: str,
    dynamic_key: str,
) -> None:
    """Assert the dynamic group never ends up referencing a deleted static group.

    Meaningful only where the dynamic row can exist independently of whether the
    static row survives (e.g. the delete-vs-first-member_of file, where an
    ``update_group`` can add the reference). Do not reuse this for interleavings
    where one of the two outcomes always makes the guard vacuous — pin the exact
    expected end state there instead.

    One check, deliberately. A key-based read (including
    :func:`fetch_member_of_keys`) resolves the target *through* ``device_groups``,
    so a reference to a deleted id disappears from the result set instead of
    showing up as a violation -- ``static_key not in references`` is then true by
    construction, which is the vacuous-guard failure mode this helper's original
    body had against the JSON column and which a second key-based half here
    reproduced. The outer-join orphan read is the one that can actually catch a
    weakened ``fk_device_group_member_of_static_group``.
    """
    _static_row, dynamic_row = await fetch_group_rows(db_session_maker, static_key=static_key, dynamic_key=dynamic_key)
    assert dynamic_row is not None
    orphans = await fetch_orphan_reference_ids(db_session_maker, dynamic_key=dynamic_key)
    assert not orphans, f"dynamic group {dynamic_key} references group ids that no longer exist: {orphans}"


@asynccontextmanager
async def capture_statements(session: AsyncSession) -> AsyncIterator[list[str]]:
    """Collect the SQL *session* issues, and only what *session* issues.

    ``before_cursor_execute`` fires per engine, not per session, so a listener
    registered on the bare engine also collects statements from every other
    connection in the pool — a second session, a fixture's cleanup query, an
    event-bus flush. Callers here assert on statement *ordering*, so a stray
    ``device_groups`` read from an unrelated connection would fail the
    assertion against code that never issued it (and the reverse pollution
    could mask a real violation). Pin the listener to this session's own
    connection.

    That pin has to *follow* the session, not snapshot it. A Session returns its
    connection to the pool on commit or rollback and checks out a fresh
    ``Connection`` for the next statement, so an identity check against the
    connection held at entry silently stops recording after the first commit —
    and a dropped statement makes a negative assertion pass vacuously, the one
    direction a contract test must never fail in. ``after_begin`` fires with the
    connection backing each new transaction, and every 2.0-style statement begins
    one, so re-pinning there tracks the session across its whole lifetime without
    ever widening to the pool.
    """
    assert not session.in_transaction(), "capture_statements requires a session with no active transaction"
    statements: list[str] = []
    detach = pin_statement_listener(session, statements)
    try:
        yield statements
    finally:
        detach()


@contextmanager
def capture_engine_statements(session: AsyncSession) -> Iterator[list[str]]:
    """Collect every statement the *engine* behind *session* issues, from any connection.

    The opposite trade-off to :func:`capture_statements`, and the right one in
    exactly one situation: the work under test runs on a session the test does not
    hold -- an API request through ``client.get``/``client.post``, which opens its
    own session from the app's factory. Pinning to the caller's connection there
    records nothing at all, and a budget assertion over an empty list passes
    vacuously, which is the failure mode that matters most in a guard.

    The cost is real and is why this is a separate name rather than a flag: this
    also sees the event-bus flush, a fixture's cleanup query, and every other
    connection in the pool. It can carry a *comparative* assertion (a delta
    between two runs) or a *categorised* one (statements naming a table), never a
    raw total. Reach for :func:`capture_statements` whenever the statements come
    from the session the test already holds and that session has no transaction
    open yet.
    """
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


def pin_statement_listener(session: AsyncSession, sink: list[str]) -> Callable[[], None]:
    """Install the pinned listener pair on *session*; returns the detach callable.

    Split out of :func:`capture_statements` so a session handed out by a factory
    — one no test body can wrap in an ``async with`` — records through the same
    pin instead of a second, subtly different recorder.
    """
    # Single-element holder rather than a set: a connection this session has
    # released is no longer ours, and matching it would re-admit the pool
    # pollution the pin exists to prevent.
    own_connection: list[object | None] = [None]

    def track_begin(_session: object, _transaction: object, connection: object) -> None:
        own_connection[0] = connection

    def listener(
        conn: object,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        if conn is own_connection[0]:
            sink.append(statement)

    bind = session.bind
    assert bind is not None
    sync_engine = bind.sync_engine if hasattr(bind, "sync_engine") else bind
    sync_session = session.sync_session
    event.listen(sync_session, "after_begin", track_begin)
    event.listen(sync_engine, "before_cursor_execute", listener)

    def detach() -> None:
        event.remove(sync_engine, "before_cursor_execute", listener)
        event.remove(sync_session, "after_begin", track_begin)

    return detach
