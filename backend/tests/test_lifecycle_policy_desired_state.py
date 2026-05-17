"""Lifecycle policy orchestration intent tests."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.appium_nodes.models import AppiumDesiredState, AppiumNode
from app.devices.models import DeviceEvent, DeviceEventType, DeviceIntent
from tests.helpers import create_device

if TYPE_CHECKING:
    from uuid import UUID

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.devices.services.intent_types import IntentRegistration
    from app.hosts.models import Host
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

    from app.devices.services import lifecycle_policy as lifecycle_policy
    from app.devices.services.intent import register_intents_and_reconcile as _real_register

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
            "app.devices.services.lifecycle_policy.register_intents_and_reconcile",
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
    # Auto-recovery must not pin a stale port. Pinning desired_port in the
    # intent payload causes port-mismatch flap once the agent restarts the
    # node on a different allocator-picked port: when this intent becomes the
    # priority winner after higher-priority transient intents (active_session,
    # health_failure) clear, ``decide_convergence_action`` sees observed.port
    # != row.desired_port and fires ``stop``, taking the device offline. The
    # intent_reconciler falls back to live ``node.port`` when the payload
    # omits ``desired_port`` (see app/devices/services/intent_reconciler.py).
    assert "desired_port" not in intent.payload


async def test_auto_recovery_intent_falls_back_to_live_node_port(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    """Regression: live ``node.port`` wins over any port the intent payload
    might carry, so a restart on a new port does not produce a phantom stop
    on the next intent-reconciliation cycle.

    Scenario reproduces the firetv-12 flap pattern: auto_recovery registered
    when node.port was P1; agent later restarted on P2 (allocator-picked);
    intent_reconciler must drive ``node.desired_port == P2``, not P1.
    """
    from app.devices.services.intent_reconciler import reconcile_device
    from app.devices.services.intent_types import NODE_PROCESS, PRIORITY_AUTO_RECOVERY

    device = await create_device(db_session, host_id=db_host.id, name="port-flap-repro", verified=True)
    node = AppiumNode(
        device_id=device.id,
        port=4757,
        grid_url="http://hub:4444",
        desired_port=None,
        pid=12345,
        active_connection_target="127.0.0.1:4757",
        desired_state=AppiumDesiredState.running,
    )
    db_session.add(node)
    await db_session.flush()
    db_session.add(
        DeviceIntent(
            device_id=device.id,
            source=f"auto_recovery:node:{device.id}",
            axis=NODE_PROCESS,
            payload={"action": "start", "priority": PRIORITY_AUTO_RECOVERY},
        )
    )
    await db_session.commit()

    await reconcile_device(db_session, device.id)
    await db_session.commit()

    await db_session.refresh(node)
    assert node.desired_state == AppiumDesiredState.running
    assert node.desired_port == 4757, (
        f"intent_reconciler must drive desired_port to live node.port (4757); got {node.desired_port}"
    )


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

    from app.devices.services import lifecycle_policy_actions as lifecycle_policy_actions

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

    from app.devices.services import lifecycle_policy_actions as lifecycle_policy_actions

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
