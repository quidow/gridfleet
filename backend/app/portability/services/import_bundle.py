"""Validate and commit device portability bundles.

Validate is read-only: parse the bundle, classify each row, suggest a host per
row. Commit (T8) re-parses from the original bundle and inserts rows in
per-row transactions with verification enqueue.
"""

from __future__ import annotations

import logging
from collections import Counter
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.core.errors import PackDisabledError, PackDrainingError, PackUnavailableError, PlatformRemovedError
from app.core.locks import acquire_group_mutation_lock
from app.devices.models import (
    Device,
    DeviceGroup,
    DeviceGroupMembership,
    DeviceOperationalState,
    GroupType,
)
from app.devices.services import write as device_write
from app.devices.services.groups import constraint_name
from app.hosts.models import Host
from app.packs.services import platform_resolver as pack_platform_resolver
from app.portability.schemas import (
    SCHEMA_VERSION,
    UNSUPPORTED_SCHEMA_VERSION_MESSAGE,
    ExportBundle,
    ExportedDevice,
    ExportedDeviceGroup,
    HostSuggestion,
    ImportCommitCreatedRow,
    ImportCommitFailedRow,
    ImportCommitRequest,
    ImportCommitResult,
    ImportCommitSkippedRow,
    ImportMapping,
    ImportPreview,
    ImportPreviewRow,
    ImportRowStatus,
)
from app.portability.services.hash import compute_bundle_hash

if TYPE_CHECKING:
    import uuid
    from collections.abc import Sequence

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.portability.protocols import VerificationEnqueuer

logger = logging.getLogger(__name__)


class BundleHashMismatchError(ValueError):
    """Raised when the supplied bundle_hash does not match the recomputed canonical hash."""


class GroupKeyCollisionError(ValueError):
    """Raised when a bundle group key already exists in the target database."""

    def __init__(self, keys: list[str]) -> None:
        self.keys = keys
        super().__init__(f"device group keys already exist in target: {', '.join(sorted(keys))}")


class UnknownGroupReferenceError(ValueError):
    """Raised when a bundle references a group key not defined in the bundle."""

    def __init__(self, keys: list[str]) -> None:
        self.keys = keys
        super().__init__(f"unknown device group references: {', '.join(sorted(keys))}")


def _identity_key(device: ExportedDevice) -> tuple[str, str, str]:
    return (device.identity_scheme, device.identity_scope, device.identity_value)


async def _load_available_hosts(session: AsyncSession) -> list[Host]:
    result = await session.execute(select(Host).order_by(Host.hostname.asc()))
    return list(result.scalars().all())


def _pick_host_suggestion(device: ExportedDevice, hosts: Sequence[Host]) -> HostSuggestion | None:
    target = device.original_host.hostname.lower()
    matches = [h for h in hosts if h.hostname.lower() == target]
    if not matches:
        return None
    # hosts.hostname is UNIQUE, so at most one match can exist naturally.
    # The tie-break branch below handles the theoretically-impossible duplicate
    # (e.g. injected via raw SQL in a defensive test) but is dead code in prod.
    if len(matches) > 1 and device.original_host.host_id is not None:
        for h in matches:
            if h.id == device.original_host.host_id:
                return HostSuggestion(id=h.id, hostname=h.hostname)
    return HostSuggestion(id=matches[0].id, hostname=matches[0].hostname)


async def _classify_pack_runnable(
    session: AsyncSession,
    device: ExportedDevice,
) -> tuple[ImportRowStatus, list[str]] | None:
    """Return an INVALID classification if the pack/platform is not runnable, else None."""
    try:
        await pack_platform_resolver.assert_runnable(session, pack_id=device.pack_id, platform_id=device.platform_id)
    except PackUnavailableError:
        return (ImportRowStatus.INVALID, [f"pack/platform not installed: {device.pack_id}/{device.platform_id}"])
    except PackDisabledError:
        return (ImportRowStatus.INVALID, [f"pack/platform not installed: {device.pack_id}/{device.platform_id}"])
    except PackDrainingError:
        return (ImportRowStatus.INVALID, [f"pack not runnable: pack {device.pack_id} is draining"])
    except PlatformRemovedError:
        return (ImportRowStatus.INVALID, [f"pack/platform not installed: {device.pack_id}/{device.platform_id}"])
    return None


