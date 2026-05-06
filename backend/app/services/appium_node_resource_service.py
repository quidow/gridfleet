"""Typed Appium parallel-resource allocator."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import delete, select, text, update

from app.models.appium_node import AppiumNode
from app.models.appium_node_resource_claim import AppiumNodeResourceClaim

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.type_defs import JsonValue

POOL_SIZE = 1000


class PoolExhaustedError(Exception):
    """Raised when no free port is available within POOL_SIZE of the start."""


async def reserve(
    db: AsyncSession,
    *,
    host_id: uuid.UUID,
    capability_key: str,
    start_port: int,
    node_id: uuid.UUID | None = None,
    owner_token: str | None = None,
    expires_at: datetime | None = None,
) -> int:
    """Reserve the first free port in [start_port, start_port + POOL_SIZE)."""
    if (node_id is None) == (owner_token is None):
        raise ValueError("Exactly one of node_id or owner_token must be provided")
    if owner_token is not None and expires_at is None:
        raise ValueError("Temporary reservations require expires_at")

    await db.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(CAST(:host_id AS text) || ':' || :capability_key, 0))"),
        {"host_id": str(host_id), "capability_key": capability_key},
    )

    stmt = text(
        """
        INSERT INTO appium_node_resource_claims
            (host_id, capability_key, port, node_id, owner_token, expires_at)
        SELECT
            CAST(:host_id AS uuid), CAST(:capability_key AS varchar), candidate.port,
            CAST(:node_id AS uuid), CAST(:owner_token AS varchar), CAST(:expires_at AS timestamptz)
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
            "owner_token": owner_token,
            "expires_at": expires_at,
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


async def release_temporary(
    db: AsyncSession,
    *,
    host_id: uuid.UUID,
    owner_token: str,
) -> int:
    result = await db.execute(
        delete(AppiumNodeResourceClaim).where(
            AppiumNodeResourceClaim.host_id == host_id,
            AppiumNodeResourceClaim.owner_token == owner_token,
            AppiumNodeResourceClaim.node_id.is_(None),
        )
    )
    return _rowcount(result)


async def transfer_temporary_to_managed(
    db: AsyncSession,
    *,
    host_id: uuid.UUID,
    owner_token: str,
    node_id: uuid.UUID,
) -> int:
    result = await db.execute(
        update(AppiumNodeResourceClaim)
        .where(
            AppiumNodeResourceClaim.host_id == host_id,
            AppiumNodeResourceClaim.owner_token == owner_token,
            AppiumNodeResourceClaim.node_id.is_(None),
        )
        .values(node_id=node_id, owner_token=None, expires_at=None)
    )
    return _rowcount(result)


async def get_capabilities(db: AsyncSession, *, node_id: uuid.UUID) -> dict[str, JsonValue]:
    """Return port claims merged with non-port live capabilities for a node."""
    port_rows = (
        await db.execute(
            select(AppiumNodeResourceClaim.capability_key, AppiumNodeResourceClaim.port).where(
                AppiumNodeResourceClaim.node_id == node_id
            )
        )
    ).all()
    extras_row = (await db.execute(select(AppiumNode.live_capabilities).where(AppiumNode.id == node_id))).first()
    extras: dict[str, JsonValue] = dict(extras_row[0]) if extras_row and extras_row[0] else {}
    merged: dict[str, JsonValue] = dict(extras)
    for key, port in port_rows:
        merged[key] = port
    return merged


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


async def get_temporary_capabilities(
    db: AsyncSession,
    *,
    host_id: uuid.UUID,
    owner_token: str,
) -> dict[str, JsonValue]:
    rows = (
        await db.execute(
            select(AppiumNodeResourceClaim.capability_key, AppiumNodeResourceClaim.port).where(
                AppiumNodeResourceClaim.host_id == host_id,
                AppiumNodeResourceClaim.owner_token == owner_token,
                AppiumNodeResourceClaim.node_id.is_(None),
            )
        )
    ).all()
    return {key: port for key, port in rows}


async def sweep_expired(db: AsyncSession) -> int:
    now = datetime.now(UTC)
    result = await db.execute(
        delete(AppiumNodeResourceClaim).where(
            AppiumNodeResourceClaim.expires_at.is_not(None),
            AppiumNodeResourceClaim.expires_at < now,
            AppiumNodeResourceClaim.node_id.is_(None),
        )
    )
    return _rowcount(result)


def _rowcount(result: object) -> int:
    rowcount = getattr(result, "rowcount", 0)
    return int(rowcount or 0)
