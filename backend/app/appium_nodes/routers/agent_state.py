from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Annotated

from fastapi import APIRouter, Query
from sqlalchemy import select
from sqlalchemy.orm import joinedload

from app.appium_nodes.exceptions import NodeManagerError
from app.appium_nodes.models import AppiumDesiredState, AppiumNode
from app.appium_nodes.schemas import NodeDesiredSpecOut, NodesDesiredOut
from app.appium_nodes.services import resource_service
from app.appium_nodes.services.reconciler_agent import build_node_launch_payload
from app.core.dependencies import DbDep
from app.devices.models import Device
from app.settings.dependencies import SettingsServicesDep

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.core.protocols import SettingsReader

router = APIRouter(prefix="/agent/appium-nodes", tags=["agent-appium-nodes"])


async def _get_desired(
    db: AsyncSession,
    host_id: uuid.UUID,
    *,
    settings: SettingsReader,
) -> NodesDesiredOut:
    rows = (
        (
            await db.execute(
                select(AppiumNode)
                .join(Device)
                .where(Device.host_id == host_id)
                .options(
                    joinedload(AppiumNode.device).joinedload(Device.host),
                    joinedload(AppiumNode.device).joinedload(Device.appium_node),
                )
                .order_by(AppiumNode.device_id)
            )
        )
        .scalars()
        .all()
    )
    specs: list[NodeDesiredSpecOut] = []
    for node in rows:
        launch = None
        unrunnable_reason = None
        if node.desired_state == AppiumDesiredState.running:
            allocated = await resource_service.get_capabilities(db, node_id=node.id)
            try:
                launch = await build_node_launch_payload(
                    db,
                    node.device,
                    port=node.desired_port or node.port,
                    allocated_caps=allocated or None,
                    settings=settings,
                )
            except (LookupError, NodeManagerError) as exc:
                unrunnable_reason = str(exc)
        specs.append(
            NodeDesiredSpecOut(
                device_id=node.device_id,
                generation=node.generation,
                desired_state=node.desired_state,
                port=node.desired_port or node.port,
                accepting_new_sessions=node.accepting_new_sessions,
                stop_pending=node.stop_pending,
                grid_run_id=node.desired_grid_run_id,
                transition_token=node.transition_token,
                transition_deadline=node.transition_deadline,
                launch=launch,
                unrunnable_reason=unrunnable_reason,
            )
        )
    return NodesDesiredOut(nodes=specs, generation_hint=max((node.generation for node in rows), default=0))


@router.get("/desired", response_model=NodesDesiredOut)
async def desired(
    db: DbDep,
    settings_services: SettingsServicesDep,
    host_id: Annotated[uuid.UUID, Query()],
) -> NodesDesiredOut:
    return await _get_desired(db, host_id, settings=settings_services.service)
