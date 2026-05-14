"""Typed Appium parallel-resource allocator."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from sqlalchemy import delete, select, text

from app.appium_nodes.models import AppiumNode, AppiumNodeResourceClaim

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.type_defs import JsonValue

POOL_SIZE = 1000
INTERNAL_APPIUM_PORT_CAPABILITY = "gridfleet:appiumPort"


class PoolExhaustedError(Exception):
    """Raised when no free port is available within POOL_SIZE of the start."""


async def reserve(
    db: AsyncSession,
    *,
    host_id: uuid.UUID,
    capability_key: str,
    start_port: int,
    node_id: uuid.UUID,
) -> int:
    """Reserve the first free port in [start_port, start_port + POOL_SIZE)."""
    await db.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(CAST(:host_id AS text) || ':' || :capability_key, 0))"),
        {"host_id": str(host_id), "capability_key": capability_key},
    )

    stmt = text(
        """
        INSERT INTO appium_node_resource_claims
            (host_id, capability_key, port, node_id)
        SELECT
            CAST(:host_id AS uuid), CAST(:capability_key AS varchar), candidate.port,
            CAST(:node_id AS uuid)
        FROM generate_series(CAST(:start_port AS integer), CAST(:end_port AS integer)) AS candidate(port)
        WHERE NOT EXISTS (
            SELECT 1 FROM appium_node_resource_claims existing
            WHERE existing.host_id = CAST(:host_id AS uuid)
              AND existing.capability_key = CAST(:capability_key AS varchar)
              AND existing.port = candidate.port
        )
        ORDER BY candidate.port
        LIMIT 1
        RETURNING port
        """
    )
    result = await db.execute(
        stmt,
        {
            "host_id": host_id,
            "capability_key": capability_key,
            "node_id": node_id,
            "start_port": start_port,
            "end_port": start_port + POOL_SIZE - 1,
        },
    )
    row = result.first()
    if row is None:
        raise PoolExhaustedError(
            f"No free port for {capability_key} on host {host_id} within {POOL_SIZE} of {start_port}"
        )
    return int(row[0])


async def release_managed(db: AsyncSession, *, node_id: uuid.UUID) -> int:
    """Delete every managed claim for the given node. Returns rows deleted."""
    result = await db.execute(delete(AppiumNodeResourceClaim).where(AppiumNodeResourceClaim.node_id == node_id))
    return _rowcount(result)


async def release_capability(db: AsyncSession, *, node_id: uuid.UUID, capability_key: str) -> int:
    """Delete one managed capability claim for the given node."""
    result = await db.execute(
        delete(AppiumNodeResourceClaim).where(
            AppiumNodeResourceClaim.node_id == node_id,
            AppiumNodeResourceClaim.capability_key == capability_key,
        )
    )
    return _rowcount(result)


async def get_capabilities(db: AsyncSession, *, node_id: uuid.UUID) -> dict[str, JsonValue]:
    """Return port claims merged with non-port live capabilities for a node."""
    port_rows = (
        await db.execute(
            select(AppiumNodeResourceClaim.capability_key, AppiumNodeResourceClaim.port).where(
                AppiumNodeResourceClaim.node_id == node_id,
                AppiumNodeResourceClaim.capability_key != INTERNAL_APPIUM_PORT_CAPABILITY,
            )
        )
    ).all()
    extras_row = (await db.execute(select(AppiumNode.live_capabilities).where(AppiumNode.id == node_id))).first()
    extras: dict[str, JsonValue] = dict(extras_row[0]) if extras_row and extras_row[0] else {}
    merged: dict[str, JsonValue] = dict(extras)
    for key, port in port_rows:
        merged[key] = port
    return merged


async def list_claims_for_node(db: AsyncSession, *, node_id: uuid.UUID) -> list[AppiumNodeResourceClaim]:
    rows = await db.scalars(
        select(AppiumNodeResourceClaim)
        .where(AppiumNodeResourceClaim.node_id == node_id)
        .order_by(AppiumNodeResourceClaim.capability_key)
    )
    return list(rows)


async def set_node_extra_capability(
    db: AsyncSession,
    *,
    node_id: uuid.UUID,
    capability_key: str,
    value: JsonValue,
) -> None:
    """Store a non-port managed capability on appium_nodes.live_capabilities."""
    await db.execute(
        text(
            "UPDATE appium_nodes SET live_capabilities = "
            "COALESCE(live_capabilities, '{}'::jsonb) || "
            "jsonb_build_object(CAST(:k AS text), CAST(:v AS jsonb)) "
            "WHERE id = :node_id"
        ),
        {"k": capability_key, "v": json.dumps(value), "node_id": node_id},
    )


def _rowcount(result: object) -> int:
    rowcount = getattr(result, "rowcount", 0)
    return int(rowcount or 0)
