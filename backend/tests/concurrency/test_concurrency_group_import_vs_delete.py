"""A bundle import must not leave its dynamic groups referencing a static group
a concurrent delete removed mid-import.

``commit_import`` used to commit static group definitions before the device loop
and insert the dynamic ones only after it. A ``delete_group`` landing in that gap
removed a static group the import had already committed, and the dynamic group
inserted afterwards dangled. The fix folds both inserts into one transaction
under the group-mutation advisory lock, so the deleter either sees both rows
(and is rejected) or neither.
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
from tests.concurrency.group_lock_helpers import (
    EVENT_WAIT_TIMEOUT_SEC,
    HANDOFF_SEC,
    build_groups_service,
    fetch_group_rows,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

pytestmark = [pytest.mark.db, pytest.mark.asyncio]


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

    Before the fix that first commit carries only the static group definitions;
    after it, both. Holding for ``HANDOFF_SEC`` gives the deleter time to run
    against whatever state that commit made visible.
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

    assert not isinstance(import_result, Exception), f"import should have succeeded: {import_result!r}"
    assert isinstance(delete_result, GroupReferencedError), (
        f"deleter must observe the imported reference, got {delete_result!r}"
    )

    static_row, dynamic_row = await fetch_group_rows(db_session_maker, static_key=static_key, dynamic_key=dynamic_key)
    assert static_row is not None, "the referenced static group must survive"
    assert dynamic_row is not None, "the dynamic group must survive"
    member_of = (dynamic_row.filters or {}).get("member_of", [])
    assert static_key in member_of
