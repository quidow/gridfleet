"""Validate and commit device portability bundles.

Validate is read-only: parse the bundle, classify each row, suggest a host per
row. Commit re-parses from the original bundle and owns its own sessions via
an injected ``session_factory``: one short read for the preview, one
transaction for group definitions, one transaction per bounded device batch
(each row contained in its own savepoint), and one transaction per bounded
membership batch.
"""

from __future__ import annotations

import logging
from collections import Counter
from itertools import batched
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError

from app.core.errors import PackDisabledError, PackDrainingError, PackUnavailableError, PlatformRemovedError
from app.devices.models import (
    Device,
    DeviceGroup,
    DeviceGroupMemberOf,
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
    MembershipSkip,
)
from app.portability.services.hash import compute_bundle_hash

if TYPE_CHECKING:
    import uuid
    from collections.abc import Mapping, Sequence

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.portability.protocols import VerificationEnqueuer

logger = logging.getLogger(__name__)
MEMBERSHIP_BATCH_SIZE = 1000  # ponytail: bounds one batch's row-lock hold; tune only from measured import contention
DEVICE_IMPORT_BATCH_SIZE = 100  # ponytail: bounds one batch's crash-durability unit; the savepoint is per-row, not this


class BundleHashMismatchError(ValueError):
    """Raised when the supplied bundle_hash does not match the recomputed canonical hash."""


class GroupKeyCollisionError(ValueError):
    """Raised when a bundle group key already exists in the target database.

    ``keys`` may be empty: the flush path re-reads to name the colliding keys
    after its own rollback, so a peer can delete the row that won between the
    collision and the re-read. Report the conflict without naming keys rather
    than guessing at them — the operator's next step (re-validate and retry) is
    the same either way.
    """

    def __init__(self, keys: list[str]) -> None:
        self.keys = keys
        detail = f": {', '.join(sorted(keys))}" if keys else " (the colliding key was removed before it was read back)"
        super().__init__(f"device group keys already exist in target{detail}")


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
    """The stored JSON column's value: native axes only, never ``member_of``.

    References live in ``device_group_member_of`` from this phase on, so a
    ``member_of``-only dynamic group persists ``filters IS NULL``.
    """
    if group.filters is None:
        return None
    dumped = group.filters.model_dump(mode="json", exclude_none=True)
    dumped.pop("member_of", None)
    return dumped or None


async def _load_existing_group_keys(session: AsyncSession, keys: set[str]) -> set[str]:
    if not keys:
        return set()
    result = await session.execute(select(DeviceGroup.key).where(DeviceGroup.key.in_(keys)))
    return {row[0] for row in result.all()}


async def _load_existing_group_ids(session: AsyncSession, keys: set[str]) -> dict[str, uuid.UUID]:
    """The named ``device_groups`` rows keyed by group key, with their current ids.

    Used by ``_stage_static_memberships`` to detect a delete+recreate: a static
    group deleted and recreated during the device loop keeps its key but gets a
    new row id, so a key-only re-check misses it and the cached id goes stale.
    Nothing excludes a peer definition writer from this window, so the id is the
    only thing that answers "is this still the group the bundle meant?".
    """
    if not keys:
        return {}
    result = await session.execute(select(DeviceGroup.key, DeviceGroup.id).where(DeviceGroup.key.in_(keys)))
    return {row[0]: row[1] for row in result.all()}


async def _insert_member_of_references(
    session: AsyncSession,
    dynamic_groups: Sequence[ExportedDeviceGroup],
    dynamic_ids: Mapping[str, uuid.UUID],
    static_ids: Mapping[str, uuid.UUID],
) -> None:
    """Persist each dynamic group's ``member_of`` as ``device_group_member_of`` rows.

    ``_validate_group_references`` already requires every ``member_of`` key to
    name a static group defined in the same bundle, so ``static_ids`` (the map
    ``_insert_group_definitions`` returns) resolves every target. It remains the
    user-facing validation gate; the FK is the final race authority.
    """
    values = [
        {"dynamic_group_id": dynamic_ids[group.key], "static_group_id": static_ids[target]}
        for group in dynamic_groups
        for target in sorted(set(group.filters.member_of if group.filters else []))
    ]
    if values:
        await session.execute(pg_insert(DeviceGroupMemberOf).values(values))


