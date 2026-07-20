"""Streaming read-only inventory snapshot of registered devices.

JSON or CSV output. Column selection via the InventoryColumn allowlist.
Stream via async generator over chunked partitions so large fleets do not
buffer entirely in memory.
"""

from __future__ import annotations

import csv
import io
import json
import uuid
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import Select, select
from sqlalchemy.orm import selectinload

from app.core.timeutil import now_utc
from app.devices.models import Device
from app.devices.services.group_membership import load_group_membership_index, load_groups_by_keys
from app.devices.services.service import UnknownGroupKeysError, _apply_device_filters
from app.devices.services.state import operational_state_sql

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.core.protocols import SettingsReader
    from app.devices.schemas.filters import DeviceQueryFilters
    from app.devices.services.service import DeviceListStatement
    from app.portability.schemas import InventoryColumn

_CHUNK = 200

_CSV_INJECTION_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _csv_safe(value: str) -> str:
    """Prefix a leading character that spreadsheets treat as a formula trigger with a single quote."""
    if value and value[0] in _CSV_INJECTION_PREFIXES:
        return "'" + value
    return value


def _prefixed_attr_value(device: Device, v: str) -> object:
    """Resolve dotted verification.* columns via their model-attribute mapping."""
    attr = {
        "verification.verified_at": "verified_at",
        "verification.session_viability_status": "session_viability_status",
        "verification.device_checks_healthy": "device_checks_healthy",
        "verification.device_checks_checked_at": "device_checks_checked_at",
    }[v]
    return getattr(device, attr)


def _host_column_value(device: Device, v: str) -> object:
    """Resolve host.* columns, tolerating a missing host relationship."""
    if v == "host.id":
        return str(device.host.id) if device.host else None
    return device.host.hostname if device.host else None


def _column_value(device: Device, column: InventoryColumn, operational_state: str) -> object:  # noqa: PLR0911 - flat column dispatch, one return per column family
    v = column.value
    if v == "operational_state":
        # Read-time projection (WS-7.2): the value is computed by the SQL twin in
        # the streaming query, not read off a stored column.
        return operational_state
    if v in ("host.id", "host.hostname"):
        return _host_column_value(device, v)
    if v == "identity.scheme":
        return device.identity_scheme
    if v == "identity.scope":
        return device.identity_scope
    if v == "identity.value":
        return device.identity_value
    if v.startswith("verification."):
        return _prefixed_attr_value(device, v)
    return getattr(device, v)


def _nested_set(out: dict[str, Any], dotted: str, value: object) -> None:
    parts = dotted.split(".")
    cursor = out
    for part in parts[:-1]:
        cursor = cursor.setdefault(part, {})
    cursor[parts[-1]] = value


def _normalize_scalar(raw: object) -> object:
    if raw is None:
        return None
    if isinstance(raw, uuid.UUID):
        return str(raw)
    if hasattr(raw, "isoformat"):
        return raw.isoformat()
    if hasattr(raw, "value"):
        return raw.value
    return raw


def _row_to_json_dict(device: Device, columns: list[InventoryColumn], operational_state: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for col in columns:
        raw = _column_value(device, col, operational_state)
        if isinstance(raw, (dict, list)):
            _nested_set(out, col.value, raw)
        else:
            _nested_set(out, col.value, _normalize_scalar(raw))
    return out


def _row_to_csv_values(device: Device, columns: list[InventoryColumn], operational_state: str) -> list[str]:
    out: list[str] = []
    for col in columns:
        raw = _column_value(device, col, operational_state)
        if raw is None:
            out.append("")
            continue
        if isinstance(raw, (dict, list)):
            out.append(_csv_safe(json.dumps(raw, sort_keys=True, ensure_ascii=False)))
            continue
        normalized = _normalize_scalar(raw)
        if normalized is None:
            out.append("")
            continue
        out.append(_csv_safe(str(normalized)))
    return out


def _base_query(filters: DeviceQueryFilters | None) -> Select[tuple[Device, str]]:
    stmt: Select[tuple[Device]] = select(Device).options(selectinload(Device.host)).order_by(Device.created_at.asc())
    if filters is not None:
        stmt = cast("DeviceListStatement", _apply_device_filters(stmt, filters))
    # operational_state is a read-time projection (WS-7.2): compute it in-SQL via
    # the twin so each streamed row carries its derived state without a stored column.
    return stmt.add_columns(operational_state_sql(now=now_utc()).label("operational_state"))


class InventoryExportService:
    """Container-held streaming inventory export."""

    def __init__(self, *, settings: SettingsReader) -> None:
        self._settings = settings

    async def _resolve_group_filter(
        self, session: AsyncSession, filters: DeviceQueryFilters | None
    ) -> set[uuid.UUID] | None:
        """Validate group keys once and return the matching device-id set, or
        ``None`` when no group filter is applied (caller streams without membership
        gating). Raises :class:`UnknownGroupKeysError` for missing keys.
        """
        if filters is None or not filters.groups:
            return None
        keys = list(filters.groups)
        groups = await load_groups_by_keys(session, keys)
        loaded_keys = {group.key for group in groups}
        missing = [key for key in keys if key not in loaded_keys]
        if missing:
            raise UnknownGroupKeysError(missing)
        candidate_stmt = cast("DeviceListStatement", _apply_device_filters(select(Device), filters)).options(
            selectinload(Device.host)
        )
        candidates = list((await session.execute(candidate_stmt)).scalars().all())
        index = await load_group_membership_index(session, groups=groups, devices=candidates, settings=self._settings)
        return {device.id for device in candidates if index.matches_all(device.id, keys)}

    async def iter_inventory_json(
        self,
        session: AsyncSession,
        *,
        columns: list[InventoryColumn],
        filters: DeviceQueryFilters | None,
    ) -> AsyncIterator[str]:
        matching_ids = await self._resolve_group_filter(session, filters)
        stmt = _base_query(filters).execution_options(yield_per=_CHUNK)
        result = await session.stream(stmt)
        yield "["
        first = True
        async for partition in result.partitions(_CHUNK):
            for device, operational_state in partition:
                if matching_ids is not None and device.id not in matching_ids:
                    continue
                payload = _row_to_json_dict(device, columns, operational_state)
                chunk = json.dumps(payload, ensure_ascii=False)
                yield ("" if first else ",") + chunk
                first = False
        yield "]"

    async def iter_inventory_csv(
        self,
        session: AsyncSession,
        *,
        columns: list[InventoryColumn],
        filters: DeviceQueryFilters | None,
    ) -> AsyncIterator[str]:
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow([c.value for c in columns])
        yield buffer.getvalue()
        buffer.seek(0)
        buffer.truncate(0)

        matching_ids = await self._resolve_group_filter(session, filters)
        stmt = _base_query(filters).execution_options(yield_per=_CHUNK)
        result = await session.stream(stmt)
        async for partition in result.partitions(_CHUNK):
            for device, operational_state in partition:
                if matching_ids is not None and device.id not in matching_ids:
                    continue
                writer.writerow(_row_to_csv_values(device, columns, operational_state))
            yield buffer.getvalue()
            buffer.seek(0)
            buffer.truncate(0)
