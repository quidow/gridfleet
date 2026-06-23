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
from app.devices.models import Device, DeviceOperationalState
from app.devices.services import write as device_write
from app.hosts.models import Host
from app.packs.services import platform_resolver as pack_platform_resolver
from app.portability.schemas import (
    SCHEMA_VERSION,
    ExportBundle,
    ExportedDevice,
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
) -> tuple[ImportRowStatus, list[str]]:
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
        "operational_state": DeviceOperationalState.offline,
        "device_type": device.device_type,
        "connection_type": device.connection_type,
        "tags": dict(device.tags),
        "device_config": dict(device.device_config),
        "test_data": dict(device.test_data),
    }


class PortabilityImportService:
    """Container-held device-portability import (validate + commit)."""

    def __init__(self, *, verification_enqueuer: VerificationEnqueuer) -> None:
        self._verification_enqueuer = verification_enqueuer

    async def validate_bundle(self, session: AsyncSession, bundle: ExportBundle) -> ImportPreview:
        """Validate a bundle and return a preview with per-row classifications.

        This method is read-only; it issues no writes to the database.

        Raises:
            ValueError: if ``bundle.schema_version`` is not supported.
        """
        if bundle.schema_version != SCHEMA_VERSION:
            raise ValueError(f"unsupported schema_version: {bundle.schema_version}")

        hosts = await _load_available_hosts(session)

        counts: Counter[tuple[str, str, str]] = Counter(_identity_key(d) for d in bundle.devices)
        duplicate_keys = {k for k, c in counts.items() if c > 1}

        rows: list[ImportPreviewRow] = []
        for idx, device in enumerate(bundle.devices):
            status, issues = await _classify_row(session, device, hosts, duplicate_keys)
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
        """Commit a validated import bundle: insert devices row-by-row with per-row savepoints.

        Each row is committed atomically together with its verification job enqueue.
        If the verification enqueue fails, the savepoint rollback unwinds the device insert.

        Raises:
            BundleHashMismatchError: if ``request.bundle_hash`` does not match the recomputed hash.
        """
        expected_hash = compute_bundle_hash(request.bundle)
        if expected_hash != request.bundle_hash:
            raise BundleHashMismatchError("bundle_hash mismatch")

        preview = await self.validate_bundle(session, request.bundle)
        by_index = {row.index: row for row in preview.rows}
        mappings_by_index = {m.index: m for m in request.mappings}

        created: list[ImportCommitCreatedRow] = []
        skipped: list[ImportCommitSkippedRow] = []
        failed: list[ImportCommitFailedRow] = []

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
            else:
                failed.append(result)

        return ImportCommitResult(created=created, skipped=skipped, failed=failed)

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
