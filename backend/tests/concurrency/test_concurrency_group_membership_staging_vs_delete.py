"""Static membership rows staged by an import must not violate the membership
FK when a concurrent delete removes the static group mid-import.

``commit_import`` commits group definitions under the group-mutation lock, then
runs the device loop (per-row commits, no lock), then stages and commits
``device_group_memberships`` rows for each created device's bundle static
groups. The definition lock is released before the device loop, so a
``delete_group`` landing between the device-loop commits and the membership
write removes a static group the membership rows are about to reference — and
that write violates ``device_group_memberships_group_id_fkey``, surfacing as a
500 with the device rows already committed. (The violation lands on the INSERT,
not the commit: these FKs are non-deferrable.)

The bundle here carries a single static group listed in the device's
``static_groups`` and NO dynamic group referencing it, so the concurrent
``delete_group`` has no referrer and succeeds — the membership commit then
hits the FK violation. (A dynamic group with ``member_of=[static_key]`` would
make ``delete_group`` raise ``GroupReferencedError`` and the race would be
unreachable; see the task-4 brief's "failing the right way" note.)

The fix re-acquires the group-mutation lock around the membership write and
re-checks that each static group still exists, skipping memberships for any
group deleted in the window. These tests pin that behaviour: the import must
succeed (device rows committed, memberships reported as skipped) rather than
500 — and must report *which* memberships it dropped, not merely survive.

The device-deleted case is the residual the lock cannot close, since nothing
serialises a device delete against an import; it is covered here too.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy import delete, select

from app.devices.models import Device, DeviceGroup, DeviceGroupMembership
from app.devices.models.group import GroupType
from app.devices.schemas.filters import DeviceGroupFilters
from app.portability.schemas import (
    ExportBundle,
    ExportedDevice,
    ExportedDeviceGroup,
    ImportCommitRequest,
    ImportCommitResult,
    ImportMapping,
    OriginalHost,
)
from app.portability.services.hash import compute_bundle_hash
from app.portability.services.import_bundle import PortabilityImportService
from app.verification.services.service import VerificationService
from tests.concurrency.group_lock_helpers import (
    EVENT_WAIT_TIMEOUT_SEC,
    HANDOFF_SEC,
    build_groups_service,
    fetch_group_rows,
)
from tests.helpers import seed_host_named

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

pytestmark = [pytest.mark.db, pytest.mark.asyncio]


def _device(identity_value: str, hostname: str, host_id: uuid.UUID, static_groups: list[str]) -> ExportedDevice:
    return ExportedDevice(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value=identity_value,
        name="Pixel",
        device_type="real_device",
        connection_type="usb",
        static_groups=static_groups,
        original_host=OriginalHost(hostname=hostname, host_id=str(host_id)),
    )


def _static_group(key: str) -> ExportedDeviceGroup:
    return ExportedDeviceGroup(key=key, name=key.replace("-", " "), group_type=GroupType.static)


def _dynamic_group(key: str, member_of: list[str]) -> ExportedDeviceGroup:
    return ExportedDeviceGroup(
        key=key,
        name=key.replace("-", " "),
        group_type=GroupType.dynamic,
        filters=DeviceGroupFilters(member_of=member_of),
    )


def _bundle_with_device(
    static_key: str, dynamic_key: str, host_id: uuid.UUID
) -> tuple[ExportBundle, list[ImportMapping]]:
    """A bundle with one static, one dynamic group, and one device in the static group.

    The dynamic group does NOT reference the static group (its ``member_of`` is
    empty), so a concurrent ``delete_group(static_key)`` has no referrer and
    succeeds — opening the membership-staging window the test exercises. The
    device's ``static_groups`` is what drives ``_plan_static_memberships``
    to stage a row. Without a device row the membership-staging commit is a
    no-op and the race cannot be exercised.
    """
    device = _device(
        identity_value=f"device-{uuid.uuid4().hex[:8]}",
        hostname="import-host",
        host_id=host_id,
        static_groups=[static_key],
    )
    bundle = ExportBundle(
        schema_version=2,
        exported_at=datetime.now(UTC),
        source_instance="alpha",
        groups=[
            _static_group(static_key),
            _dynamic_group(dynamic_key, member_of=[]),
        ],
        devices=[device],
    )
    mappings = [ImportMapping(index=0, target_host_id=host_id)]
    return bundle, mappings


def _signal_after_device_loop_commit(session: AsyncSession, fired: asyncio.Event) -> None:
    """Set *fired* once the device-row commit lands, then hold for HANDOFF_SEC.

    The bundle has exactly one device, so the commit sequence in
    ``commit_import`` is: (1) group definitions [inside lock], (2) the one
    device row, (3) staged memberships. We fire on commit #2 — the device row
    commit — and hold inside the interception so the deleter runs before
    commit #3 (memberships) executes.
    """
    original_commit = session.commit
    count = 0

    async def _intercepted(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        nonlocal count
        result = await original_commit(*args, **kwargs)
        count += 1
        if count == 2 and not fired.is_set():
            fired.set()
            await asyncio.sleep(HANDOFF_SEC)
        return result

    session.commit = _intercepted  # type: ignore[assignment, method-assign]


async def _wait_for_device_commit(fired: asyncio.Event) -> None:
    try:
        await asyncio.wait_for(fired.wait(), timeout=EVENT_WAIT_TIMEOUT_SEC)
    except TimeoutError:
        pytest.fail(
            f"import: never observed the device-row commit (commit #2) within "
            f"{EVENT_WAIT_TIMEOUT_SEC}s. The session.commit override in "
            "_signal_after_device_loop_commit likely no longer fires — did the "
            "commit count in commit_import change?"
        )


async def test_delete_during_membership_staging_does_not_500(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    seeded_driver_packs: None,
) -> None:
    _ = seeded_driver_packs
    suffix = uuid.uuid4().hex[:8]
    static_key = f"static-{suffix}"
    dynamic_key = f"dynamic-{suffix}"

    host = await seed_host_named(db_session, "import-host")

    bundle, mappings = _bundle_with_device(static_key, dynamic_key, host.id)
    request = ImportCommitRequest(
        bundle=bundle,
        bundle_hash=compute_bundle_hash(bundle),
        mappings=mappings,
    )
    device_committed = asyncio.Event()

    async def run_import() -> ImportCommitResult:
        async with db_session_maker() as session:
            _signal_after_device_loop_commit(session, device_committed)
            return await PortabilityImportService(verification_enqueuer=VerificationService()).commit_import(
                session, request
            )

    async def delete_static() -> bool:
        await _wait_for_device_commit(device_committed)
        async with db_session_maker() as session:
            return await build_groups_service().delete_group(session, static_key)

    import_result, delete_result = await asyncio.gather(run_import(), delete_static(), return_exceptions=True)

    assert not isinstance(import_result, Exception), (
        f"import must not raise when a concurrent delete removes a static group "
        f"during membership staging; got {import_result!r}"
    )
    # The deleter always wins here, and deliberately so: `_bundle_with_device`
    # gives the dynamic group `member_of=[]`, which `_group_filters_payload`
    # collapses to `filters=None`, so `delete_group`'s `has_key("member_of")`
    # scan finds no referrer. A `GroupReferencedError` branch would be dead
    # code — the reference case belongs to the tests that seed one.
    assert delete_result is True, f"deleter must succeed against an unreferenced static group; got {delete_result!r}"

    static_row, _dynamic_row = await fetch_group_rows(db_session_maker, static_key=static_key, dynamic_key=dynamic_key)
    assert static_row is None, "static group must be gone when delete succeeded"

    # Not raising is only half the contract. The operator has to be told which
    # memberships were dropped, and no row may survive pointing at the deleted
    # group. Without these, stripping the `stale_keys` reporting from
    # `_stage_static_memberships` and keeping only the filtering stays green.
    assert len(import_result.created) == 1, "the device row committed before the delete and must still be reported"
    assert [(s.index, s.group_key) for s in import_result.memberships_skipped] == [(0, static_key)], (
        f"the skipped membership must name the deleted group: {import_result.memberships_skipped!r}"
    )
    assert "deleted during import" in import_result.memberships_skipped[0].reason

    async with db_session_maker() as verify_session:
        surviving = (await verify_session.execute(select(DeviceGroupMembership))).scalars().all()
    assert surviving == [], f"no membership row may survive the deleted group: {surviving!r}"


async def test_concurrent_add_members_during_staging_keeps_the_memberships(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    seeded_driver_packs: None,
) -> None:
    """An operator adding an imported device to its bundle group must not void the batch.

    Same window as the delete test, different peer: ``add_members`` inserts the
    exact ``(group_id, device_id)`` pair the import is about to stage. A plain
    INSERT would violate the unique constraint and roll back *every* membership
    in the import, so a bundle importing many devices loses all of them to one
    operator click. Staging uses ``ON CONFLICT DO NOTHING`` — the same idiom
    ``add_members`` itself uses — so the row survives exactly once and the rest
    of the batch commits.
    """
    _ = seeded_driver_packs
    suffix = uuid.uuid4().hex[:8]
    static_key = f"static-{suffix}"
    dynamic_key = f"dynamic-{suffix}"

    host = await seed_host_named(db_session, "import-host")
    bundle, mappings = _bundle_with_device(static_key, dynamic_key, host.id)
    request = ImportCommitRequest(bundle=bundle, bundle_hash=compute_bundle_hash(bundle), mappings=mappings)
    device_committed = asyncio.Event()

    async def run_import() -> ImportCommitResult:
        async with db_session_maker() as session:
            _signal_after_device_loop_commit(session, device_committed)
            return await PortabilityImportService(verification_enqueuer=VerificationService()).commit_import(
                session, request
            )

    async def add_the_same_member() -> int | None:
        await _wait_for_device_commit(device_committed)
        async with db_session_maker() as session:
            device_id = (
                await session.execute(
                    select(Device.id).where(Device.identity_value == bundle.devices[0].identity_value)
                )
            ).scalar_one()
            return await build_groups_service().add_members(session, static_key, [device_id])

    import_result, add_result = await asyncio.gather(run_import(), add_the_same_member(), return_exceptions=True)

    assert not isinstance(import_result, Exception), (
        f"import must not raise on a conflicting peer add; got {import_result!r}"
    )
    assert not isinstance(add_result, Exception), f"add_members must not raise; got {add_result!r}"
    assert import_result.memberships_skipped == [], (
        f"a benign unique-constraint conflict must not be reported as a skipped membership: "
        f"{import_result.memberships_skipped!r}"
    )

    # Exactly one membership row, whichever writer laid it down.
    async with db_session_maker() as verify_session:
        rows = (
            (
                await verify_session.execute(
                    select(DeviceGroupMembership)
                    .join(DeviceGroup, DeviceGroup.id == DeviceGroupMembership.group_id)
                    .where(DeviceGroup.key == static_key)
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 1, f"expected exactly one membership row, got {len(rows)}"


async def test_device_deleted_during_staging_does_not_500(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    seeded_driver_packs: None,
) -> None:
    """A device deleted mid-import must cost its memberships, not the whole result.

    ``device_group_memberships.device_id`` is a plain, non-deferrable FK, so the
    violation fires when the staging INSERT executes, not when the transaction
    commits. Nothing serialises a device delete against an import, so this is
    the one IntegrityError the staging block still has to absorb — and the
    device rows it would discard have already committed.
    """
    _ = seeded_driver_packs
    suffix = uuid.uuid4().hex[:8]
    static_key = f"static-{suffix}"
    dynamic_key = f"dynamic-{suffix}"

    host = await seed_host_named(db_session, "import-host")
    bundle, mappings = _bundle_with_device(static_key, dynamic_key, host.id)
    request = ImportCommitRequest(bundle=bundle, bundle_hash=compute_bundle_hash(bundle), mappings=mappings)
    device_committed = asyncio.Event()

    async def run_import() -> ImportCommitResult:
        async with db_session_maker() as session:
            _signal_after_device_loop_commit(session, device_committed)
            return await PortabilityImportService(verification_enqueuer=VerificationService()).commit_import(
                session, request
            )

    async def delete_the_device() -> None:
        await _wait_for_device_commit(device_committed)
        async with db_session_maker() as session:
            await session.execute(delete(Device).where(Device.identity_value == bundle.devices[0].identity_value))
            await session.commit()

    import_result, _ = await asyncio.gather(run_import(), delete_the_device(), return_exceptions=True)

    assert not isinstance(import_result, Exception), (
        f"import must absorb the membership FK violation and still report its device rows; got {import_result!r}"
    )
    assert len(import_result.created) == 1, "the device row committed before the delete and must still be reported"
    assert [(s.index, s.group_key) for s in import_result.memberships_skipped] == [(0, static_key)], (
        f"the dropped membership must be reported: {import_result.memberships_skipped!r}"
    )
