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
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import TYPE_CHECKING

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
    from collections.abc import AsyncIterator

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


def _signaling_session_factory(
    inner: async_sessionmaker[AsyncSession],
    committed: asyncio.Event,
    *,
    fire_after_begin_call: int,
) -> async_sessionmaker[AsyncSession]:
    """A ``session_factory`` stand-in whose Nth ``.begin()`` transaction signals *committed*.

    ``commit_import`` no longer holds a caller session to intercept ``.commit()``
    on directly -- it owns its own sessions via the injected ``session_factory``
    and commits each one through ``session_factory.begin()``'s own context-manager
    exit, which never calls the ``AsyncSession.commit()`` method. Wrapping
    ``.begin()`` itself is the only hook point left: the code right after the
    inner ``async with session.begin():`` block runs exactly once that
    transaction has committed and before ``commit_import`` moves on to its next
    step, which is where *committed* fires and the handoff pause happens.

    ``commit_import``'s first ``session_factory.begin()`` call is always the
    definitions transaction (static groups, dynamic groups, and the
    ``device_group_member_of`` edges), so ``fire_after_begin_call=1`` pins
    exactly that commit.
    """
    begin_calls = 0

    class _SignalingFactory:
        def __call__(self) -> AsyncSession:
            return inner()

        @asynccontextmanager
        async def begin(self) -> AsyncIterator[AsyncSession]:
            nonlocal begin_calls
            begin_calls += 1
            is_target = begin_calls == fire_after_begin_call
            async with inner() as session, session.begin():
                yield session
            if is_target and not committed.is_set():
                committed.set()
                await asyncio.sleep(HANDOFF_SEC)

    return _SignalingFactory()  # type: ignore[return-value]


async def _wait_for_import_commit(committed: asyncio.Event) -> None:
    """Await *committed* with a bounded timeout instead of hanging forever.

    ``committed`` is only ever set by ``_signaling_session_factory``'s wrapped
    ``.begin()``. If ``commit_import`` ever stops opening its definitions
    transaction through ``session_factory.begin()`` — e.g. the fold this test
    pins is undone and a later refactor changes it again — that signal would
    never fire and a bare ``await committed.wait()`` would hang the test run
    forever (``pytest-timeout`` is deliberately not a dependency here). Fail
    fast with a message that names the likely cause instead.
    """
    try:
        await asyncio.wait_for(committed.wait(), timeout=EVENT_WAIT_TIMEOUT_SEC)
    except TimeoutError:
        pytest.fail(
            f"import: never observed commit_import's first session_factory.begin() commit "
            f"within {EVENT_WAIT_TIMEOUT_SEC}s. The signal in _signaling_session_factory "
            "likely no longer fires on the definitions-transaction commit path."
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
        factory = _signaling_session_factory(db_session_maker, import_committed, fire_after_begin_call=1)
        service = PortabilityImportService(verification_enqueuer=VerificationService(), session_factory=factory)
        return await service.commit_import(request)

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
