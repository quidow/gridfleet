from __future__ import annotations

import asyncio
import contextlib
import logging
from functools import partial
from typing import TYPE_CHECKING, Any, cast

import httpx2 as httpx
from sqlalchemy import select
from sqlalchemy.exc import NoResultFound
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent_comm.operations import pack_device_lifecycle_action
from app.appium_nodes.exceptions import NodeManagerError
from app.core.errors import AgentCallError
from app.devices import locking as device_locking
from app.devices.models import Device
from app.packs.services import platform_catalog as pack_platform_catalog
from app.packs.services import platform_resolver as pack_platform_resolver

if TYPE_CHECKING:
    import uuid
    from collections.abc import Awaitable, Callable

    from app.agent_comm.http_pool import AgentHttpPool
    from app.agent_comm.protocols import CircuitBreakerProtocol
    from app.appium_nodes.models import AppiumNode
    from app.appium_nodes.services.desired_state_writer import DesiredStateCaller
    from app.core.protocols import SettingsReader
    from app.devices.protocols import DeviceCrudProtocol, MaintenanceProtocol, OperatorNodeLifecycleProtocol
    from app.events.catalog import EventSeverity
    from app.events.protocols import EventPublisher

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


def _completion_payload(
    operation: str, total: int, succeeded: int, failed: int
) -> tuple[dict[str, Any], EventSeverity]:
    return (
        {"operation": operation, "total": total, "succeeded": succeeded, "failed": failed},
        _bulk_severity(total, succeeded, failed),
    )


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


async def _bulk_start_one(
    db: AsyncSession, device: Device, caller: str, *, operator: OperatorNodeLifecycleProtocol
) -> AppiumNode:
    return await operator.request_start(
        db, device, caller=cast("DesiredStateCaller", caller), reason=f"{caller} start requested"
    )


async def _bulk_stop_one(
    db: AsyncSession, device: Device, caller: str, *, operator: OperatorNodeLifecycleProtocol
) -> AppiumNode:
    node: AppiumNode | None = device.appium_node
    if node is None or not node.observed_running:
        raise NodeManagerError(f"No running node for device {device.id}")
    return await operator.request_stop(db, device, reason=f"{caller} stop requested")


async def _bulk_restart_one(
    db: AsyncSession, device: Device, caller: str, *, operator: OperatorNodeLifecycleProtocol
) -> AppiumNode:
    return await operator.request_restart(
        db, device, caller=cast("DesiredStateCaller", caller), reason=f"{caller} restart requested"
    )


