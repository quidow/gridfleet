"""Static membership rows staged by an import must not violate the membership
FK when a concurrent delete removes the static group mid-import.

``commit_import`` commits group definitions under the group-mutation lock, then
runs the device loop (per-row commits, no lock), then stages and commits
``device_group_memberships`` rows for each created device's bundle static
groups. That last commit is outside the lock, so a ``delete_group`` landing
between the device-loop commits and the membership commit removes a static
group the membership rows are about to reference — and the final commit
violates ``device_group_memberships_group_id_fkey``, surfacing as a 500 with
the device rows already committed.

The bundle here carries a single static group listed in the device's
``static_groups`` and NO dynamic group referencing it, so the concurrent
``delete_group`` has no referrer and succeeds — the membership commit then
hits the FK violation. (A dynamic group with ``member_of=[static_key]`` would
make ``delete_group`` raise ``GroupReferencedError`` and the race would be
unreachable; see the task-4 brief's "failing the right way" note.)

The fix (Task 7) re-acquires the group-mutation lock around the
membership-staging commit and re-checks that each static group still exists,
skipping memberships for any group deleted in the window. This test pins that
behaviour: the import must succeed (device rows committed, memberships skipped
gracefully) rather than 500.
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
    device's ``static_groups`` is what drives ``_insert_static_memberships``
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
    assert isinstance(delete_result, GroupReferencedError) or delete_result is True, (
        f"deleter must either be rejected (GroupReferencedError) or succeed; got {delete_result!r}"
    )

    static_row, dynamic_row = await fetch_group_rows(db_session_maker, static_key=static_key, dynamic_key=dynamic_key)
    # The two legitimate end states:
    # - deleter won: static gone, device row committed, no membership row.
    # - deleter lost (GroupReferencedError): static and dynamic both survive,
    #   device row committed, membership row present.
    if isinstance(delete_result, GroupReferencedError):
        assert static_row is not None, "static group must survive when delete was rejected"
        assert dynamic_row is not None, "dynamic group must survive when delete was rejected"
    else:
        assert delete_result is True
        assert static_row is None, "static group must be gone when delete succeeded"