async def _lock_existing_device_ids(session: AsyncSession, device_ids: set[uuid.UUID]) -> set[uuid.UUID]:
    """Return present device ids and prevent their deletion through the batch insert."""
    if not device_ids:
        return set()
    result = await session.execute(
        select(Device.id)
        .where(Device.id.in_(device_ids))
        .order_by(Device.id)
        .with_for_update(read=True, key_share=True)
    )
    return set(result.scalars().all())


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

    def __init__(
        self,
        *,
        verification_enqueuer: VerificationEnqueuer,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self._verification_enqueuer = verification_enqueuer
        self._session_factory = session_factory

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

    async def commit_import(self, request: ImportCommitRequest) -> ImportCommitResult:
        """Commit definitions, then per-batch devices, then per-batch memberships.

        Owns every session it uses via the injected ``session_factory``: a
        short read for validation, one transaction for definitions, one
        transaction per bounded device batch (each row in its own savepoint),
        and one transaction per bounded membership batch. Nothing is held
        across batches, which is why membership staging re-checks every group
        id it cached rather than trusting the definition pass.

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
        async with self._session_factory() as read_db:
            preview = await self.validate_bundle(read_db, request.bundle)

        group_id_by_key = await self._commit_group_definitions(request)

        by_index = {row.index: row for row in preview.rows}
        mappings_by_index = {m.index: m for m in request.mappings}
        rows_to_insert, skipped = self._plan_device_rows(by_index, mappings_by_index)

        created, failed, device_id_by_index = await self._insert_device_batches(rows_to_insert)

        memberships_skipped = await self._stage_static_memberships(
            by_index=by_index,
            device_id_by_index=device_id_by_index,
            group_id_by_key=group_id_by_key,
        )

        return ImportCommitResult(
            created=created,
            skipped=skipped,
            failed=failed,
            memberships_skipped=memberships_skipped,
        )

    async def _commit_group_definitions(self, request: ImportCommitRequest) -> dict[str, uuid.UUID]:
        """Commit the bundle's group definitions and ``member_of`` edges in one transaction.

        Raises:
            GroupKeyCollisionError: if a bundle group key already exists in the target.
        """
        static_groups = [g for g in request.bundle.groups if g.group_type == GroupType.static]
        dynamic_groups = [g for g in request.bundle.groups if g.group_type == GroupType.dynamic]
        if not static_groups and not dynamic_groups:
            return {}

        session_factory = self._session_factory
        try:
            # Static definitions, dynamic definitions, and the edges joining
            # them commit atomically. Publishing a static group before its
            # referring edge would let a concurrent delete_group see an
            # unreferenced target and remove it, and the edge that follows
            # would have nothing to point at.
            async with session_factory.begin() as definition_db:
                return await self._insert_group_definitions_and_edges(definition_db, static_groups, dynamic_groups)
        except IntegrityError as exc:
            if constraint_name(exc) != "ix_device_groups_key":
                raise
            # The transaction is gone, so re-read through a fresh session to
            # name the keys that actually landed rather than blaming every
            # key the bundle carried.
            bundle_group_keys = {g.key for g in request.bundle.groups}
            async with session_factory() as conflict_db:
                collided = await _load_existing_group_keys(conflict_db, bundle_group_keys)
            raise GroupKeyCollisionError(sorted(collided)) from exc

    def _plan_device_rows(
        self,
        by_index: dict[int, ImportPreviewRow],
        mappings_by_index: dict[int, ImportMapping],
    ) -> tuple[list[tuple[int, ImportPreviewRow, ImportMapping]], list[ImportCommitSkippedRow]]:
        """Classify each preview row as skipped or queued for insertion. Issues no writes."""
        skipped: list[ImportCommitSkippedRow] = []
        rows_to_insert: list[tuple[int, ImportPreviewRow, ImportMapping]] = []
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

            rows_to_insert.append((idx, row, mapping))
        return rows_to_insert, skipped

    async def _insert_device_batches(
        self,
        rows_to_insert: list[tuple[int, ImportPreviewRow, ImportMapping]],
    ) -> tuple[list[ImportCommitCreatedRow], list[ImportCommitFailedRow], dict[int, uuid.UUID]]:
        """Insert queued rows in bounded batches, each row contained in its own savepoint.

        A completed batch is the crash-durability unit; the savepoint inside
        ``_insert_row_with_savepoint`` is the public per-row failure-isolation
        unit. A failure that escapes the savepoint's own containment rolls back
        this batch only — earlier completed batches stay durable.
        """
        created: list[ImportCommitCreatedRow] = []
        failed: list[ImportCommitFailedRow] = []
        device_id_by_index: dict[int, uuid.UUID] = {}
        for batch in batched(rows_to_insert, DEVICE_IMPORT_BATCH_SIZE, strict=False):
            async with self._session_factory.begin() as batch_db:
                for idx, row, mapping in batch:
                    host = await batch_db.get(Host, mapping.target_host_id)
                    if host is None:
                        failed.append(ImportCommitFailedRow(index=idx, reason="host not found"))
                        continue

                    result = await self._insert_row_with_savepoint(batch_db, idx, row, mapping)
                    if isinstance(result, ImportCommitCreatedRow):
                        created.append(result)
                        device_id_by_index[idx] = result.device_id
                    else:
                        failed.append(result)
        return created, failed, device_id_by_index

    async def _stage_static_memberships(
        self,
        *,
        by_index: dict[int, ImportPreviewRow],
        device_id_by_index: dict[int, uuid.UUID],
        group_id_by_key: dict[str, uuid.UUID],
    ) -> list[MembershipSkip]:
        """Commit memberships in bounded, referentially stable batches."""
        planned = [
            (idx, key, device_id, group_id)
            for idx, device_id in device_id_by_index.items()
            for key in by_index[idx].device.static_groups
            if (group_id := group_id_by_key.get(key)) is not None
        ]
        if not planned:
            return []

        memberships_skipped: list[MembershipSkip] = []
        for batch in batched(planned, MEMBERSHIP_BATCH_SIZE, strict=False):
            async with self._session_factory.begin() as db:
                current_group_ids = await _load_existing_group_ids(db, {key for _, key, _, _ in batch})
                existing_device_ids = await _lock_existing_device_ids(db, {device_id for _, _, device_id, _ in batch})
                values: list[dict[str, uuid.UUID]] = []
                for idx, key, device_id, cached_group_id in batch:
                    current_group_id = current_group_ids.get(key)
                    if current_group_id != cached_group_id:
                        reason = (
                            f"static group '{key}' deleted during import"
                            if current_group_id is None
                            else f"static group '{key}' deleted and recreated during import"
                        )
                        memberships_skipped.append(MembershipSkip(index=idx, group_key=key, reason=reason))
                    elif device_id not in existing_device_ids:
                        memberships_skipped.append(
                            MembershipSkip(index=idx, group_key=key, reason="device was deleted during import")
                        )
                    else:
                        values.append({"group_id": cached_group_id, "device_id": device_id})
                if values:
                    await db.execute(
                        pg_insert(DeviceGroupMembership)
                        .values(values)
                        .on_conflict_do_nothing(
                            index_elements=[DeviceGroupMembership.group_id, DeviceGroupMembership.device_id]
                        )
                    )
        return memberships_skipped

    async def _insert_group_definitions_and_edges(
        self,
        session: AsyncSession,
        static_groups: list[ExportedDeviceGroup],
        dynamic_groups: list[ExportedDeviceGroup],
    ) -> dict[str, uuid.UUID]:
        """Stage static/dynamic group definitions and their ``member_of`` edges.

        All three inserts share the caller's transaction. A key-collision
        ``IntegrityError`` from either flush propagates to ``commit_import``,
        which owns collision translation after the transaction has rolled back.
        """
        group_id_by_key = await self._insert_group_definitions(session, static_groups)
        dynamic_id_by_key = await self._insert_dynamic_group_definitions(session, dynamic_groups)
        await _insert_member_of_references(session, dynamic_groups, dynamic_id_by_key, group_id_by_key)
        return group_id_by_key

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
        await session.flush()
        return {group.key: group.id for group in groups}

    async def _insert_dynamic_group_definitions(
        self,
        session: AsyncSession,
        dynamic_groups: list[ExportedDeviceGroup],
    ) -> dict[str, uuid.UUID]:
        """Insert the bundle's dynamic group definitions.

        Returns the inserted ids keyed by group key. This map feeds relation
        staging (``_insert_member_of_references``) only — dynamic groups have
        no membership rows, so folding it into the static ``group_id_by_key``
        map would let membership staging resolve a key it must never resolve.
        """
        if not dynamic_groups:
            return {}
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
        await session.flush()
        return {group.key: group.id for group in groups}

    async def _insert_row_with_savepoint(
        self,
        db: AsyncSession,
        idx: int,
        row: ImportPreviewRow,
        mapping: ImportMapping,
    ) -> ImportCommitCreatedRow | ImportCommitFailedRow:
        """Stage and flush one device row inside its own savepoint.

        This module's only ``begin_nested()``: a row failure rolls back only
        this row, never the batch it shares a transaction with, so per-row
        partial success stays a public contract even when rows commit in
        bounded batches. Translation happens only after the nested context has
        exited (committed or rolled back) and never calls a savepoint method
        directly.
        """
        try:
            async with db.begin_nested():
                payload = _build_create_payload(row.device, mapping.target_host_id)
                device = device_write.stage_device_record(db, payload)
                await db.flush()
                await self._verification_enqueuer.enqueue_for_device(db, device)
                created = ImportCommitCreatedRow(index=idx, device_id=device.id)
        except IntegrityError as exc:
            return ImportCommitFailedRow(index=idx, reason=f"identity conflict: {exc.orig}")
        except Exception as exc:  # noqa: BLE001 -- public per-row partial-success contract
            reason = str(exc) or exc.__class__.__name__
            if "verification" in reason.lower() or "create_job" in reason.lower():
                reason = f"verification enqueue failed: {reason}"
            return ImportCommitFailedRow(index=idx, reason=reason)
        return created
