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

from app.devices.models import Device
from app.devices.services.service import _apply_device_filters

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from sqlalchemy.ext.asyncio import AsyncSession

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


def _column_value(device: Device, column: InventoryColumn) -> object:
    v = column.value
    if v == "host.id":
        return str(device.host.id) if device.host else None
    if v == "host.hostname":
        return device.host.hostname if device.host else None
    if v == "identity.scheme":
        return device.identity_scheme
    if v == "identity.scope":
        return device.identity_scope
    if v == "identity.value":
        return device.identity_value
    if v.startswith("hardware."):
        attr = {
            "hardware.battery_level_percent": "battery_level_percent",
            "hardware.battery_temperature_c": "battery_temperature_c",
            "hardware.charging_state": "charging_state",
            "hardware.health_status": "hardware_health_status",
            "hardware.telemetry_reported_at": "hardware_telemetry_reported_at",
        }[v]
        return getattr(device, attr)
    if v.startswith("verification."):
        attr = {
            "verification.verified_at": "verified_at",
            "verification.session_viability_status": "session_viability_status",
            "verification.device_checks_healthy": "device_checks_healthy",
            "verification.device_checks_checked_at": "device_checks_checked_at",
        }[v]
        return getattr(device, attr)
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


def _row_to_json_dict(device: Device, columns: list[InventoryColumn]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for col in columns:
        raw = _column_value(device, col)
        if isinstance(raw, (dict, list)):
            _nested_set(out, col.value, raw)
        else:
            _nested_set(out, col.value, _normalize_scalar(raw))
    return out


def _row_to_csv_values(device: Device, columns: list[InventoryColumn]) -> list[str]:
    out: list[str] = []
    for col in columns:
        raw = _column_value(device, col)
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


def _base_query(filters: DeviceQueryFilters | None) -> Select[tuple[Device]]:
    stmt: Select[tuple[Device]] = select(Device).options(selectinload(Device.host)).order_by(Device.created_at.asc())
    if filters is not None:
        stmt = cast("DeviceListStatement", _apply_device_filters(stmt, filters))
    return stmt


class InventoryExportService:
    """Container-held streaming inventory export."""

    async def iter_inventory_json(
        self,
        session: AsyncSession,
        *,
        columns: list[InventoryColumn],
        filters: DeviceQueryFilters | None,
    ) -> AsyncIterator[str]:
        stmt = _base_query(filters).execution_options(yield_per=_CHUNK)
        result = await session.stream(stmt)
        yield "["
        first = True
        async for partition in result.partitions(_CHUNK):
            for (device,) in partition:
                payload = _row_to_json_dict(device, columns)
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

        stmt = _base_query(filters).execution_options(yield_per=_CHUNK)
        result = await session.stream(stmt)
        async for partition in result.partitions(_CHUNK):
            for (device,) in partition:
                writer.writerow(_row_to_csv_values(device, columns))
            yield buffer.getvalue()
            buffer.seek(0)
            buffer.truncate(0)