async def _classify_existing_identity(
    session: AsyncSession,
    device: ExportedDevice,
    suggestion: HostSuggestion | None,
) -> tuple[ImportRowStatus, list[str]] | None:
    """Return a CONFLICT_SKIP classification if the identity already exists, else None."""
    if device.identity_scope == "global":
        existing = await session.execute(
            select(Device.id).where(
                Device.identity_scope == "global",
                Device.identity_scheme == device.identity_scheme,
                Device.identity_value == device.identity_value,
            )
        )
        if existing.first() is not None:
            return (ImportRowStatus.CONFLICT_SKIP, ["identity already exists (global scope)"])
    elif device.identity_scope == "host" and suggestion is not None:
        existing = await session.execute(
            select(Device.id).where(
                Device.identity_scope == "host",
                Device.identity_scheme == device.identity_scheme,
                Device.identity_value == device.identity_value,
                Device.host_id == suggestion.id,
            )
        )
        if existing.first() is not None:
            return (ImportRowStatus.CONFLICT_SKIP, ["identity already exists on suggested host"])
    return None


async def _classify_row(
    session: AsyncSession,
    device: ExportedDevice,
    hosts: Sequence[Host],
    duplicate_keys: set[tuple[str, str, str]],
    static_group_keys: set[str],
) -> tuple[ImportRowStatus, list[str]]:
    unknown_static = sorted(set(device.static_groups) - static_group_keys)
    if unknown_static:
        return (ImportRowStatus.INVALID, [f"unknown static group keys: {', '.join(unknown_static)}"])
    pack_invalid = await _classify_pack_runnable(session, device)
    if pack_invalid is not None:
        return pack_invalid
    if _identity_key(device) in duplicate_keys:
        return (ImportRowStatus.DUPLICATE_IN_BUNDLE, ["identity duplicated within bundle"])
    suggestion = _pick_host_suggestion(device, hosts)
    conflict = await _classify_existing_identity(session, device, suggestion)
    if conflict is not None:
        return conflict
    return (ImportRowStatus.VALID_NEW, [])


def _build_create_payload(device: ExportedDevice, target_host_id: uuid.UUID) -> dict[str, Any]:
    return {
        "pack_id": device.pack_id,
        "platform_id": device.platform_id,
        "identity_scheme": device.identity_scheme,
        "identity_scope": device.identity_scope,
        "identity_value": device.identity_value,
        "connection_target": device.connection_target,
        "name": device.name,
        "os_version": "unknown",
        "host_id": target_host_id,
        "operational_state_last_emitted": DeviceOperationalState.offline,
        "device_type": device.device_type,
        "connection_type": device.connection_type,
        "device_config": dict(device.device_config),
        "test_data": dict(device.test_data),
    }


def _group_filters_payload(group: ExportedDeviceGroup) -> dict[str, Any] | None:
    if group.filters is None:
        return None
    dumped = group.filters.model_dump(mode="json", exclude_none=True)
    if not dumped.get("member_of"):
        dumped.pop("member_of", None)
    return dumped or None


async def _flush_groups_or_collide(session: AsyncSession, keys: list[str]) -> None:
    """Flush staged ``device_groups`` rows, turning a key collision into a typed error.

    Nothing reserves a key that is not yet in the table, so two operators committing
    the same bundle both pass validation and the loser's flush violates
    ``ix_device_groups_key``. The unique index is the real guarantee; this translates
    it into the ``GroupKeyCollisionError`` the route already maps to 409, so the loser
    gets the documented conflict rather than an unhandled 500 on a dead transaction.
    """
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        if constraint_name(exc) != "ix_device_groups_key":
            raise
        # The transaction is gone, so re-read to name the keys that actually landed
        # rather than blaming every key the bundle carried.
        collided = await _load_existing_group_keys(session, set(keys))
        raise GroupKeyCollisionError(sorted(collided or keys)) from exc


async def _load_existing_group_keys(session: AsyncSession, keys: set[str]) -> set[str]:
    if not keys:
        return set()
    result = await session.execute(select(DeviceGroup.key).where(DeviceGroup.key.in_(keys)))
    return {row[0] for row in result.all()}


async def _validate_group_references(session: AsyncSession, bundle: ExportBundle) -> set[str]:
    """Validate bundle group definitions and references.

    Returns the set of static group keys defined in the bundle after verifying:
    - no bundle group key collides with an existing DB group;
    - every dynamic group's ``member_of`` references a static group in the bundle;
    - every device ``static_groups`` key references a static group in the bundle.

    Raises:
        GroupKeyCollisionError: if any bundle group key already exists in the DB.
        UnknownGroupReferenceError: if any ``member_of`` or device ``static_groups``
            reference is not a static group defined in the bundle.
    """
    bundle_keys = {g.key for g in bundle.groups}
    existing = await _load_existing_group_keys(session, bundle_keys)
    if existing:
        raise GroupKeyCollisionError(sorted(existing))

    static_group_keys = {g.key for g in bundle.groups if g.group_type == GroupType.static}
    dynamic_groups = [g for g in bundle.groups if g.group_type == GroupType.dynamic]

    unknown_refs: set[str] = set()
    for group in dynamic_groups:
        if group.filters is None:
            continue
        for key in group.filters.member_of:
            if key not in static_group_keys:
                unknown_refs.add(key)
    for device in bundle.devices:
        for key in device.static_groups:
            if key not in static_group_keys:
                unknown_refs.add(key)
    if unknown_refs:
        raise UnknownGroupReferenceError(sorted(unknown_refs))

    return static_group_keys


