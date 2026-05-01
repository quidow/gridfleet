import asyncio
import contextlib
import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.exc import NoResultFound
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.errors import AgentCallError
from app.models.device import Device
from app.services import device_locking
from app.services.agent_operations import pack_device_lifecycle_action
from app.services.event_bus import event_bus
from app.services.maintenance_service import enter_maintenance, exit_maintenance
from app.services.node_manager import NodeManager, get_node_manager
from app.services.pack_platform_catalog import platform_has_lifecycle_action
from app.services.pack_platform_resolver import resolve_pack_platform

logger = logging.getLogger(__name__)

MAX_CONCURRENCY = 5


async def _load_devices(db: AsyncSession, device_ids: list[uuid.UUID]) -> list[Device]:
    from app.services import device_locking

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


ManagerCall = Callable[[NodeManager, AsyncSession, Device], Awaitable[object]]


async def _run_per_device_node_action(
    db: AsyncSession,
    device_ids: list[uuid.UUID],
    *,
    operation: str,
    manager_call: ManagerCall,
) -> dict[str, Any]:
    existing_device_ids = await _load_existing_device_ids(db, device_ids)
    session_factory = _session_factory_from_db(db)
    errors: dict[str, str] = {}
    sem = asyncio.Semaphore(MAX_CONCURRENCY)

    async def _one(device_id: uuid.UUID) -> None:
        async with sem, session_factory() as session:
            try:
                device = await device_locking.lock_device(session, device_id)
                manager = get_node_manager(device)
                await manager_call(manager, session, device)
                await session.commit()
            except NoResultFound:
                errors[str(device_id)] = "Device not found"
            except Exception as e:
                errors[str(device_id)] = str(e)
                with contextlib.suppress(Exception):
                    await session.rollback()

    await asyncio.gather(*[_one(did) for did in existing_device_ids])
    succeeded = len(existing_device_ids) - len(errors)
    await event_bus.publish(
        "bulk.operation_completed",
        {
            "operation": operation,
            "total": len(existing_device_ids),
            "succeeded": succeeded,
            "failed": len(errors),
        },
    )
    return _result(len(existing_device_ids), succeeded, errors)


async def bulk_start_nodes(db: AsyncSession, device_ids: list[uuid.UUID]) -> dict[str, Any]:
    return await _run_per_device_node_action(
        db,
        device_ids,
        operation="start_nodes",
        manager_call=lambda mgr, sess, dev: mgr.start_node(sess, dev),
    )


async def bulk_stop_nodes(db: AsyncSession, device_ids: list[uuid.UUID]) -> dict[str, Any]:
    return await _run_per_device_node_action(
        db,
        device_ids,
        operation="stop_nodes",
        manager_call=lambda mgr, sess, dev: mgr.stop_node(sess, dev),
    )


async def bulk_restart_nodes(db: AsyncSession, device_ids: list[uuid.UUID]) -> dict[str, Any]:
    return await _run_per_device_node_action(
        db,
        device_ids,
        operation="restart_nodes",
        manager_call=lambda mgr, sess, dev: mgr.restart_node(sess, dev),
    )


async def bulk_set_auto_manage(db: AsyncSession, device_ids: list[uuid.UUID], auto_manage: bool) -> dict[str, Any]:
    devices = await _load_devices(db, device_ids)
    for device in devices:
        device.auto_manage = auto_manage
    await db.commit()
    await event_bus.publish(
        "bulk.operation_completed",
        {
            "operation": "set_auto_manage",
            "total": len(devices),
            "succeeded": len(devices),
            "failed": 0,
        },
    )
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
    await db.commit()
    await event_bus.publish(
        "bulk.operation_completed",
        {
            "operation": "update_tags",
            "total": len(devices),
            "succeeded": len(devices),
            "failed": 0,
        },
    )
    return _result(len(devices), len(devices), {})


async def bulk_delete(db: AsyncSession, device_ids: list[uuid.UUID]) -> dict[str, Any]:
    from app.services.device_service import delete_device

    errors: dict[str, str] = {}
    for device_id in device_ids:
        try:
            deleted = await delete_device(db, device_id)
            if not deleted:
                errors[str(device_id)] = "Device not found"
        except Exception as e:
            errors[str(device_id)] = str(e)
    succeeded = len(device_ids) - len(errors)
    await event_bus.publish(
        "bulk.operation_completed",
        {
            "operation": "delete",
            "total": len(device_ids),
            "succeeded": succeeded,
            "failed": len(errors),
        },
    )
    return _result(len(device_ids), succeeded, errors)


async def bulk_enter_maintenance(db: AsyncSession, device_ids: list[uuid.UUID], drain: bool = False) -> dict[str, Any]:
    devices = await _load_devices(db, device_ids)
    ordered_ids = [device.id for device in devices]
    errors: dict[str, str] = {}
    for device_id in ordered_ids:
        try:
            device = await device_locking.lock_device(db, device_id)
            await enter_maintenance(db, device, drain=drain, commit=False)
        except Exception as e:
            errors[str(device_id)] = str(e)
    await db.commit()
    succeeded = len(ordered_ids) - len(errors)
    await event_bus.publish(
        "bulk.operation_completed",
        {
            "operation": "enter_maintenance",
            "total": len(ordered_ids),
            "succeeded": succeeded,
            "failed": len(errors),
        },
    )
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
    await event_bus.publish(
        "bulk.operation_completed",
        {
            "operation": "reconnect",
            "total": len(devices),
            "succeeded": succeeded,
            "failed": len(errors),
        },
    )
    return _result(len(devices), succeeded, errors)


async def bulk_exit_maintenance(db: AsyncSession, device_ids: list[uuid.UUID]) -> dict[str, Any]:
    devices = await _load_devices(db, device_ids)
    errors: dict[str, str] = {}
    for device in devices:
        try:
            await exit_maintenance(db, device, commit=False)
        except ValueError as e:
            errors[str(device.id)] = str(e)
        except Exception as e:
            errors[str(device.id)] = str(e)
    await db.commit()
    succeeded = len(devices) - len(errors)
    await event_bus.publish(
        "bulk.operation_completed",
        {
            "operation": "exit_maintenance",
            "total": len(devices),
            "succeeded": succeeded,
            "failed": len(errors),
        },
    )
    return _result(len(devices), succeeded, errors)
