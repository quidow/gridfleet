"""Router for device diagnostic bundle export and snapshot history."""

import logging
import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import DateTime, cast, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import DbDep
from app.core.http_errors import found_or_404
from app.core.leader.models import ControlPlaneStateEntry
from app.devices.dependencies import DeviceServicesDep
from app.devices.routers.helpers import get_device_or_404
from app.diagnostics.dependencies import DiagnosticsServicesDep
from app.diagnostics.schemas import (
    DiagnosticExportResponse,
    DiagnosticSnapshotDetail,
    DiagnosticSnapshotListResponse,
    DiagnosticSnapshotSummary,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/diagnostics", tags=["diagnostics"])

_RATE_LIMIT_WINDOW = timedelta(seconds=5)
_RATE_LIMIT_NAMESPACE = "diagnostics_export_throttle"


async def _enforce_rate_limit(db: AsyncSession, device_id: uuid.UUID) -> None:
    """Atomic per-device rate limit using a single conditional upsert.

    Replaces an earlier get-then-set pattern that had a TOCTOU window —
    two concurrent operator clicks on the same device could both pass the
    freshness check before either wrote the timestamp. The upsert below
    is one statement: it inserts when no row exists, and updates only
    when the existing ``captured_at`` is older than the rate-limit
    window. When the row exists and is still fresh, the WHERE clause
    rejects the update, the RETURNING clause yields no row, and the
    request is throttled.
    """
    key = str(device_id)
    now = datetime.now(UTC)
    cutoff = now - _RATE_LIMIT_WINDOW
    value = {"captured_at": now.isoformat()}
    insert_stmt = pg_insert(ControlPlaneStateEntry).values(
        namespace=_RATE_LIMIT_NAMESPACE,
        key=key,
        value=value,
    )
    stmt = insert_stmt.on_conflict_do_update(
        constraint="uq_control_plane_state_entries_namespace_key",
        set_={"value": insert_stmt.excluded.value},
        where=(cast(ControlPlaneStateEntry.value["captured_at"].astext, DateTime(timezone=True)) < cutoff),
    ).returning(ControlPlaneStateEntry.value)
    result = await db.execute(stmt)
    if result.scalar_one_or_none() is not None:
        return
    # Conflict and WHERE rejected the update — still inside the cooldown
    # window. Compute Retry-After from the row we did not touch.
    stored = (
        await db.execute(
            select(ControlPlaneStateEntry.value).where(
                ControlPlaneStateEntry.namespace == _RATE_LIMIT_NAMESPACE,
                ControlPlaneStateEntry.key == key,
            )
        )
    ).scalar_one_or_none()
    remaining = 1
    if isinstance(stored, dict):
        captured_at_raw = stored.get("captured_at")
        if isinstance(captured_at_raw, str):
            try:
                captured_at = datetime.fromisoformat(captured_at_raw)
            except ValueError:
                captured_at = None
            if captured_at is not None:
                if captured_at.tzinfo is None:
                    captured_at = captured_at.replace(tzinfo=UTC)
                elapsed = now - captured_at
                if elapsed < _RATE_LIMIT_WINDOW:
                    remaining = max(1, int((_RATE_LIMIT_WINDOW - elapsed).total_seconds()) + 1)
    raise HTTPException(
        status_code=429,
        detail="Diagnostic export rate-limited; retry after cooldown",
        headers={"Retry-After": str(remaining)},
    )


@router.post(
    "/devices/{device_id}/export",
    response_model=DiagnosticExportResponse,
)
async def export_device_diagnostics(
    device_id: uuid.UUID,
    db: DbDep,
    device_services: DeviceServicesDep,
    diagnostics_services: DiagnosticsServicesDep,
    persist: bool = Query(default=True),
    redact: bool = Query(default=False),
) -> DiagnosticExportResponse:
    device = await get_device_or_404(device_id, db, device_services.crud)
    await _enforce_rate_limit(db, device_id)
    warnings: list[str] = []
    payload = await diagnostics_services.export.assemble_bundle(db, device, redact=redact)
    snapshot_id: uuid.UUID | None = None
    if persist:
        try:
            snapshot_id = await diagnostics_services.export.capture_snapshot(
                db, device, trigger="operator", reason=None
            )
        except Exception as exc:  # noqa: BLE001 - operator should still receive the assembled payload.
            warnings.append(f"snapshot persistence failed: {exc.__class__.__name__}")
            logger.warning(
                "Diagnostic snapshot persistence failed for device %s",
                device.id,
                exc_info=True,
            )
    await db.commit()
    return DiagnosticExportResponse(payload=payload, snapshot_id=snapshot_id, warnings=warnings)


@router.get(
    "/devices/{device_id}/snapshots",
    response_model=DiagnosticSnapshotListResponse,
)
async def list_device_diagnostic_snapshots(
    device_id: uuid.UUID,
    db: DbDep,
    device_services: DeviceServicesDep,
    diagnostics_services: DiagnosticsServicesDep,
    limit: int = Query(default=20, ge=1, le=100),
    before: uuid.UUID | None = Query(default=None),
) -> DiagnosticSnapshotListResponse:
    await get_device_or_404(device_id, db, device_services.crud)
    try:
        rows, next_before = await diagnostics_services.export.list_snapshots(db, device_id, limit=limit, before=before)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Unknown before cursor") from exc
    return DiagnosticSnapshotListResponse(
        items=[DiagnosticSnapshotSummary.model_validate(row) for row in rows],
        next_before=next_before,
    )


@router.get(
    "/devices/{device_id}/snapshots/{snapshot_id}",
    response_model=DiagnosticSnapshotDetail,
)
async def get_device_diagnostic_snapshot(
    device_id: uuid.UUID,
    snapshot_id: uuid.UUID,
    db: DbDep,
    device_services: DeviceServicesDep,
    diagnostics_services: DiagnosticsServicesDep,
    redact: bool = Query(default=False),
) -> DiagnosticSnapshotDetail:
    await get_device_or_404(device_id, db, device_services.crud)
    row = found_or_404(await diagnostics_services.export.get_snapshot(db, device_id, snapshot_id), "Snapshot not found")
    payload = row.payload
    if redact:
        payload = await diagnostics_services.export.redact_bundle(db, payload)
    return DiagnosticSnapshotDetail(
        id=row.id,
        captured_at=row.captured_at,
        trigger=row.trigger,
        reason=row.reason,
        payload=payload,
    )
