from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.appium_nodes.services import resource_service
from app.core.dependencies import DbDep
from app.devices.models import Device
from app.devices.schemas.probe_targets import ProbeTargetOut, ProbeTargetsOut
from app.devices.services.state import maintenance_sql
from app.hosts.models import Host
from app.settings.dependencies import SettingsServicesDep

router = APIRouter(prefix="/agent/devices", tags=["agent-devices"])


@router.get("/probe-targets", response_model=ProbeTargetsOut)
async def probe_targets(
    db: DbDep,
    settings_services: SettingsServicesDep,
    host_id: Annotated[uuid.UUID, Query()],
) -> ProbeTargetsOut:
    if await db.get(Host, host_id) is None:
        raise HTTPException(status_code=404, detail="Unknown host_id")

    devices = (
        (
            await db.execute(
                select(Device)
                .where(Device.host_id == host_id)
                .where(~maintenance_sql())
                .options(selectinload(Device.appium_node))
                .order_by(Device.id)
            )
        )
        .scalars()
        .all()
    )
    node_ids = [device.appium_node.id for device in devices if device.appium_node is not None]
    claimed_ports_by_node = await resource_service.get_port_claims_for_nodes(db, node_ids=node_ids)
    settings = settings_services.service
    ip_ping_timeout_sec = settings.get_float("device_checks.ip_ping.timeout_sec")
    ip_ping_count = settings.get_int("device_checks.ip_ping.count_per_cycle")

    entries: list[ProbeTargetOut] = []
    for device in devices:
        connection_target = device.connection_target or device.identity_value
        if not connection_target:
            continue
        claimed_ports = claimed_ports_by_node.get(device.appium_node.id, {}) if device.appium_node else {}
        entries.append(
            ProbeTargetOut(
                device_id=device.id,
                connection_target=connection_target,
                identity_value=device.identity_value,
                pack_id=device.pack_id,
                platform_id=device.platform_id,
                device_type=device.device_type.value,
                connection_type=device.connection_type.value if device.connection_type else None,
                ip_address=device.ip_address,
                ip_ping_timeout_sec=ip_ping_timeout_sec,
                ip_ping_count=ip_ping_count,
                claimed_ports=claimed_ports,
            )
        )
    return ProbeTargetsOut(host_id=host_id, devices=entries)
