"""Tests for PackStatusService.upsert_plugin_status.

Verifies that plugin status is keyed by (host_id, runtime_id, plugin_name)
and that a second call with updated version/status updates the row instead of
inserting a duplicate.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import Mock

import pytest
from sqlalchemy import select, text

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

from app.hosts.models import Host, HostPluginRuntimeStatus, HostStatus, OSType
from app.packs.services.feature_dispatch import FeatureService
from app.packs.services.status import PackStatusService
from tests.helpers import test_event_bus as event_bus

_status_svc = PackStatusService(
    publisher=event_bus, feature=FeatureService(publisher=event_bus, circuit_breaker=Mock())
)


@pytest.mark.asyncio
async def test_plugin_status_is_keyed_by_runtime(db_session: AsyncSession) -> None:
    """upsert_plugin_status inserts a row keyed by (host_id, runtime_id, plugin_name)."""
    host = Host(
        hostname="h-plugin-status.local",
        ip="10.0.0.88",
        os_type=OSType.linux,
        agent_port=5100,
        status=HostStatus.online,
    )
    db_session.add(host)
    await db_session.commit()

    await _status_svc.upsert_plugin_status(
        db_session,
        host_id=host.id,
        runtime_id="runtime-abc",
        plugin_name="images",
        version="3.0.0",
        status="installed",
    )
    await db_session.commit()

    rows = (
        await db_session.execute(
            text("SELECT plugin_name, status FROM host_plugin_runtime_statuses WHERE runtime_id = :r"),
            {"r": "runtime-abc"},
        )
    ).fetchall()
    assert rows == [("images", "installed")]


@pytest.mark.asyncio
async def test_plugin_status_upsert_updates_existing_row(db_session: AsyncSession) -> None:
    """Calling upsert_plugin_status twice updates version+status without inserting a duplicate."""
    host = Host(
        hostname="h-plugin-upsert.local",
        ip="10.0.0.89",
        os_type=OSType.linux,
        agent_port=5100,
        status=HostStatus.online,
    )
    db_session.add(host)
    await db_session.commit()

    await _status_svc.upsert_plugin_status(
        db_session,
        host_id=host.id,
        runtime_id="runtime-xyz",
        plugin_name="images",
        version="3.0.0",
        status="installed",
    )
    await db_session.commit()

    await _status_svc.upsert_plugin_status(
        db_session,
        host_id=host.id,
        runtime_id="runtime-xyz",
        plugin_name="images",
        version="3.1.0",
        status="installed",
    )
    await db_session.commit()

    all_rows = (
        (
            await db_session.execute(
                select(HostPluginRuntimeStatus).where(
                    HostPluginRuntimeStatus.host_id == host.id,
                    HostPluginRuntimeStatus.runtime_id == "runtime-xyz",
                )
            )
        )
        .scalars()
        .all()
    )

    assert len(all_rows) == 1
    assert all_rows[0].version == "3.1.0"
    assert all_rows[0].status == "installed"