async def _run_per_device_node_action(
    db: AsyncSession,
    device_ids: list[uuid.UUID],
    *,
    operation: str,
    action_fn: Callable[..., Awaitable[object]],
    caller: str,
    publisher: EventPublisher,
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
    data, severity = _completion_payload(operation, total, succeeded, failed)
    await publisher.publish("bulk.operation_completed", data, severity=severity)
    return _result(len(existing_device_ids), succeeded, errors)


class BulkOperationsService:
    def __init__(
        self,
        *,
        publisher: EventPublisher,
        settings: SettingsReader,
        circuit_breaker: CircuitBreakerProtocol,
        maintenance: MaintenanceProtocol,
        crud: DeviceCrudProtocol,
        operator: OperatorNodeLifecycleProtocol,
        pool: AgentHttpPool | None = None,
    ) -> None:
        self._publisher = publisher
        self._settings = settings
        self._circuit_breaker = circuit_breaker
        self._maintenance = maintenance
        self._crud = crud
        self._operator = operator
        self._pool = pool

    async def bulk_start_nodes(
        self, db: AsyncSession, device_ids: list[uuid.UUID], *, caller: str = "bulk"
    ) -> dict[str, Any]:
        return await _run_per_device_node_action(
            db,
            device_ids,
            operation="start_nodes",
            action_fn=partial(_bulk_start_one, operator=self._operator),
            caller=caller,
            publisher=self._publisher,
        )

    async def bulk_stop_nodes(
        self, db: AsyncSession, device_ids: list[uuid.UUID], *, caller: str = "bulk"
    ) -> dict[str, Any]:
        return await _run_per_device_node_action(
            db,
            device_ids,
            operation="stop_nodes",
            action_fn=partial(_bulk_stop_one, operator=self._operator),
            caller=caller,
            publisher=self._publisher,
        )

    async def bulk_restart_nodes(
        self, db: AsyncSession, device_ids: list[uuid.UUID], *, caller: str = "bulk"
    ) -> dict[str, Any]:
        return await _run_per_device_node_action(
            db,
            device_ids,
            operation="restart_nodes",
            action_fn=partial(_bulk_restart_one, operator=self._operator),
            caller=caller,
            publisher=self._publisher,
        )

    async def bulk_update_tags(
        self, db: AsyncSession, device_ids: list[uuid.UUID], tags: dict[str, str], merge: bool = True
    ) -> dict[str, Any]:
        devices = await _load_devices(db, device_ids)
        for device in devices:
            if merge:
                merged = {**(device.tags or {}), **tags}
                device.tags = merged
            else:
                device.tags = tags
        data, severity = _completion_payload("update_tags", len(devices), len(devices), 0)
        self._publisher.queue_for_session(db, "bulk.operation_completed", data, severity=severity)
        await db.commit()
        return _result(len(devices), len(devices), {})

    async def bulk_delete(self, db: AsyncSession, device_ids: list[uuid.UUID]) -> dict[str, Any]:
        errors: dict[str, str] = {}
        for device_id in device_ids:
            try:
                deleted = await self._crud.delete_device(db, device_id)
                if not deleted:
                    errors[str(device_id)] = "Device not found"
            except Exception as e:  # noqa: BLE001 — per-device error accumulation; bulk delete must continue past one failure
                errors[str(device_id)] = str(e)
        succeeded = len(device_ids) - len(errors)
        total = len(device_ids)
        failed = len(errors)
        data, severity = _completion_payload("delete", total, succeeded, failed)
        await self._publisher.publish("bulk.operation_completed", data, severity=severity)
        return _result(len(device_ids), succeeded, errors)

    async def bulk_enter_maintenance(self, db: AsyncSession, device_ids: list[uuid.UUID]) -> dict[str, Any]:
        devices = await _load_devices(db, device_ids)
        ordered_ids = [device.id for device in devices]
        errors: dict[str, str] = {}
        for device_id in ordered_ids:
            try:
                device = await device_locking.lock_device(db, device_id)
                await self._maintenance.enter_maintenance(db, device, commit=False)
            except Exception as e:  # noqa: BLE001 — per-device error accumulation; bulk enter_maintenance must continue past one failure
                errors[str(device_id)] = str(e)
        succeeded = len(ordered_ids) - len(errors)
        failed = len(errors)
        total = len(ordered_ids)
        data, severity = _completion_payload("enter_maintenance", total, succeeded, failed)
        self._publisher.queue_for_session(db, "bulk.operation_completed", data, severity=severity)
        await db.commit()
        return _result(len(ordered_ids), succeeded, errors)

    async def bulk_exit_maintenance(self, db: AsyncSession, device_ids: list[uuid.UUID]) -> dict[str, Any]:
        devices = await _load_devices(db, device_ids)
        errors: dict[str, str] = {}
        successful: list[uuid.UUID] = []
        for device in devices:
            try:
                await self._maintenance.exit_maintenance(db, device, commit=False)
                successful.append(device.id)
            except ValueError as e:
                errors[str(device.id)] = str(e)
            except Exception as e:  # noqa: BLE001 — per-device error accumulation; bulk exit_maintenance must continue past one failure
                errors[str(device.id)] = str(e)
        succeeded = len(devices) - len(errors)
        failed = len(errors)
        total = len(devices)
        data, severity = _completion_payload("exit_maintenance", total, succeeded, failed)
        self._publisher.queue_for_session(db, "bulk.operation_completed", data, severity=severity)
        await db.commit()

        # Enqueue recovery jobs after the bulk transaction commits to avoid
        # create_job committing mid-loop (which could leave a device stranded
        # with state mutations flushed but no recovery job if create_job raises).
        for device_id in successful:
            try:
                await self._maintenance.schedule_device_recovery(db, device_id)
            except Exception as exc:  # noqa: BLE001 — best-effort recovery scheduling; device_connectivity_loop is the fallback
                logger.warning("bulk_exit_maintenance: failed to enqueue recovery for %s: %s", device_id, exc)

        return _result(len(devices), succeeded, errors)

    async def bulk_reconnect(
        self, db: AsyncSession, device_ids: list[uuid.UUID], *, caller: str = "bulk"
    ) -> dict[str, Any]:
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

        eligible = [
            d
            for d in devices
            if (
                await _supports_reconnect(d)
                and d.connection_type
                and d.connection_type.value == "network"
                and d.ip_address
                and d.host
            )
        ]
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
                        settings=self._settings,
                        circuit_breaker=self._circuit_breaker,
                        pool=self._pool,
                    )
                    if not data.get("success"):
                        errors[str(device.id)] = "Reconnect failed"
                except AgentCallError as e:
                    errors[str(device.id)] = str(e)

        await asyncio.gather(*[_reconnect_one(d) for d in eligible])
        succeeded = len(devices) - len(errors)
        total = len(devices)
        failed = len(errors)
        data, severity = _completion_payload("reconnect", total, succeeded, failed)
        await self._publisher.publish("bulk.operation_completed", data, severity=severity)
        return _result(len(devices), succeeded, errors)
