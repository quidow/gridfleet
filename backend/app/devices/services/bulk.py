import asyncio
import contextlib
import logging
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.exc import NoResultFound
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent_comm.operations import pack_device_lifecycle_action
from app.appium_nodes.exceptions import NodeManagerError
from app.appium_nodes.models import AppiumNode
from app.appium_nodes.services.reconciler_allocation import candidate_ports
from app.core.errors import AgentCallError
from app.devices import locking as device_locking
from app.devices.models import Device
from app.devices.services.intent import register_intents_and_reconcile, revoke_intents_and_reconcile
from app.devices.services.intent_types import (
    GRID_ROUTING,
    NODE_PROCESS,
    PRIORITY_AUTO_RECOVERY,
    PRIORITY_OPERATOR_STOP,
    IntentRegistration,
)
from app.devices.services.maintenance import enter_maintenance, exit_maintenance, schedule_device_recovery
from app.devices.services.service import delete_device
from app.events import event_bus, queue_event_for_session
from app.events.catalog import EventSeverity
from app.packs.services import platform_catalog as pack_platform_catalog
from app.packs.services import platform_resolver as pack_platform_resolver
from app.settings import settings_service

platform_has_lifecycle_action = pack_platform_catalog.platform_has_lifecycle_action
resolve_pack_platform = pack_platform_resolver.resolve_pack_platform

logger = logging.getLogger(__name__)

MAX_CONCURRENCY = 5


def _bulk_severity(total: int, succeeded: int, failed: int) -> EventSeverity:
    if failed == 0:
        return "success"
    if succeeded == 0:
        return "critical"
    return "warning"


async def _load_devices(db: AsyncSession, device_ids: list[uuid.UUID]) -> list[Device]:
    return await device_locking.lock_devices(db, device_ids)


async def _load_existing_device_ids(db: AsyncSession, device_ids: list[uuid.UUID]) -> list[uuid.UUID]:
    if not device_ids:
        return []
    ordered_ids = sorted(set(device_ids))
    result = await db.execute(select(Device.id).where(Device.id.in_(ordered_ids)).order_by(Device.id))
    return list(result.scalars().all())


def _session_factory_from_db(db: AsyncSession) -> async_sessionmaker[AsyncSession]:
    if db.bind is None:
        raise RuntimeError("Bulk node action session is not bound")
    return async_sessionmaker(db.bind, class_=AsyncSession, expire_on_commit=False)


def _result(total: int, succeeded: int, errors: dict[str, str]) -> dict[str, Any]:
    return {"total": total, "succeeded": succeeded, "failed": total - succeeded, "errors": errors}


def _operator_stop_sources(device_id: uuid.UUID) -> list[str]:
    return [f"operator:stop:node:{device_id}", f"operator:stop:grid:{device_id}"]


def _operator_start_source(device_id: uuid.UUID) -> str:
    return f"operator:start:{device_id}"


def _operator_start_intent(device: Device, desired_port: int) -> IntentRegistration:
    return IntentRegistration(
        source=_operator_start_source(device.id),
        axis=NODE_PROCESS,
        payload={"action": "start", "priority": PRIORITY_AUTO_RECOVERY, "desired_port": desired_port},
    )


def _operator_restart_intent(device: Device, desired_port: int) -> IntentRegistration:
    window_sec = int(settings_service.get("appium_reconciler.restart_window_sec"))
    deadline = datetime.now(UTC) + timedelta(seconds=window_sec)
    return IntentRegistration(
        source=_operator_start_source(device.id),
        axis=NODE_PROCESS,
        payload={
            "action": "start",
            "priority": PRIORITY_AUTO_RECOVERY,
            "desired_port": desired_port,
            "transition_token": str(uuid.uuid4()),
            "transition_deadline": deadline.isoformat(),
        },
    )


