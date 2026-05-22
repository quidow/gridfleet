"""Validate and commit device portability bundles.

Validate is read-only: parse the bundle, classify each row, suggest a host per
row. Commit (T8) re-parses from the original bundle and inserts rows in
per-row transactions with verification enqueue.
"""

from __future__ import annotations

import logging
from collections import Counter
from typing import TYPE_CHECKING

from sqlalchemy import select

from app.devices.models import Device
from app.devices.schemas.portability import (
    SCHEMA_VERSION,
    ExportBundle,
    ExportedDevice,
    HostSuggestion,
    ImportPreview,
    ImportPreviewRow,
    ImportRowStatus,
)
from app.devices.services.portability_hash import compute_bundle_hash
from app.hosts.models import Host

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


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
