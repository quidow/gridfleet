"""Admin endpoints for managed Appium node rows."""

from __future__ import annotations

import uuid  # noqa: TC003 - FastAPI evaluates path parameter annotations at runtime.

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: TC002 - FastAPI dependency annotation.

from app.database import get_db
from app.models.appium_node import AppiumNode, NodeState
from app.schemas.device import AppiumNodeRead
from app.services import appium_node_locking, device_locking
from app.services.auth_dependencies import require_admin
from app.services.desired_state_writer import write_desired_state

router = APIRouter(prefix="/api/admin/appium-nodes", tags=["admin"])


class ClearTransitionBody(BaseModel):
    reason: str | None = None


@router.post("/{node_id}/clear-transition", response_model=AppiumNodeRead)
async def clear_transition(
    node_id: uuid.UUID,
    body: ClearTransitionBody,
    db: AsyncSession = Depends(get_db),
    username: str = Depends(require_admin),
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

    await write_desired_state(
        db,
        node=locked_node,
        target=locked_node.desired_state if locked_node.desired_state != NodeState.error else NodeState.stopped,
        caller="admin_clear_transition",
        desired_port=locked_node.desired_port,
        actor=username,
        reason=body.reason,
    )
    await db.commit()
    await db.refresh(locked_node)
    return locked_node