def _operator_stop_intents(device_id: uuid.UUID) -> list[IntentRegistration]:
    return [
        IntentRegistration(
            source=f"operator:stop:node:{device_id}",
            axis=NODE_PROCESS,
            payload={"action": "stop", "priority": PRIORITY_OPERATOR_STOP, "stop_mode": "hard"},
        ),
        IntentRegistration(
            source=f"operator:stop:grid:{device_id}",
            axis=GRID_ROUTING,
            payload={"accepting_new_sessions": False, "priority": PRIORITY_OPERATOR_STOP},
        ),
    ]


async def _bulk_start_one(db: AsyncSession, device: Device, caller: str) -> AppiumNode:
    if device.host_id is None:
        raise NodeManagerError(f"Device {device.id} has no host assigned")
    desired_port = (await candidate_ports(db, host_id=device.host_id))[0]
    node: AppiumNode | None = device.appium_node
    if node is None:
        node = AppiumNode(
            device_id=device.id,
            port=desired_port,
            grid_url=settings_service.get("grid.hub_url"),
        )
        db.add(node)
        await db.flush()
        device.appium_node = node
    await revoke_intents_and_reconcile(
        db,
        device_id=device.id,
        sources=_operator_stop_sources(device.id),
        reason=f"{caller} start requested",
    )
    await register_intents_and_reconcile(
        db,
        device_id=device.id,
        intents=[_operator_start_intent(device, desired_port)],
        reason=f"{caller} start requested",
    )
    await db.commit()
    await db.refresh(node)
    return node


async def _bulk_stop_one(db: AsyncSession, device: Device, caller: str) -> AppiumNode:
    node: AppiumNode | None = device.appium_node
    if node is None or not node.observed_running:
        raise NodeManagerError(f"No running node for device {device.id}")
    await register_intents_and_reconcile(
        db,
        device_id=device.id,
        intents=_operator_stop_intents(device.id),
        reason=f"{caller} stop requested",
    )
    await db.commit()
    await db.refresh(node)
    return node


async def _bulk_restart_one(db: AsyncSession, device: Device, caller: str) -> AppiumNode:
    node: AppiumNode | None = device.appium_node
    if node is None or not node.observed_running:
        return await _bulk_start_one(db, device, caller)
    await register_intents_and_reconcile(
        db,
        device_id=device.id,
        intents=[_operator_restart_intent(device, node.port)],
        reason=f"{caller} restart requested",
    )
    await db.commit()
    await db.refresh(node)
    return node


async def _run_per_device_node_action(
    db: AsyncSession,
    device_ids: list[uuid.UUID],
    *,
    operation: str,
    action_fn: Callable[..., Awaitable[object]],
    caller: str,
) -> dict[str, Any]:
    existing_device_ids = await _load_existing_device_ids(db, device_ids)
    session_factory = _session_factory_from_db(db)
    errors: dict[str, str] = {}
    sem = asyncio.Semaphore(MAX_CONCURRENCY)

    async def _one(device_id: uuid.UUID) -> None:
        async with sem, session_factory() as session:
            try:
                device = await device_locking.lock_device(session, device_id)
                await action_fn(session, device, caller)
                await session.commit()
            except NoResultFound:
                errors[str(device_id)] = "Device not found"
            except Exception as e:  # noqa: BLE001 — per-device error accumulation; bulk ops must continue past one failure
                errors[str(device_id)] = str(e)
                with contextlib.suppress(Exception):  # best-effort rollback cleanup
                    await session.rollback()

    await asyncio.gather(*[_one(did) for did in existing_device_ids])
    succeeded = len(existing_device_ids) - len(errors)
    total = len(existing_device_ids)
    failed = len(errors)
    await event_bus.publish(
        "bulk.operation_completed",
        {
            "operation": operation,
            "total": total,
            "succeeded": succeeded,
            "failed": failed,
        },
        severity=_bulk_severity(total, succeeded, failed),
    )
    return _result(len(existing_device_ids), succeeded, errors)


async def bulk_start_nodes(
    db: AsyncSession,
    device_ids: list[uuid.UUID],
    *,
    caller: str = "bulk",
) -> dict[str, Any]:
    return await _run_per_device_node_action(
        db,
        device_ids,
        operation="start_nodes",
        action_fn=_bulk_start_one,
        caller=caller,
    )


