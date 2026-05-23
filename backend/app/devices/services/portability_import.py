"""Validate and commit device portability bundles.

Validate is read-only: parse the bundle, classify each row, suggest a host per
row. Commit (T8) re-parses from the original bundle and inserts rows in
per-row transactions with verification enqueue.
"""

from __future__ import annotations

import logging
import uuid
from collections import Counter
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from app.core.errors import PackDisabledError, PackDrainingError, PackUnavailableError, PlatformRemovedError
from app.devices.models import Device, DeviceOperationalState
from app.devices.schemas.device import DeviceVerificationCreate
from app.devices.schemas.portability import (
    SCHEMA_VERSION,
    ExportBundle,
    ExportedDevice,
    HostSuggestion,
    ImportCommitCreatedRow,
    ImportCommitFailedRow,
    ImportCommitRequest,
    ImportCommitResult,
    ImportCommitSkippedRow,
    ImportPreview,
    ImportPreviewRow,
    ImportRowStatus,
)
from app.devices.services import verification_job_state
from app.devices.services import write as device_write
from app.devices.services.portability_hash import compute_bundle_hash
from app.hosts.models import Host
from app.jobs import JOB_KIND_DEVICE_VERIFICATION
from app.jobs import queue as job_queue
from app.packs.services import platform_resolver as pack_platform_resolver

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.ext.asyncio import AsyncSession

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


async def _classify_row(
    session: AsyncSession,
    device: ExportedDevice,
    hosts: Sequence[Host],
    duplicate_keys: set[tuple[str, str, str]],
) -> tuple[ImportRowStatus, list[str]]:
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
    if _identity_key(device) in duplicate_keys:
        return (ImportRowStatus.DUPLICATE_IN_BUNDLE, ["identity duplicated within bundle"])
    suggestion = _pick_host_suggestion(device, hosts)
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
    return (ImportRowStatus.VALID_NEW, [])


async def validate_bundle(session: AsyncSession, bundle: ExportBundle) -> ImportPreview:
    """Validate a bundle and return a preview with per-row classifications.

    This function is read-only; it issues no writes to the database.

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
        "auto_manage": device.auto_manage,
        "tags": dict(device.tags),
        "device_config": dict(device.device_config),
        "test_data": dict(device.test_data),
    }


async def _enqueue_verification(session: AsyncSession, device: Device) -> None:
    job_id = uuid.uuid4()
    snapshot = verification_job_state.new_job(str(job_id))
    data = DeviceVerificationCreate(
        pack_id=device.pack_id,
        platform_id=device.platform_id,
        identity_scheme=device.identity_scheme,
        identity_scope=device.identity_scope,
        identity_value=device.identity_value,
        connection_target=device.connection_target,
        name=device.name,
        host_id=device.host_id,
        device_type=device.device_type,
        connection_type=device.connection_type,
        auto_manage=device.auto_manage,
        tags=device.tags or None,
    )
    payload: dict[str, Any] = {"mode": "create", "data": data.model_dump(mode="json")}
    await job_queue.create_job(
        session,
        kind=JOB_KIND_DEVICE_VERIFICATION,
        payload=payload,
        snapshot=snapshot,
        max_attempts=1,
        job_id=job_id,
        commit=False,
    )


async def commit_import(session: AsyncSession, request: ImportCommitRequest) -> ImportCommitResult:
    """Commit a validated import bundle: insert devices row-by-row with per-row savepoints.

    Each row is committed atomically together with its verification job enqueue.
    If the verification enqueue fails, the savepoint rollback unwinds the device insert.

    Raises:
        BundleHashMismatchError: if ``request.bundle_hash`` does not match the recomputed hash.
    """
    expected_hash = compute_bundle_hash(request.bundle)
    if expected_hash != request.bundle_hash:
        raise BundleHashMismatchError("bundle_hash mismatch")

    preview = await validate_bundle(session, request.bundle)
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

        savepoint = await session.begin_nested()
        try:
            payload = _build_create_payload(row.device, mapping.target_host_id)
            device = device_write.stage_device_record(session, payload)
            await session.flush()
            await _enqueue_verification(session, device)
            await savepoint.commit()
            await session.commit()
            created.append(ImportCommitCreatedRow(index=idx, device_id=device.id))
        except Exception as exc:  # noqa: BLE001
            await savepoint.rollback()
            reason = str(exc) or exc.__class__.__name__
            lower = reason.lower()
            if "duplicate key" in lower or "unique" in lower:
                failed.append(ImportCommitFailedRow(index=idx, reason=f"identity conflict: {reason}"))
            elif "verification" in lower or "create_job" in lower:
                failed.append(ImportCommitFailedRow(index=idx, reason=f"verification enqueue failed: {reason}"))
            else:
                failed.append(ImportCommitFailedRow(index=idx, reason=reason))

    return ImportCommitResult(created=created, skipped=skipped, failed=failed)
