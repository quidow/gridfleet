"""Admin endpoints for managed Appium node rows."""

from __future__ import annotations

import uuid  # noqa: TC003 - FastAPI evaluates path parameter annotations at runtime.

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.appium_nodes.models import AppiumNode
from app.appium_nodes.services import locking as appium_node_locking
from app.dependencies import AdminDep, DbDep  # noqa: TC001 - FastAPI route dependency annotations are runtime API.
from app.models.device_event import DeviceEventType
from app.schemas.device import AppiumNodeRead
from app.services import device_locking
from app.services.device_event_service import record_event

router = APIRouter(prefix="/api/admin/appium-nodes", tags=["admin"])


class ClearTransitionBody(BaseModel):
    reason: str | None = None


@router.post("/{node_id}/clear-transition", response_model=AppiumNodeRead)
async def clear_transition(
    node_id: uuid.UUID,
    body: ClearTransitionBody,
    db: DbDep,
    username: AdminDep,
) -> AppiumNode:
    node = await db.get(AppiumNode, node_id)
    if node is None:
        raise HTTPException(status_code=404, detail="AppiumNode not found")

    await device_locking.lock_device(db, node.device_id)
    locked_node = await appium_node_locking.lock_appium_node_for_device(db, node.device_id)
    if locked_node is None:
        raise HTTPException(status_code=404, detail="AppiumNode not found")
    if locked_node.transition_token is None:
        await db.refresh(locked_node)
        return locked_node

    old_token = locked_node.transition_token
    locked_node.transition_token = None
    locked_node.transition_deadline = None
    await record_event(
        db,
        locked_node.device_id,
        DeviceEventType.desired_state_changed,
        {
            "field": "transition_token",
            "old_value": str(old_token),
            "new_value": None,
            "caller": "admin_clear_transition",
            "actor": username,
            "reason": body.reason,
        },
    )
    await db.commit()
    await db.refresh(locked_node)
    return locked_node