async def bulk_stop_nodes(
    db: AsyncSession,
    device_ids: list[uuid.UUID],
    *,
    caller: str = "bulk",
) -> dict[str, Any]:
    return await _run_per_device_node_action(
        db,
        device_ids,
        operation="stop_nodes",
        action_fn=_bulk_stop_one,
        caller=caller,
    )


async def bulk_restart_nodes(
    db: AsyncSession,
    device_ids: list[uuid.UUID],
    *,
    caller: str = "bulk",
) -> dict[str, Any]:
    return await _run_per_device_node_action(
        db,
        device_ids,
        operation="restart_nodes",
        action_fn=_bulk_restart_one,
        caller=caller,
    )


async def bulk_set_auto_manage(db: AsyncSession, device_ids: list[uuid.UUID], auto_manage: bool) -> dict[str, Any]:
    devices = await _load_devices(db, device_ids)
    for device in devices:
        device.auto_manage = auto_manage
    queue_event_for_session(
        db,
        "bulk.operation_completed",
        {
            "operation": "set_auto_manage",
            "total": len(devices),
            "succeeded": len(devices),
            "failed": 0,
        },
        severity=_bulk_severity(len(devices), len(devices), 0),
    )
    await db.commit()
    return _result(len(devices), len(devices), {})


async def bulk_update_tags(
    db: AsyncSession, device_ids: list[uuid.UUID], tags: dict[str, str], merge: bool = True
) -> dict[str, Any]:
    devices = await _load_devices(db, device_ids)
    for device in devices:
        if merge:
            merged = {**(device.tags or {}), **tags}
            device.tags = merged
        else:
            device.tags = tags
    queue_event_for_session(
        db,
        "bulk.operation_completed",
        {
            "operation": "update_tags",
            "total": len(devices),
            "succeeded": len(devices),
            "failed": 0,
        },
        severity=_bulk_severity(len(devices), len(devices), 0),
    )
    await db.commit()
    return _result(len(devices), len(devices), {})


async def bulk_delete(db: AsyncSession, device_ids: list[uuid.UUID]) -> dict[str, Any]:
    errors: dict[str, str] = {}
    for device_id in device_ids:
        try:
            deleted = await delete_device(db, device_id)
            if not deleted:
                errors[str(device_id)] = "Device not found"
        except Exception as e:  # noqa: BLE001 — per-device error accumulation; bulk delete must continue past one failure
            errors[str(device_id)] = str(e)
    succeeded = len(device_ids) - len(errors)
    total = len(device_ids)
    failed = len(errors)
    await event_bus.publish(
        "bulk.operation_completed",
        {
            "operation": "delete",
            "total": total,
            "succeeded": succeeded,
            "failed": failed,
        },
        severity=_bulk_severity(total, succeeded, failed),
    )
    return _result(len(device_ids), succeeded, errors)


async def bulk_enter_maintenance(db: AsyncSession, device_ids: list[uuid.UUID]) -> dict[str, Any]:
    devices = await _load_devices(db, device_ids)
    ordered_ids = [device.id for device in devices]
    errors: dict[str, str] = {}
    for device_id in ordered_ids:
        try:
            device = await device_locking.lock_device(db, device_id)
            await enter_maintenance(db, device, commit=False)
        except Exception as e:  # noqa: BLE001 — per-device error accumulation; bulk enter_maintenance must continue past one failure
            errors[str(device_id)] = str(e)
    succeeded = len(ordered_ids) - len(errors)
    failed = len(errors)
    total = len(ordered_ids)
    queue_event_for_session(
        db,
        "bulk.operation_completed",
        {
            "operation": "enter_maintenance",
            "total": total,
            "succeeded": succeeded,
            "failed": failed,
        },
        severity=_bulk_severity(total, succeeded, failed),
    )
    await db.commit()
    return _result(len(ordered_ids), succeeded, errors)