class PortabilityImportService:
    """Container-held device-portability import (validate + commit)."""

    def __init__(self, *, verification_enqueuer: VerificationEnqueuer) -> None:
        self._verification_enqueuer = verification_enqueuer

    async def validate_bundle(self, session: AsyncSession, bundle: ExportBundle) -> ImportPreview:
        """Validate a bundle and return a preview with per-row classifications.

        This method is read-only; it issues no writes to the database.

        Raises:
            ValueError: if ``bundle.schema_version`` is not supported.
            GroupKeyCollisionError: if any bundle group key already exists in the target.
            UnknownGroupReferenceError: if any group/device reference is unresolvable.
        """
        # ExportBundle's own gate already rejects a foreign version at parse time; this
        # backstops a bundle whose version was mutated after construction.
        if bundle.schema_version != SCHEMA_VERSION:
            raise ValueError(UNSUPPORTED_SCHEMA_VERSION_MESSAGE)

        static_group_keys = await _validate_group_references(session, bundle)

        hosts = await _load_available_hosts(session)

        counts: Counter[tuple[str, str, str]] = Counter(_identity_key(d) for d in bundle.devices)
        duplicate_keys = {k for k, c in counts.items() if c > 1}

        rows: list[ImportPreviewRow] = []
        for idx, device in enumerate(bundle.devices):
            status, issues = await _classify_row(session, device, hosts, duplicate_keys, static_group_keys)
            suggestion = _pick_host_suggestion(device, hosts)
            rows.append(
                ImportPreviewRow(
                    index=idx,
                    device=device,
                    status=status,
                    host_suggestion=suggestion,
                    issues=issues,
                )
            )

        return ImportPreview(
            schema_version=SCHEMA_VERSION,
            source_instance=bundle.source_instance,
            exported_at=bundle.exported_at,
            bundle_hash=compute_bundle_hash(bundle),
            available_hosts=[HostSuggestion(id=h.id, hostname=h.hostname) for h in hosts],
            rows=rows,
        )

    async def commit_import(self, session: AsyncSession, request: ImportCommitRequest) -> ImportCommitResult:
        """Commit a validated import bundle in dependency order within one transaction:
        static group definitions, devices (with verification enqueue), static memberships
        for created devices, dynamic group definitions.

        Raises:
            BundleHashMismatchError: if ``request.bundle_hash`` does not match the recomputed hash.
            ValueError: if the bundle schema version is unsupported.
            GroupKeyCollisionError: if a bundle group key already exists in the target.
            UnknownGroupReferenceError: if any group/device reference is unresolvable.
        """
        expected_hash = compute_bundle_hash(request.bundle)
        if expected_hash != request.bundle_hash:
            raise BundleHashMismatchError("bundle_hash mismatch")

        # Validate (read-only) before any writes so group references resolve against
        # the pre-import DB state rather than the rows this commit is about to insert.
        preview = await self.validate_bundle(session, request.bundle)

        static_groups = [g for g in request.bundle.groups if g.group_type == GroupType.static]
        dynamic_groups = [g for g in request.bundle.groups if g.group_type == GroupType.dynamic]

        group_id_by_key: dict[str, uuid.UUID] = {}
        if static_groups or dynamic_groups:
            # Both inserts land in one transaction. Splitting them — statics committed
            # here, dynamics only after the device loop — left a window the width of an
            # entire import in which a concurrent delete_group could remove a static
            # group the dynamics were about to reference.
            #
            # The advisory lock is not strictly required today: _validate_group_references
            # guarantees a bundle's member_of names only bundle statics, inserted in this
            # same transaction. It is here so that relaxing that bundle-internal invariant
            # later cannot silently reopen the hole. Do not delete it as redundant.
            #
            # Acquired only when there is group work to do: the lock must scale with
            # group count, not device count, and a groupless bundle has nothing for it
            # to protect.
            await acquire_group_mutation_lock(session)
            group_id_by_key = await self._insert_group_definitions(session, static_groups)
            await self._insert_dynamic_group_definitions(session, dynamic_groups, group_id_by_key)
            # Commit the definitions before the device loop so they survive a
            # failure of any per-row commit below.
            await session.commit()

        by_index = {row.index: row for row in preview.rows}
        mappings_by_index = {m.index: m for m in request.mappings}

        created: list[ImportCommitCreatedRow] = []
        skipped: list[ImportCommitSkippedRow] = []
        failed: list[ImportCommitFailedRow] = []

        device_id_by_index: dict[int, uuid.UUID] = {}
        for idx, row in by_index.items():
            if row.status == ImportRowStatus.DUPLICATE_IN_BUNDLE:
                skipped.append(ImportCommitSkippedRow(index=idx, reason="duplicate in bundle"))
                continue
            if row.status == ImportRowStatus.CONFLICT_SKIP:
                skipped.append(ImportCommitSkippedRow(index=idx, reason="identity already exists"))
                continue
            if row.status == ImportRowStatus.INVALID:
                skipped.append(ImportCommitSkippedRow(index=idx, reason="invalid"))
                continue

            mapping = mappings_by_index.get(idx)
            if mapping is None:
                skipped.append(ImportCommitSkippedRow(index=idx, reason="no mapping"))
                continue

            host = await session.get(Host, mapping.target_host_id)
            if host is None:
                failed.append(ImportCommitFailedRow(index=idx, reason="host not found"))
                continue

            result = await self._insert_row_with_savepoint(session, idx, row, mapping)
            if isinstance(result, ImportCommitCreatedRow):
                created.append(result)
                device_id_by_index[idx] = result.device_id
            else:
                failed.append(result)

        self._insert_static_memberships(
            session,
            static_group_keys={g.key for g in static_groups},
            by_index=by_index,
            device_id_by_index=device_id_by_index,
            group_id_by_key=group_id_by_key,
        )

        # Static memberships are staged, not committed, by the helper above. Commit
        # unconditionally so they land even when no device row committed.
        await session.commit()

        return ImportCommitResult(created=created, skipped=skipped, failed=failed)

    async def _insert_group_definitions(
        self,
        session: AsyncSession,
        static_groups: list[ExportedDeviceGroup],
    ) -> dict[str, uuid.UUID]:
        groups = [
            DeviceGroup(
                key=group_def.key,
                name=group_def.name,
                description=group_def.description,
                group_type=GroupType.static,
                filters=None,
            )
            for group_def in static_groups
        ]
        session.add_all(groups)
        await _flush_groups_or_collide(session, [g.key for g in groups])
        return {group.key: group.id for group in groups}

    def _insert_static_memberships(
        self,
        session: AsyncSession,
        static_group_keys: set[str],
        by_index: dict[int, ImportPreviewRow],
        device_id_by_index: dict[int, uuid.UUID],
        group_id_by_key: dict[str, uuid.UUID],
    ) -> None:
        if not static_group_keys:
            return
        for idx, device_id in device_id_by_index.items():
            row = by_index[idx]
            for key in row.device.static_groups:
                if key not in static_group_keys:
                    continue
                group_id = group_id_by_key.get(key)
                if group_id is None:
                    continue
                session.add(DeviceGroupMembership(group_id=group_id, device_id=device_id))

    async def _insert_dynamic_group_definitions(
        self,
        session: AsyncSession,
        dynamic_groups: list[ExportedDeviceGroup],
        group_id_by_key: dict[str, uuid.UUID],
    ) -> None:
        if not dynamic_groups:
            return
        groups = [
            DeviceGroup(
                key=group_def.key,
                name=group_def.name,
                description=group_def.description,
                group_type=GroupType.dynamic,
                filters=_group_filters_payload(group_def),
            )
            for group_def in dynamic_groups
        ]
        session.add_all(groups)
        await _flush_groups_or_collide(session, [g.key for g in groups])
        for group in groups:
            group_id_by_key[group.key] = group.id

    async def _insert_row_with_savepoint(
        self,
        session: AsyncSession,
        idx: int,
        row: ImportPreviewRow,
        mapping: ImportMapping,
    ) -> ImportCommitCreatedRow | ImportCommitFailedRow:
        savepoint = await session.begin_nested()
        savepoint_released = False
        try:
            payload = _build_create_payload(row.device, mapping.target_host_id)
            device = device_write.stage_device_record(session, payload)
            await session.flush()
            await self._verification_enqueuer.enqueue_for_device(session, device)
            await savepoint.commit()
            savepoint_released = True
            await session.commit()
            return ImportCommitCreatedRow(index=idx, device_id=device.id)
        except IntegrityError as exc:
            if not savepoint_released:
                await savepoint.rollback()
            return ImportCommitFailedRow(index=idx, reason=f"identity conflict: {exc.orig}")
        except Exception as exc:  # noqa: BLE001 — per-row import: any staging/enqueue failure becomes a failed row, never aborts the bundle
            if not savepoint_released:
                await savepoint.rollback()
            reason = str(exc) or exc.__class__.__name__
            lower = reason.lower()
            if "verification" in lower or "create_job" in lower:
                return ImportCommitFailedRow(index=idx, reason=f"verification enqueue failed: {reason}")
            return ImportCommitFailedRow(index=idx, reason=reason)
