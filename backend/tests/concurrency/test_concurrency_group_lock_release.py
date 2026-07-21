"""Every rejected group write must release the advisory lock immediately.

``acquire_group_mutation_lock`` is transaction-scoped, so a writer that returns
or raises without committing or rolling back keeps a *fleet-global* lock until
the session closes — for an API request, that is after response serialization.
A client hammering PATCH/DELETE on unknown keys would then serialise every
group write in the system for the tail of each request.

These tests assert the release *while the rejecting session is still open*.
That distinction is the whole point: the sibling concurrency tests call the
service inside ``async with db_session_maker() as session``, and that context
manager's exit rolls back — silently releasing a lock the service should have
released itself, and hiding exactly this defect.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from app.core.locks import acquire_group_mutation_lock
from app.devices.models.group import DeviceGroup, GroupType
from app.devices.schemas.group import DeviceGroupCreate, DeviceGroupUpdate
from app.devices.services.groups import GroupReferencedError, UnknownMemberOfError
from app.portability.schemas import (
    ExportBundle,
    ExportedDevice,
    ExportedDeviceGroup,
    ImportCommitRequest,
    ImportMapping,
    OriginalHost,
)
from app.portability.services.hash import compute_bundle_hash
from app.portability.services.import_bundle import PortabilityImportService
from app.verification.services.service import VerificationService
from tests.concurrency.group_lock_helpers import build_groups_service
from tests.helpers import seed_host_named

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

# Generous: a released lock is acquired in microseconds, so any real wait here
# means the lock is still held. Only bounds the failure, never the pass.
_ACQUIRE_TIMEOUT_SEC = 5.0


async def _assert_lock_is_free(
    db_session_maker: async_sessionmaker[AsyncSession],
    *,
    after: str,
) -> None:
    """Fail unless a fresh transaction can take the group-mutation lock now."""
    async with db_session_maker() as peer:
        try:
            await asyncio.wait_for(acquire_group_mutation_lock(peer), timeout=_ACQUIRE_TIMEOUT_SEC)
        except TimeoutError:
            pytest.fail(
                f"the group-mutation advisory lock was still held after {after}. "
                "That writer returned without commit or rollback, so a fleet-global "
                "lock survives until the session closes at request teardown."
            )
        await peer.rollback()


async def _seed_referenced_pair(db_session: AsyncSession) -> tuple[str, str]:
    suffix = uuid.uuid4().hex[:8]
    static_key = f"static-{suffix}"
    dynamic_key = f"dynamic-{suffix}"
    db_session.add(DeviceGroup(key=static_key, name=static_key, group_type=GroupType.static))
    db_session.add(
        DeviceGroup(
            key=dynamic_key,
            name=dynamic_key,
            group_type=GroupType.dynamic,
            filters={"member_of": [static_key]},
        )
    )
    await db_session.commit()
    return static_key, dynamic_key


async def test_delete_of_referenced_group_releases_the_lock(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """The 409 path: _assert_no_references raises after the lock is taken."""
    static_key, _dynamic_key = await _seed_referenced_pair(db_session)
    service = build_groups_service()

    async with db_session_maker() as session:
        with pytest.raises(GroupReferencedError):
            await service.delete_group(session, static_key)
        await _assert_lock_is_free(db_session_maker, after="a rejected delete_group (GroupReferencedError)")


async def test_delete_of_unknown_group_releases_the_lock(
    db_session_maker: async_sessionmaker[AsyncSession],
) -> None:
    service = build_groups_service()
    async with db_session_maker() as session:
        assert await service.delete_group(session, f"missing-{uuid.uuid4().hex[:8]}") is False
        await _assert_lock_is_free(db_session_maker, after="delete_group on an unknown key")


async def test_update_of_unknown_group_releases_the_lock(
    db_session_maker: async_sessionmaker[AsyncSession],
) -> None:
    service = build_groups_service()
    async with db_session_maker() as session:
        result = await service.update_group(
            session,
            f"missing-{uuid.uuid4().hex[:8]}",
            DeviceGroupUpdate(description="never applied"),
        )
        assert result is None
        await _assert_lock_is_free(db_session_maker, after="update_group on an unknown key")


async def test_update_with_unresolvable_member_of_releases_the_lock(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _static_key, dynamic_key = await _seed_referenced_pair(db_session)
    service = build_groups_service()

    async with db_session_maker() as session:
        with pytest.raises(UnknownMemberOfError):
            await service.update_group(
                session,
                dynamic_key,
                DeviceGroupUpdate(filters={"member_of": [f"nope-{uuid.uuid4().hex[:8]}"]}),  # type: ignore[arg-type]
            )
        await _assert_lock_is_free(db_session_maker, after="update_group with an unresolvable member_of")


async def test_create_with_unresolvable_member_of_releases_the_lock(
    db_session_maker: async_sessionmaker[AsyncSession],
) -> None:
    service = build_groups_service()
    suffix = uuid.uuid4().hex[:8]

    async with db_session_maker() as session:
        with pytest.raises(UnknownMemberOfError):
            await service.create_group(
                session,
                DeviceGroupCreate(
                    key=f"dynamic-{suffix}",
                    name=f"dynamic-{suffix}",
                    group_type=GroupType.dynamic,
                    filters={"member_of": [f"nope-{suffix}"]},  # type: ignore[arg-type]
                ),
            )
        await _assert_lock_is_free(db_session_maker, after="create_group with an unresolvable member_of")


async def test_dynamic_count_scans_run_after_definition_transactions(
    db_session_maker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = build_groups_service()
    original_count = service._dynamic_device_count
    observed: list[str] = []

    async def count_outside_transaction(db: AsyncSession, group: DeviceGroup) -> int:
        assert not db.in_transaction(), "dynamic count started before the definition transaction released its lock"
        key = group.key
        count = await original_count(db, group)
        assert not db.in_transaction(), "dynamic count left its read transaction open"
        observed.append(key)
        return count

    monkeypatch.setattr(service, "_dynamic_device_count", count_outside_transaction)
    suffix = uuid.uuid4().hex[:8]

    async with db_session_maker() as session:
        static = await service.create_group(
            session,
            DeviceGroupCreate(key=f"static-{suffix}", name="static", group_type=GroupType.static),
        )
        dynamic = await service.create_group(
            session,
            DeviceGroupCreate(
                key=f"dynamic-{suffix}",
                name="dynamic",
                group_type=GroupType.dynamic,
                filters={"member_of": [static["key"]]},  # type: ignore[arg-type]
            ),
        )
        updated = await service.update_group(
            session,
            dynamic["key"],
            DeviceGroupUpdate(description="updated"),
        )

    assert updated is not None
    assert observed == [dynamic["key"], dynamic["key"]]


async def test_import_with_no_groups_does_not_hold_the_lock(
    db_session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """A groupless bundle takes no group-mutation lock and must not leave one held.

    The new membership-staging acquire in ``commit_import`` is guarded by an
    ``if group_id_by_key:`` check (the same guard the definition block already
    uses), so a bundle with no groups never acquires. A peer must be able to
    take the lock immediately after a groupless import returns.
    """
    bundle = ExportBundle(
        schema_version=2,
        exported_at=datetime.now(UTC),
        source_instance="alpha",
        groups=[],
        devices=[],
    )
    request = ImportCommitRequest(bundle=bundle, bundle_hash=compute_bundle_hash(bundle), mappings=[])
    async with db_session_maker() as session:
        result = await PortabilityImportService(verification_enqueuer=VerificationService()).commit_import(
            session, request
        )
        assert result.created == [] and result.skipped == [] and result.failed == []
        # Inside the session, per this module's docstring: the context manager's
        # exit rolls back, which would release a leaked lock and pass vacuously.
        await _assert_lock_is_free(db_session_maker, after="a groupless commit_import")


async def test_import_releases_the_lock_after_membership_staging(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    seeded_driver_packs: None,
) -> None:
    """The membership-staging acquire is released after the final commit.

    Pins that the new acquire (Task 7) does not leak past ``commit_import``'s
    return. A bundle with one static group and one device exercises the
    membership-staging path; on success the lock must be free.
    """
    suffix = uuid.uuid4().hex[:8]
    static_key = f"static-{suffix}"
    host = await seed_host_named(db_session, "import-host")
    bundle = ExportBundle(
        schema_version=2,
        exported_at=datetime.now(UTC),
        source_instance="alpha",
        groups=[ExportedDeviceGroup(key=static_key, name=static_key, group_type=GroupType.static)],
        devices=[
            ExportedDevice(
                pack_id="appium-uiautomator2",
                platform_id="android_mobile",
                identity_scheme="android_serial",
                identity_scope="host",
                identity_value=f"device-{suffix}",
                name="Pixel",
                device_type="real_device",
                connection_type="usb",
                static_groups=[static_key],
                original_host=OriginalHost(hostname="import-host", host_id=str(host.id)),
            ),
        ],
    )
    request = ImportCommitRequest(
        bundle=bundle,
        bundle_hash=compute_bundle_hash(bundle),
        mappings=[ImportMapping(index=0, target_host_id=host.id)],
    )
    async with db_session_maker() as session:
        await PortabilityImportService(verification_enqueuer=VerificationService()).commit_import(session, request)
        # Inside the session: see the sibling test and this module's docstring.
        await _assert_lock_is_free(db_session_maker, after="commit_import with membership staging")
