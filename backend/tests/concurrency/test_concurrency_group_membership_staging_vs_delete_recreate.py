"""A delete+recreate of a static group mid-import must not stage a membership
against the stale (pre-recreate) group id.

``commit_import`` caches each static group's id in ``group_id_by_key`` when it
commits the definitions block, then runs the device loop without the
group-mutation lock, then re-acquires the lock in ``_stage_static_memberships``
and re-checks which static groups still exist before staging membership rows.

Re-checking **by key** is not enough: a concurrent ``delete_group(key)`` then
``create_group(key)`` (static, no ``member_of`` -> no advisory lock) replaces
the row with a new id while leaving the key present. The key still exists so
the old code skips it, but ``group_id_by_key`` still holds the stale id, and
``_plan_static_memberships`` plans ``DeviceGroupMembership(group_id=<stale>)``
-> the final commit violates ``device_group_memberships_group_id_fkey`` -> 500,
with the device rows already committed and their memberships lost.

The fix re-reads each static group's current id and skips any key whose id
changed (as well as any key whose row vanished). This test pins that: the
deleter deletes the static group and recreates it (new id) during the device
loop; the import must succeed, skip the membership for the recreated group,
and leave no membership row — rather than 500.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy import select

from app.devices.models.group import DeviceGroup, DeviceGroupMembership, GroupType
from app.devices.schemas.filters import DeviceGroupFilters
from app.devices.schemas.group import DeviceGroupCreate
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
    """One static, one dynamic group, one device in the static group.

    The dynamic group's ``member_of`` is empty, so the concurrent
    ``delete_group(static_key)`` has no referrer and succeeds — opening the
    window for the deleter to then recreate the static group with a new id.
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
    """Set *fired* once the device-row commit (commit #2) lands, then hold for HANDOFF_SEC."""
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


async def _membership_count(
    db_session_maker: async_sessionmaker[AsyncSession], device_id: uuid.UUID, group_key: str
) -> int:
    """Count membership rows joining the named device to a group with the given key."""
    async with db_session_maker() as verify:
        stmt = (
            select(DeviceGroupMembership.id)
            .join(DeviceGroup, DeviceGroupMembership.group_id == DeviceGroup.id)
            .where(DeviceGroupMembership.device_id == device_id, DeviceGroup.key == group_key)
        )
        rows = (await verify.execute(stmt)).all()
        return len(rows)


async def test_delete_and_recreate_during_membership_staging_does_not_500(
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

    async def delete_and_recreate_static() -> bool:
        await _wait_for_device_commit(device_committed)
        async with db_session_maker() as session:
            deleted = await build_groups_service().delete_group(session, static_key)
        if not deleted:
            return False
        # Recreate as a static group with no member_of -> create_group takes
        # the advisory lock with when=False, so this does not block on the
        # import's staging-phase lock acquire and commits with a new row id.
        async with db_session_maker() as session:
            await build_groups_service().create_group(
                session,
                DeviceGroupCreate(key=static_key, name=static_key, group_type=GroupType.static),
            )
        return True

    import_result, recreate_result = await asyncio.gather(
        run_import(), delete_and_recreate_static(), return_exceptions=True
    )

    assert not isinstance(import_result, Exception), (
        f"import must not raise when a concurrent delete+recreate changes a static "
        f"group's id during membership staging; got {import_result!r}"
    )
    assert recreate_result is True, f"deleter must delete and recreate the static group; got {recreate_result!r}"

    # The static group exists again (recreated with a new id), and the device
    # row was committed — but the membership for the recreated group must have
    # been skipped (stale id), leaving no membership row.
    assert any(skip.group_key == static_key for skip in import_result.memberships_skipped), (
        f"memberships_skipped must name the recreated static group; got {import_result.memberships_skipped!r}"
    )

    static_row, _dynamic_row = await fetch_group_rows(db_session_maker, static_key=static_key, dynamic_key=dynamic_key)
    assert static_row is not None, "static group must exist after the recreate"

    created_device_ids = [row.device_id for row in import_result.created]
    assert len(created_device_ids) == 1
    membership_count = await _membership_count(db_session_maker, created_device_ids[0], static_key)
    assert membership_count == 0, "no membership row may reference the recreated static group (stale id skipped)"
