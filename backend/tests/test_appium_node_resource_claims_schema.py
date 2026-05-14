"""Schema and cascade guards for typed Appium resource claims."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from sqlalchemy import inspect, select

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.hosts.models import Host


@pytest.mark.db
@pytest.mark.asyncio
async def test_appium_node_resource_claims_table_exists(db_session: AsyncSession) -> None:
    def _inspect(sync_conn: Connection) -> None:
        insp = inspect(sync_conn)
        assert "appium_node_resource_claims" in insp.get_table_names()
        cols = {c["name"]: c for c in insp.get_columns("appium_node_resource_claims")}
        assert cols.keys() >= {
            "id",
            "host_id",
            "capability_key",
            "port",
            "node_id",
            "claimed_at",
        }
        assert cols["host_id"]["nullable"] is False
        assert cols["capability_key"]["nullable"] is False
        assert cols["port"]["nullable"] is False
        assert cols["node_id"]["nullable"] is False

        unique_cols = {
            tuple(sorted(c["column_names"])) for c in insp.get_unique_constraints("appium_node_resource_claims")
        }
        assert ("capability_key", "host_id", "port") in unique_cols

        fks = insp.get_foreign_keys("appium_node_resource_claims")
        node_fk = next((f for f in fks if "node_id" in f["constrained_columns"]), None)
        assert node_fk is not None
        assert node_fk["referred_table"] == "appium_nodes"
        assert node_fk["options"].get("ondelete") == "CASCADE"

    await db_session.run_sync(lambda s: _inspect(s.connection()))


@pytest.mark.db
@pytest.mark.asyncio
async def test_managed_claim_cascades_when_node_deleted(db_session: AsyncSession, db_host: Host) -> None:
    from app.appium_nodes.models import AppiumDesiredState, AppiumNode, AppiumNodeResourceClaim
    from tests.helpers import create_device

    device = await create_device(db_session, host_id=db_host.id, name="cascade-device")
    node = AppiumNode(
        device_id=device.id,
        port=4723,
        grid_url="http://hub:4444",
        desired_state=AppiumDesiredState.running,
        desired_port=4723,
        pid=0,
        active_connection_target="",
    )
    db_session.add(node)
    await db_session.flush()

    claim = AppiumNodeResourceClaim(
        host_id=device.host_id,
        capability_key="appium:mjpegServerPort",
        port=8001,
        node_id=node.id,
    )
    db_session.add(claim)
    await db_session.commit()

    await db_session.delete(node)
    await db_session.commit()

    remaining = (await db_session.execute(select(AppiumNodeResourceClaim))).scalars().all()
    assert remaining == [], "Cascade delete must drop the claim when its node is deleted"
