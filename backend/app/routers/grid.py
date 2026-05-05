from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.services import device_service, grid_service

router = APIRouter(prefix="/api/grid", tags=["grid"])


@router.get("/status")
async def grid_status(db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    grid_data = await grid_service.get_grid_status()
    devices = await device_service.list_devices(db)

    registry_devices = []
    for device in devices:
        entry = {
            "id": str(device.id),
            "identity_value": device.identity_value,
            "connection_target": device.connection_target,
            "name": device.name,
            "platform_id": device.platform_id,
            "operational_state": device.operational_state.value,
            "hold": device.hold.value if device.hold else None,
            "node_state": device.appium_node.state.value if device.appium_node else None,
            "node_port": device.appium_node.port if device.appium_node else None,
        }
        registry_devices.append(entry)

    # Extract session and queue counts from Grid status
    value = grid_data.get("value", {})
    nodes = value.get("nodes", []) if isinstance(value, dict) else []
    active_sessions = sum(1 for n in nodes for s in n.get("slots", []) if s.get("session"))
    queue_size = len(value.get("sessionQueueRequests", [])) if isinstance(value, dict) else 0

    return {
        "grid": grid_data,
        "registry": {
            "device_count": len(registry_devices),
            "devices": registry_devices,
        },
        "active_sessions": active_sessions,
        "queue_size": queue_size,
    }


@router.get("/queue")
async def grid_queue() -> dict[str, Any]:
    grid_data = await grid_service.get_grid_status()
    value = grid_data.get("value", {})
    requests = value.get("sessionQueueRequests", []) if isinstance(value, dict) else []
    return {
        "queue_size": len(requests),
        "requests": requests,
    }