async def bulk_reconnect(db: AsyncSession, device_ids: list[uuid.UUID]) -> dict[str, Any]:
    """Reconnect network-connected ADB devices."""
    devices = await _load_devices(db, device_ids)
    errors: dict[str, str] = {}
    sem = asyncio.Semaphore(MAX_CONCURRENCY)

    # Filter to eligible devices
    lifecycle_cache: dict[tuple[str, str], list[dict]] = {}  # type: ignore[type-arg]

    async def _supports_reconnect(device: Device) -> bool:
        key = (device.pack_id, device.platform_id)
        if key not in lifecycle_cache:
            try:
                resolved = await resolve_pack_platform(
                    db,
                    pack_id=device.pack_id,
                    platform_id=device.platform_id,
                    device_type=device.device_type.value if device.device_type else None,
                )
                lifecycle_cache[key] = resolved.lifecycle_actions
            except LookupError:
                lifecycle_cache[key] = []
        return platform_has_lifecycle_action(lifecycle_cache[key], "reconnect")

    eligible = []
    for d in devices:
        if (
            await _supports_reconnect(d)
            and d.connection_type
            and d.connection_type.value == "network"
            and d.ip_address
            and d.host
        ):
            eligible.append(d)
    for d in devices:
        if d not in eligible:
            errors[str(d.id)] = "Not a network-connected Android device"

    async def _reconnect_one(device: Device) -> None:
        async with sem:
            host = device.host
            assert host is not None  # guaranteed by filter
            assert device.connection_target is not None  # guaranteed by filter
            assert device.ip_address is not None  # guaranteed by filter
            try:
                data = await pack_device_lifecycle_action(
                    host.ip,
                    host.agent_port,
                    device.connection_target,
                    pack_id=device.pack_id,
                    platform_id=device.platform_id,
                    action="reconnect",
                    args={"ip_address": device.ip_address, "port": 5555},
                    http_client_factory=httpx.AsyncClient,
                )
                if not data.get("success"):
                    errors[str(device.id)] = "Reconnect failed"
            except AgentCallError as e:
                errors[str(device.id)] = str(e)

    await asyncio.gather(*[_reconnect_one(d) for d in eligible])
    succeeded = len(devices) - len(errors)
    total = len(devices)
    failed = len(errors)
    await event_bus.publish(
        "bulk.operation_completed",
        {
            "operation": "reconnect",
            "total": total,
            "succeeded": succeeded,
            "failed": failed,
        },
        severity=_bulk_severity(total, succeeded, failed),
    )
    return _result(len(devices), succeeded, errors)


async def bulk_exit_maintenance(db: AsyncSession, device_ids: list[uuid.UUID]) -> dict[str, Any]:
    devices = await _load_devices(db, device_ids)
    errors: dict[str, str] = {}
    successful: list[uuid.UUID] = []
    for device in devices:
        try:
            await exit_maintenance(db, device, commit=False)
            successful.append(device.id)
        except ValueError as e:
            errors[str(device.id)] = str(e)
        except Exception as e:  # noqa: BLE001 — per-device error accumulation; bulk exit_maintenance must continue past one failure
            errors[str(device.id)] = str(e)
    succeeded = len(devices) - len(errors)
    failed = len(errors)
    total = len(devices)
    queue_event_for_session(
        db,
        "bulk.operation_completed",
        {
            "operation": "exit_maintenance",
            "total": total,
            "succeeded": succeeded,
            "failed": failed,
        },
        severity=_bulk_severity(total, succeeded, failed),
    )
    await db.commit()

    # Enqueue recovery jobs after the bulk transaction commits to avoid
    # create_job committing mid-loop (which could leave a device stranded
    # with state mutations flushed but no recovery job if create_job raises).
    for device_id in successful:
        try:
            await schedule_device_recovery(db, device_id)
        except Exception as exc:  # noqa: BLE001 — best-effort recovery scheduling; device_connectivity_loop is the fallback
            logger.warning("bulk_exit_maintenance: failed to enqueue recovery for %s: %s", device_id, exc)

    return _result(len(devices), succeeded, errors)
