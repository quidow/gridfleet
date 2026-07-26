"""A bundle import must not leave its dynamic groups referencing a static group
a concurrent delete removed mid-import.

What this pins is the *ordering of the edge rows*: ``commit_import`` stages both
group definitions **and** their ``device_group_member_of`` rows inside one
transaction and commits them together. Any arrangement that publishes a static
group before its referring edge — a second commit for the dynamic definitions, a
separate membership-style pass for the edges — reopens a window in which a
``delete_group`` sees an unreferenced target, removes it, and the edge inserted
afterwards has nothing to point at. The restrictive foreign key would then fail
the import's own INSERT rather than corrupting anything, but the operator gets a
500 on a half-applied bundle instead of a bundle that either landed or did not.

Because the edge is committed with the definitions, the deleter can never win
this race: it is released on the import's first commit, and that commit already
carries the reference. The test asserts that single terminal state rather than
branching on an interleaving that cannot occur.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest

from app.devices.models.group import GroupType
from app.devices.schemas.filters import DeviceGroupFilters
from app.devices.services.groups import GroupReferencedError
from app.portability.schemas import ExportBundle, ExportedDeviceGroup, ImportCommitRequest, ImportCommitResult
from app.portability.services.hash import compute_bundle_hash
from app.portability.services.import_bundle import PortabilityImportService
from app.verification.services.service import VerificationService
from tests.concurrency.group_lock_helpers import build_groups_service, fetch_group_rows, fetch_member_of_keys

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

# Long enough for the released deleter to reach its own statements while the
# import's first commit is still the most recent thing to have landed. Only
# widens the window; the asserted outcome does not depend on the exact value.
HANDOFF_SEC = 0.5

# A bound, not a race parameter: comfortably above HANDOFF_SEC so it never trips
# under normal timing, but it turns a seam that stopped firing into a legible
# TimeoutError rather than a hung run (``pytest-timeout`` is deliberately not a
# dependency here).
EVENT_WAIT_TIMEOUT_SEC = 5.0


def _groups_bundle(static_key: str, dynamic_key: str) -> ExportBundle:
    return ExportBundle(
        schema_version=2,
        exported_at=datetime.now(UTC),
        source_instance="alpha",
        groups=[
            ExportedDeviceGroup(key=static_key, name=static_key, group_type=GroupType.static),
            ExportedDeviceGroup(
                key=dynamic_key,
                name=dynamic_key,
                group_type=GroupType.dynamic,
                filters=DeviceGroupFilters(member_of=[static_key]),
            ),
        ],
        devices=[],
    )


def _signal_after_first_commit(session: AsyncSession, committed: asyncio.Event) -> None:
    """Set *committed* once *session* has committed for the first time.

    That first commit is ``commit_import``'s definition transaction: static
    groups, dynamic groups, and the ``device_group_member_of`` rows joining
    them. Holding for ``HANDOFF_SEC`` gives the deleter time to run against
    exactly the state it published — which is the point, since an arrangement
    that published the target without its edge would hand the deleter a
    deletable row here.
    """
    original_commit = session.commit
    fired = False

    async def _intercepted(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        nonlocal fired
        result = await original_commit(*args, **kwargs)
        if not fired:
            fired = True
            committed.set()
            await asyncio.sleep(HANDOFF_SEC)
        return result

    session.commit = _intercepted  # type: ignore[assignment, method-assign]


async def _wait_for_import_commit(committed: asyncio.Event) -> None:
    """Await *committed* with a bounded timeout instead of hanging forever.

    ``committed`` is only ever set by ``_signal_after_first_commit``'s override of
    ``session.commit``. If ``commit_import`` ever stops calling ``session.commit``
    on this path — e.g. the fold this test pins is undone and a later refactor
    changes it again — that override would never fire and a bare
    ``await committed.wait()`` would hang the test run forever
    (``pytest-timeout`` is deliberately not a dependency here). Fail fast with a
    message that names the likely cause instead.
    """
    try:
        await asyncio.wait_for(committed.wait(), timeout=EVENT_WAIT_TIMEOUT_SEC)
    except TimeoutError:
        pytest.fail(
            f"import: never observed commit_import's first session.commit within "
            f"{EVENT_WAIT_TIMEOUT_SEC}s. The session.commit override in "
            "_signal_after_first_commit likely no longer fires on the commit path."
        )


async def test_delete_during_import_cannot_orphan_a_dynamic_group(
    db_session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """A delete released on the import's first commit must find the edge already there.

    The bundle carries one static group and one dynamic group whose ``member_of``
    names it. ``commit_import`` inserts both definitions and the
    ``device_group_member_of`` row joining them, then commits once; the deleter
    is released on that commit and must be refused by name.
    """
    suffix = uuid.uuid4().hex[:8]
    static_key = f"static-{suffix}"
    dynamic_key = f"dynamic-{suffix}"
    bundle = _groups_bundle(static_key, dynamic_key)
    request = ImportCommitRequest(
        bundle=bundle,
        bundle_hash=compute_bundle_hash(bundle),
        mappings=[],
    )
    import_committed = asyncio.Event()

    async def run_import() -> ImportCommitResult:
        async with db_session_maker() as session:
            _signal_after_first_commit(session, import_committed)
            return await PortabilityImportService(verification_enqueuer=VerificationService()).commit_import(
                session, request
            )

    async def delete_static() -> bool:
        await _wait_for_import_commit(import_committed)
        async with db_session_maker() as session:
            return await build_groups_service().delete_group(session, static_key)

    import_result, delete_result = await asyncio.gather(run_import(), delete_static(), return_exceptions=True)

    # One terminal state, not two. The deleter is released on the commit that
    # publishes the edge, so it can only ever observe a referenced target. There
    # is no "delete wins" branch to assert: an interleaving that produced one
    # would mean the definitions and their device_group_member_of rows had been
    # split across commits again, which is the regression this file exists to
    # catch — so it has to fail here rather than be tolerated as an alternative.
    assert not isinstance(import_result, Exception), f"the import must land intact, got {import_result!r}"
    assert isinstance(delete_result, GroupReferencedError), (
        f"deleter must observe the imported reference, got {delete_result!r}"
    )
    assert delete_result.dependents == [dynamic_key], (
        f"the 409 must name the imported referrer, got {delete_result.dependents!r}"
    )

    static_row, dynamic_row = await fetch_group_rows(db_session_maker, static_key=static_key, dynamic_key=dynamic_key)
    assert static_row is not None, "the referenced static group must survive the rejected delete"
    assert dynamic_row is not None, "the imported dynamic group must survive"
    assert await fetch_member_of_keys(db_session_maker, dynamic_key=dynamic_key) == [static_key], (
        f"the imported dynamic group must still reference {static_key}"
    )
