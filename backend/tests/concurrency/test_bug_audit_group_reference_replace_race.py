"""Two concurrent ``update_group`` calls on one dynamic group must not union
their ``member_of`` sets.

``_replace_member_of`` is a delete-then-insert, which is last-writer-wins only
if the two writers are ordered. Unordered, each one's ``DELETE FROM
device_group_member_of`` plans against a snapshot in which the other's INSERT has
not committed, so it removes nothing of the peer's and adds its own: a group the
operator set to ``member_of: [a]`` and then to ``member_of: [b]`` ends up
matching devices in *both*, and no error is reported to either caller.

The group-mutation advisory lock used to hide this by serialising every
definition writer in the fleet. Nothing about the foreign keys replaces it —
they constrain what an edge may point at, not how many edges a replacement
leaves behind — so ``update_group`` takes the source row's ``FOR UPDATE`` for
this, the same way ``add_members`` does for its own duplicate-insert race.

The flush is not a substitute. A ``member_of``-only payload leaves ``filters``
at the value it already had, SQLAlchemy omits unchanged columns, and with no
other field in the payload the flush emits no statement against
``device_groups`` at all — so there is no incidental row lock to inherit.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import TYPE_CHECKING, Any

import pytest

from app.devices.models.group import DeviceGroup, GroupType
from app.devices.schemas.group import DeviceGroupUpdate
from tests.concurrency.group_lock_helpers import build_groups_service, fetch_member_of_keys

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

# A bound on the gather, not a race parameter: two writers contending for one
# row settle in milliseconds, and a legible timeout beats a hung run.
RACE_TIMEOUT_SEC = 10.0

# The race is unsynchronised on purpose — there is no seam to hang an event on
# that would not also impose the ordering under test. Repeating instead: the
# defect reproduced on every attempt before the row lock, so a handful of runs
# is ample, and each one is a real interleaving rather than a staged one.
ATTEMPTS = 5


async def _seed(db_session: AsyncSession) -> tuple[str, str, str]:
    suffix = uuid.uuid4().hex[:8]
    first, second, dynamic_key = f"static-a-{suffix}", f"static-b-{suffix}", f"dynamic-{suffix}"
    db_session.add_all(
        [
            DeviceGroup(key=first, name=first, group_type=GroupType.static),
            DeviceGroup(key=second, name=second, group_type=GroupType.static),
            DeviceGroup(key=dynamic_key, name=dynamic_key, group_type=GroupType.dynamic),
        ]
    )
    await db_session.commit()
    return first, second, dynamic_key


async def _race_one_attempt(
    db_session_maker: async_sessionmaker[AsyncSession],
    *,
    dynamic_key: str,
    first: str,
    second: str,
) -> tuple[list[str], list[list[str]]]:
    """Replace *dynamic_key*'s reference with *first* and *second* at once.

    Returns the edges that survived and the ``member_of`` list each caller was
    handed back.
    """
    service = build_groups_service()

    async def replace(target: str) -> dict[str, Any] | None:
        async with db_session_maker() as session:
            return await service.update_group(
                session,
                dynamic_key,
                DeviceGroupUpdate(filters={"member_of": [target]}),  # type: ignore[arg-type]
            )

    results = await asyncio.wait_for(
        asyncio.gather(replace(first), replace(second), return_exceptions=True),
        timeout=RACE_TIMEOUT_SEC,
    )
    for result in results:
        assert not isinstance(result, Exception), f"an uncontested update raised {result!r}"
    references = await fetch_member_of_keys(db_session_maker, dynamic_key=dynamic_key)
    payloads = [result["filters"]["member_of"] for result in results if isinstance(result, dict)]
    return references, payloads


async def test_concurrent_reference_replacements_do_not_union(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
) -> None:
    for attempt in range(ATTEMPTS):
        first, second, dynamic_key = await _seed(db_session)
        references, payloads = await _race_one_attempt(
            db_session_maker, dynamic_key=dynamic_key, first=first, second=second
        )

        assert references in ([first], [second]), (
            f"attempt {attempt}: both replacements survived, so neither is the group's definition: {references}"
        )
        # ``payloads`` is always ``[[first], [second]]`` — each caller echoes only
        # its own requested target and never re-reads — so once the assertion
        # above holds, this one follows from it rather than checking anything
        # independent. What it does still catch is either caller echoing a
        # merged ``[first, second]`` back instead of its own single target. The
        # loser echoing its own committed-then-superseded target is correct
        # HTTP semantics, not a bug.
        assert references in payloads, f"attempt {attempt}: no caller was told what actually landed: {payloads}"
