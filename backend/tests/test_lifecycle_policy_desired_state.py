"""Lifecycle policy orchestration intent tests."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.models.appium_node import AppiumDesiredState, AppiumNode
from app.models.device_event import DeviceEvent, DeviceEventType
from app.models.device_intent import DeviceIntent
from tests.helpers import create_device

if TYPE_CHECKING:
    from uuid import UUID

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.devices.services.intent_types import IntentRegistration
    from app.models.host import Host
pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("seeded_driver_packs")]


async def test_attempt_auto_recovery_registers_auto_recovery_intent(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="dw-recover", verified=True)
    node = AppiumNode(
        device_id=device.id,
        port=4723,
        grid_url="http://hub:4444",
        desired_port=None,
        pid=None,
        active_connection_target=None,
        desired_state=AppiumDesiredState.stopped,
    )
    db_session.add(node)
    await db_session.commit()

    from app.devices.services.intent import register_intents_and_reconcile as _real_register
    from app.services import lifecycle_policy

    async def _register_then_mark_running(
        db: AsyncSession,
        *,
        device_id: UUID,
        intents: list[IntentRegistration],
        reason: str,
    ) -> None:
        """Run real intent registration so the auto_recovery intent row is
        written, then simulate the reconciler bringing the node up so
        wait_for_node_running exits on its first poll instead of blocking
        for its full 60s timeout."""
        await _real_register(db, device_id=device_id, intents=intents, reason=reason)
        observed_node = (
            await db.execute(select(AppiumNode).where(AppiumNode.device_id == device_id))
        ).scalar_one_or_none()
        if observed_node is not None:
            observed_node.pid = 12345
            observed_node.active_connection_target = "127.0.0.1:4723"

    with (
        patch.object(
            lifecycle_policy.session_viability,
            "run_session_viability_probe",
            new=AsyncMock(return_value={"status": "passed"}),
        ),
        patch(
            "app.services.lifecycle_policy.register_intents_and_reconcile",
            new=AsyncMock(side_effect=_register_then_mark_running),
        ),
    ):
        await lifecycle_policy.attempt_auto_recovery(
            db_session,
            device,
            source="health_recovery",
            reason="test",
        )

    intent = (
        await db_session.execute(
            select(DeviceIntent).where(
                DeviceIntent.device_id == device.id,
                DeviceIntent.source == f"auto_recovery:node:{device.id}",
            )
        )
    ).scalar_one()
    assert intent.payload["action"] == "start"
    assert intent.payload["desired_port"] == 4723


async def test_handle_node_crash_tags_desired_state_with_lifecycle_crash(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="dw-crash", verified=True)
    node = AppiumNode(
        device_id=device.id,
        port=4723,
        grid_url="http://hub:4444",
        active_connection_target="",
        desired_state=AppiumDesiredState.running,
        desired_port=4723,
        pid=99,
    )
    db_session.add(node)
    await db_session.commit()

    from app.services import lifecycle_policy_actions

    await lifecycle_policy_actions.handle_node_crash(
        db_session,
        device,
        source="connectivity_lost",
        reason="agent disconnected",
    )

    events = (
        (
            await db_session.execute(
                select(DeviceEvent).where(
                    DeviceEvent.device_id == device.id,
                    DeviceEvent.event_type == DeviceEventType.desired_state_changed,
                )
            )
        )
        .scalars()
        .all()
    )
    assert any(
        event.details is not None
        and event.details.get("caller") == "intent_reconciler"
        and event.details.get("new_desired_state") == "stopped"
        for event in events
    )


async def test_handle_node_crash_writes_desired_stopped_when_node_already_stopped(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="dw-crash-stopped", verified=True)
    node = AppiumNode(
        device_id=device.id,
        port=4723,
        grid_url="http://hub:4444",
        pid=None,
        active_connection_target=None,
        desired_state=AppiumDesiredState.running,
        desired_port=4723,
    )
    db_session.add(node)
    await db_session.commit()

    from app.services import lifecycle_policy_actions

    await lifecycle_policy_actions.handle_node_crash(
        db_session,
        device,
        source="health_check_fail",
        reason="probe failed",
    )

    events = (
        (
            await db_session.execute(
                select(DeviceEvent).where(
                    DeviceEvent.device_id == device.id,
                    DeviceEvent.event_type == DeviceEventType.desired_state_changed,
                )
            )
        )
        .scalars()
        .all()
    )
    desired_events = [
        event for event in events if event.details is not None and event.details.get("new_desired_state") == "stopped"
    ]
    assert len(desired_events) == 1
    assert desired_events[0].details is not None
    assert desired_events[0].details["caller"] == "intent_reconciler"
