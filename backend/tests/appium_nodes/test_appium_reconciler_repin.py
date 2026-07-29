"""D3: a desired-port re-pin moves node ownership and survives the next tick."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from sqlalchemy import select

from app.appium_nodes.models import AppiumDesiredState, AppiumNode
from app.appium_nodes.services.reconciler import _repin_desired_port
from app.appium_nodes.services.reconciler_convergence import DesiredRow
from app.devices.models import DeviceOperationalState
from app.devices.services.intent_reconciler import reconcile_device
from tests.contracts.test_no_direct_device_state_writes import PROTECTED_COLUMN_WRITERS
from tests.fakes import FakeSettingsReader
from tests.helpers import create_device
from tests.helpers import test_event_bus as event_bus

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.hosts.models import Host

CONFLICT_PORT = 4723
SETTINGS = FakeSettingsReader({"appium.port_range_start": 4723, "appium.port_range_end": 4823})


def _row(device: object, host_id: object, node: AppiumNode) -> DesiredRow:
    return DesiredRow(
        device_id=device.id,
        host_id=host_id,
        node_id=node.id,
        connection_target=device.connection_target,
        desired_state="running",
        desired_port=CONFLICT_PORT,
        port=CONFLICT_PORT,
        pid=None,
        active_connection_target=None,
        stop_pending=False,
    )


async def _seed(db_session: AsyncSession, host_id: object, name: str) -> tuple[object, AppiumNode]:
    device = await create_device(
        db_session,
        host_id=host_id,
        name=name,
        identity_value=f"{name}-001",
        connection_target=f"{name}-target",
        operational_state=DeviceOperationalState.available,
    )
    node = AppiumNode(
        device_id=device.id,
        port=CONFLICT_PORT,
        pid=None,
        desired_state=AppiumDesiredState.running,
        desired_port=CONFLICT_PORT,
    )
    db_session.add(node)
    await db_session.commit()
    return device, node


@pytest.mark.db
async def test_repin_moves_node_port_with_desired_port(db_session: AsyncSession, db_host: Host) -> None:
    device, node = await _seed(db_session, db_host.id, "repin-owner")

    await _repin_desired_port(
        db_session, _row(device, db_host.id, node), conflict_port=CONFLICT_PORT, settings=SETTINGS
    )
    await db_session.commit()
    await db_session.refresh(node)

    assert node.desired_port is not None
    assert node.desired_port != CONFLICT_PORT
    assert node.port == node.desired_port, "ownership stayed on the conflicted port"


@pytest.mark.db
async def test_repin_survives_the_next_intent_reconciler_tick(db_session: AsyncSession, db_host: Host) -> None:
    """The intent reconciler re-derives desired_port from node.port every ~5s.
    With ownership moved, that recompute lands on the re-pinned port."""
    device, node = await _seed(db_session, db_host.id, "repin-survives")

    await _repin_desired_port(
        db_session, _row(device, db_host.id, node), conflict_port=CONFLICT_PORT, settings=SETTINGS
    )
    await db_session.commit()
    await db_session.refresh(node)
    repinned = node.desired_port

    await reconcile_device(db_session, device.id, publisher=event_bus)
    await db_session.commit()

    reloaded = (await db_session.execute(select(AppiumNode).where(AppiumNode.device_id == device.id))).scalar_one()
    assert reloaded.desired_port == repinned, "the intent reconciler undid the re-pin"
    assert reloaded.port == repinned


def test_desired_port_still_has_exactly_one_sanctioned_writer() -> None:
    """D3 moves ownership; it must not add a second desired_port writer."""
    assert PROTECTED_COLUMN_WRITERS["desired_port"] == frozenset({"app/appium_nodes/services/desired_state_writer.py"})
