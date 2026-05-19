"""Regression tests for unified operator-driven Appium node lifecycle writes."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import select

from app.appium_nodes.models import AppiumDesiredState, AppiumNode
from app.devices.models import DeviceIntent
from app.devices.services import state_write_guard
from app.devices.services.intent_reconciler import reconcile_device
from tests.helpers import create_device

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.hosts.models import Host

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("seeded_driver_packs")]


async def test_stale_operator_start_intent_does_not_force_old_desired_port(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    """Pre-PR-#301-shape stale operator:start intent must not overwrite a freshly
    allocated desired_port. Repro for the Roku flip observed on 2026-05-18:
    stale payload re-asserted desired_port=4724 every intent_reconciler tick,
    while start_node had just allocated port 4725.
    """
    device = await create_device(db_session, host_id=db_host.id, name="roku-flip-repro", verified=True)
    with state_write_guard.bypass():
        node = AppiumNode(
            device_id=device.id,
            port=4725,
            grid_url="http://hub:4444",
            desired_state=AppiumDesiredState.running,
            desired_port=4725,
            pid=27765,
            active_connection_target=device.connection_target,
        )
    db_session.add(node)
    await db_session.flush()
    device.appium_node = node

    stale_token = uuid.uuid4()
    stale_deadline = datetime.now(UTC) - timedelta(days=2)
    stale_intent = DeviceIntent(
        device_id=device.id,
        source=f"operator:start:{device.id}",
        axis="node_process",
        payload={
            "action": "start",
            "priority": 20,
            "desired_port": 4724,
            "transition_token": str(stale_token),
            "transition_deadline": stale_deadline.isoformat(),
        },
        precondition=None,  # pre-#301 row shape
        expires_at=None,
        created_at=stale_deadline - timedelta(minutes=2),
        updated_at=stale_deadline - timedelta(minutes=2),
    )
    db_session.add(stale_intent)
    await db_session.commit()

    await reconcile_device(db_session, device.id)
    await db_session.refresh(node)

    assert node.desired_port == 4725, (
        f"intent_reconciler reasserted stale desired_port={node.desired_port}; "
        "the unified intent path must refresh or expire stale operator:start payloads"
    )

    remaining = (
        (await db_session.execute(select(DeviceIntent).where(DeviceIntent.device_id == device.id))).scalars().all()
    )
    assert all(intent.payload.get("transition_token") != str(stale_token) for intent in remaining), (
        "stale transition_token must not survive after reconcile"
    )
