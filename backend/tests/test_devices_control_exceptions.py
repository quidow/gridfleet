"""Phase 2: narrowed exception handling in devices_control reconnect route (Site 4)."""

import uuid
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import pytest
from fastapi import HTTPException

from app.models.device import ConnectionType, DeviceType
from app.routers import devices_control
from app.services.node_service_types import NodeManagerError, NodePortConflictError


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


# ---------------------------------------------------------------------------
# Site 4: reconnect_device — NodeManagerError → 502
# ---------------------------------------------------------------------------


async def test_reconnect_node_manager_error_returns_502() -> None:
    """NodeManagerError from restart_node must map to HTTP 502."""
    device_id = uuid.uuid4()
    device = _reconnect_device(id=device_id)
    db = SimpleNamespace(commit=AsyncMock())

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
    db = SimpleNamespace(commit=AsyncMock())

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
    db = SimpleNamespace(commit=AsyncMock())

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
    db = SimpleNamespace(commit=AsyncMock())

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
