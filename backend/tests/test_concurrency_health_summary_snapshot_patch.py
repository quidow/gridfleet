from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from app.services import device_health_summary

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

pytestmark = pytest.mark.asyncio


async def test_health_summary_patch_preserves_concurrent_fields(
    db_session_maker: async_sessionmaker[AsyncSession],
) -> None:
    device_key = "race-health-summary"
    first_writer_about_to_write = asyncio.Event()
    second_writer_committed = asyncio.Event()
    original_set_value = device_health_summary.control_plane_state_store.set_value
    original_patch_value = device_health_summary.control_plane_state_store.patch_value

    async def gated_set_value(
        db: AsyncSession,
        namespace: str,
        key: str,
        value: object,
    ) -> None:
        if key == device_key and isinstance(value, dict) and "device_checks_healthy" in value:
            first_writer_about_to_write.set()
            await asyncio.wait_for(second_writer_committed.wait(), timeout=2.0)
        await original_set_value(db, namespace, key, value)
        if key == device_key and isinstance(value, dict) and "node_running" in value:
            second_writer_committed.set()

    async def gated_patch_value(
        db: AsyncSession,
        namespace: str,
        key: str,
        value: dict[str, object],
    ) -> None:
        if key == device_key and "device_checks_healthy" in value:
            first_writer_about_to_write.set()
            await asyncio.wait_for(second_writer_committed.wait(), timeout=2.0)
        await original_patch_value(db, namespace, key, value)
        if key == device_key and "node_running" in value:
            second_writer_committed.set()

    async def write_device_checks() -> None:
        async with db_session_maker() as session:
            await device_health_summary.patch_health_snapshot(
                session,
                device_key,
                {"device_checks_healthy": False},
            )
            await session.commit()

    async def write_node_state() -> None:
        await asyncio.wait_for(first_writer_about_to_write.wait(), timeout=2.0)
        async with db_session_maker() as session:
            await device_health_summary.patch_health_snapshot(
                session,
                device_key,
                {"node_running": True},
            )
            await session.commit()

    with (
        patch.object(device_health_summary.control_plane_state_store, "set_value", gated_set_value),
        patch.object(device_health_summary.control_plane_state_store, "patch_value", gated_patch_value),
    ):
        await asyncio.gather(write_device_checks(), write_node_state())

    async with db_session_maker() as session:
        snapshot = await device_health_summary.get_health_snapshot(session, device_key)

    assert snapshot is not None
    assert snapshot.get("device_checks_healthy") is False
    assert snapshot.get("node_running") is True
