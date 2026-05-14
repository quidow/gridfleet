"""Phase 2: narrowed exception handling in devices_control reconnect route (Site 4)."""

import uuid
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.appium_node import AppiumDesiredState, AppiumNode
from app.models.device import ConnectionType, DeviceOperationalState, DeviceType
from app.models.device_intent import DeviceIntent
from app.models.host import Host
from app.routers import devices_control
from app.services.intent_reconciler import _reconcile_device
from app.services.intent_service import IntentService
from app.services.intent_types import NODE_PROCESS, PRIORITY_HEALTH_FAILURE, RECOVERY, IntentRegistration
from app.services.node_service_types import NodeManagerError, NodePortConflictError
from tests.helpers import create_device


def _reconnect_device(**overrides: object) -> SimpleNamespace:
    """Build a minimal reconnect-eligible device SimpleNamespace."""
    host = SimpleNamespace(ip="10.0.0.1", agent_port=5100)
    values: dict[str, Any] = {
        "id": uuid.uuid4(),
        "pack_id": "appium-uiautomator2",
        "platform_id": "android_mobile",
        "device_type": DeviceType.real_device,
        "connection_type": ConnectionType.network,
        "ip_address": "10.0.0.20",
        "host": host,
        "host_id": uuid.uuid4(),
        "connection_target": "10.0.0.20:5555",
        "identity_value": "stable",
        "auto_manage": True,
        "appium_node": SimpleNamespace(observed_running=True),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


_RESOLVED = SimpleNamespace(lifecycle_actions=[{"id": "reconnect"}])


@pytest.mark.db
async def test_reconnect_persists_session_viability_clear_before_intent_reconcile(
    db_session: AsyncSession,
    db_host: Host,
    seeded_driver_packs: None,
) -> None:
    del seeded_driver_packs
    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="reconnect-clears-viability",
        identity_value="reconnect-clears-viability",
        connection_type="network",
        ip_address="10.0.0.20",
        connection_target="10.0.0.20:5555",
        operational_state=DeviceOperationalState.offline,
        session_viability_status="failed",
        session_viability_error="Appium node is not running",
    )
    node = AppiumNode(
        device_id=device.id,
        port=4723,
        grid_url="http://grid:4444",
        desired_state=AppiumDesiredState.running,
        desired_port=4723,
        pid=123,
        active_connection_target=device.connection_target,
    )
    db_session.add(node)
    await db_session.flush()
    service = IntentService(db_session)
    await service.register_intents(
        device_id=device.id,
        reason="health failure",
        intents=[
            IntentRegistration(
                source=f"health_failure:node:{device.id}",
                axis=NODE_PROCESS,
                payload={"action": "stop", "priority": PRIORITY_HEALTH_FAILURE, "stop_mode": "graceful"},
            ),
            IntentRegistration(
                source=f"health_failure:recovery:{device.id}",
                axis=RECOVERY,
                payload={"allowed": False, "priority": PRIORITY_HEALTH_FAILURE, "reason": "Node health failure"},
            ),
        ],
    )
    await _reconcile_device(db_session, device.id)
    await db_session.commit()
    await db_session.refresh(device)
    assert device.session_viability_status == "failed"
    assert device.recovery_allowed is False

    with (
        patch.object(devices_control, "pack_device_lifecycle_action", new=AsyncMock(return_value={"success": True})),
        patch.object(devices_control.node_manager, "restart_node", new=AsyncMock(return_value=node)),
    ):
        result = await devices_control.reconnect_device(device.id, db=db_session)

    assert result["success"] is True
    await db_session.refresh(device)
    assert device.session_viability_status is None
    assert device.session_viability_error is None
    assert device.recovery_allowed is True
    remaining_sources = set((await db_session.execute(select(DeviceIntent.source))).scalars().all())
    assert f"health_failure:node:{device.id}" not in remaining_sources
    assert f"health_failure:recovery:{device.id}" not in remaining_sources


# ---------------------------------------------------------------------------
# Site 4: reconnect_device — NodeManagerError → 502
# ---------------------------------------------------------------------------


async def test_reconnect_node_manager_error_returns_502() -> None:
    """NodeManagerError from restart_node must map to HTTP 502."""
    device_id = uuid.uuid4()
    device = _reconnect_device(id=device_id)
    db = SimpleNamespace(commit=AsyncMock(), flush=AsyncMock())

    with (
        patch.object(devices_control, "get_device_or_404", new=AsyncMock(return_value=device)),
        patch.object(devices_control, "resolve_pack_platform", new=AsyncMock(return_value=_RESOLVED)),
        patch.object(devices_control, "platform_has_lifecycle_action", new=Mock(return_value=True)),
        patch.object(
            devices_control,
            "pack_device_lifecycle_action",
            new=AsyncMock(return_value={"success": True}),
        ),
        patch.object(devices_control, "revoke_intents_and_reconcile", new=AsyncMock()),
        patch.object(
            devices_control.node_manager,
            "restart_node",
            new=AsyncMock(side_effect=NodeManagerError("restart failed")),
        ),
        pytest.raises(HTTPException) as exc,
    ):
        await devices_control.reconnect_device(device_id, db=db)  # type: ignore[arg-type]

    assert exc.value.status_code == 502
    assert "restart failed" in exc.value.detail


async def test_reconnect_port_conflict_error_returns_502() -> None:
    """NodePortConflictError from start_node must also map to HTTP 502."""
    device_id = uuid.uuid4()
    # Use observed_running=False so start_node is invoked (not restart_node)
    device = _reconnect_device(id=device_id, appium_node=SimpleNamespace(observed_running=False))
    db = SimpleNamespace(commit=AsyncMock(), flush=AsyncMock())

    with (
        patch.object(devices_control, "get_device_or_404", new=AsyncMock(return_value=device)),
        patch.object(devices_control, "resolve_pack_platform", new=AsyncMock(return_value=_RESOLVED)),
        patch.object(devices_control, "platform_has_lifecycle_action", new=Mock(return_value=True)),
        patch.object(
            devices_control,
            "pack_device_lifecycle_action",
            new=AsyncMock(return_value={"success": True}),
        ),
        patch.object(devices_control, "revoke_intents_and_reconcile", new=AsyncMock()),
        patch.object(
            devices_control.node_manager,
            "start_node",
            new=AsyncMock(side_effect=NodePortConflictError("port occupied")),
        ),
        pytest.raises(HTTPException) as exc,
    ):
        await devices_control.reconnect_device(device_id, db=db)  # type: ignore[arg-type]

    assert exc.value.status_code == 502
    assert "port occupied" in exc.value.detail


async def test_reconnect_inner_http_400_propagates_unchanged() -> None:
    """Inner HTTPException(400) from host_id check must NOT be re-wrapped as 502.

    This was a bug in the original bare except — the inner 400 was caught and
    re-raised as 502.  After narrowing, HTTPException propagates unchanged.
    """
    device_id = uuid.uuid4()
    # host_id=None triggers the inner raise HTTPException(400)
    device = _reconnect_device(
        id=device_id,
        host_id=None,
        appium_node=SimpleNamespace(observed_running=False),
    )
    db = SimpleNamespace(commit=AsyncMock(), flush=AsyncMock())

    with (
        patch.object(devices_control, "get_device_or_404", new=AsyncMock(return_value=device)),
        patch.object(devices_control, "resolve_pack_platform", new=AsyncMock(return_value=_RESOLVED)),
        patch.object(devices_control, "platform_has_lifecycle_action", new=Mock(return_value=True)),
        patch.object(
            devices_control,
            "pack_device_lifecycle_action",
            new=AsyncMock(return_value={"success": True}),
        ),
        patch.object(devices_control, "revoke_intents_and_reconcile", new=AsyncMock()),
        pytest.raises(HTTPException) as exc,
    ):
        await devices_control.reconnect_device(device_id, db=db)  # type: ignore[arg-type]

    # Must be 400, NOT 502
    assert exc.value.status_code == 400
    assert "no host assigned" in exc.value.detail


async def test_reconnect_unexpected_exception_bubbles() -> None:
    """Unexpected RuntimeError must NOT be caught — it bubbles past the narrowed except."""
    device_id = uuid.uuid4()
    device = _reconnect_device(id=device_id)
    db = SimpleNamespace(commit=AsyncMock(), flush=AsyncMock())

    with (
        patch.object(devices_control, "get_device_or_404", new=AsyncMock(return_value=device)),
        patch.object(devices_control, "resolve_pack_platform", new=AsyncMock(return_value=_RESOLVED)),
        patch.object(devices_control, "platform_has_lifecycle_action", new=Mock(return_value=True)),
        patch.object(
            devices_control,
            "pack_device_lifecycle_action",
            new=AsyncMock(return_value={"success": True}),
        ),
        patch.object(devices_control, "revoke_intents_and_reconcile", new=AsyncMock()),
        patch.object(
            devices_control.node_manager,
            "restart_node",
            new=AsyncMock(side_effect=RuntimeError("unexpected boom")),
        ),
        pytest.raises(RuntimeError, match="unexpected boom"),
    ):
        await devices_control.reconnect_device(device_id, db=db)  # type: ignore[arg-type]
