from __future__ import annotations

import asyncio
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, Any, cast

import httpx2 as httpx
from sqlalchemy import select
from sqlalchemy.exc import NoResultFound
from sqlalchemy.orm import selectinload

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

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.agent_comm.http_pool import AgentHttpPool
    from app.agent_comm.protocols import CircuitBreakerProtocol
    from app.appium_nodes.models import AppiumNode
    from app.appium_nodes.services.desired_state_writer import DesiredStateCaller
    from app.core.protocols import SettingsReader
    from app.core.type_defs import SessionFactory
    from app.devices.locking import LockedDevice
    from app.devices.protocols import DeviceCrudProtocol, MaintenanceProtocol, OperatorNodeLifecycleProtocol
    from app.events.catalog import EventSeverity
    from app.events.protocols import EventPublisher

    type LockedDeviceAction = Callable[[AsyncSession, LockedDevice, str], Awaitable[object]]

platform_has_lifecycle_action = pack_platform_catalog.platform_has_lifecycle_action
resolve_pack_platform = pack_platform_resolver.resolve_pack_platform

MAX_CONCURRENCY = 5


@dataclass(frozen=True, slots=True)
class BulkItemResult:
    """The outcome of one device's own transaction. ``error`` is None on success."""

    device_id: uuid.UUID
    error: str | None


@dataclass(frozen=True, slots=True)
class ReconnectTarget:
    """Everything one reconnect agent call needs, as immutable scalars.

    Nothing ORM-shaped crosses into the effect phase: the read session that
    produced these values is closed before the first agent call.
    """

    device_id: uuid.UUID
    host_ip: str
    agent_port: int
    connection_target: str
    pack_id: str
    platform_id: str
    ip_address: str


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


async def _load_existing_device_ids(session_factory: SessionFactory, device_ids: list[uuid.UUID]) -> list[uuid.UUID]:
    """Sort, deduplicate, and drop unknown ids on one short read session.

    Sorting is what keeps concurrent bulk callers from deadlocking against each
    other once every item takes its own ``FOR UPDATE``; deduplicating is what
    stops one input id from producing two items (and, before dedupe, a spurious
    "Device not found" from the second pass over an already-deleted row).
    """
    if not device_ids:
        return []
    ordered_ids = sorted(set(device_ids))
    async with session_factory() as db:
        result = await db.execute(select(Device.id).where(Device.id.in_(ordered_ids)).order_by(Device.id))
        return list(result.scalars().all())


def _result(total: int, succeeded: int, errors: dict[str, str]) -> dict[str, Any]:
    return {"total": total, "succeeded": succeeded, "failed": total - succeeded, "errors": errors}


async def _publish_summary(publisher: EventPublisher, operation: str, results: list[BulkItemResult]) -> dict[str, Any]:
    """One standalone summary transaction, after every per-item transaction has ended."""
    errors = {str(item.device_id): item.error for item in results if item.error is not None}
    total = len(results)
    succeeded = total - len(errors)
    data, severity = _completion_payload(operation, total, succeeded, len(errors))
    await publisher.publish("bulk.operation_completed", data, severity=severity)
    return _result(total, succeeded, errors)


async def _bulk_start_one(
    db: AsyncSession, locked: LockedDevice, caller: str, *, operator: OperatorNodeLifecycleProtocol
) -> AppiumNode:
    return await operator.request_start(
        db, locked.device, caller=cast("DesiredStateCaller", caller), reason=f"{caller} start requested"
    )


async def _bulk_stop_one(
    db: AsyncSession, locked: LockedDevice, caller: str, *, operator: OperatorNodeLifecycleProtocol
) -> AppiumNode:
    device = locked.device
    node: AppiumNode | None = device.appium_node
    if node is None or not node.observed_running:
        raise NodeManagerError(f"No running node for device {device.id}")
    return await operator.request_stop(db, device, reason=f"{caller} stop requested")


async def _bulk_restart_one(
    db: AsyncSession, locked: LockedDevice, caller: str, *, operator: OperatorNodeLifecycleProtocol
) -> AppiumNode:
    return await operator.request_restart(
        db, locked.device, caller=cast("DesiredStateCaller", caller), reason=f"{caller} restart requested"
    )


async def _run_per_device_action(
    session_factory: SessionFactory,
    device_ids: list[uuid.UUID],
    *,
    operation: str,
    action_fn: LockedDeviceAction,
    caller: str,
    publisher: EventPublisher,
) -> dict[str, Any]:
    """One fresh transaction per device, at most ``MAX_CONCURRENCY`` in flight.

    ``session_factory.begin()`` owns the unwind, so a failed item needs no manual
    rollback and cannot poison a peer or the summary.
    """
    existing_device_ids = await _load_existing_device_ids(session_factory, device_ids)
    sem = asyncio.Semaphore(MAX_CONCURRENCY)

    async def _one(device_id: uuid.UUID) -> BulkItemResult:
        try:
            async with sem, session_factory.begin() as db:
                locked = await device_locking.lock_device_handle(db, device_id)
                await action_fn(db, locked, caller)
        except NoResultFound:
            return BulkItemResult(device_id, "Device not found")
        except Exception as exc:  # noqa: BLE001 — per-device error accumulation; bulk ops must continue past one failure
            return BulkItemResult(device_id, str(exc))
        return BulkItemResult(device_id, None)

    results = await asyncio.gather(*[_one(device_id) for device_id in existing_device_ids])
    return await _publish_summary(publisher, operation, list(results))


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
        session_factory: SessionFactory,
        pool: AgentHttpPool | None = None,
    ) -> None:
        self._publisher = publisher
        self._settings = settings
        self._circuit_breaker = circuit_breaker
        self._maintenance = maintenance
        self._crud = crud
        self._operator = operator
        self._session_factory = session_factory
        self._pool = pool

    async def bulk_start_nodes(self, device_ids: list[uuid.UUID], *, caller: str = "bulk") -> dict[str, Any]:
        return await _run_per_device_action(
            self._session_factory,
            device_ids,
            operation="start_nodes",
            action_fn=partial(_bulk_start_one, operator=self._operator),
            caller=caller,
            publisher=self._publisher,
        )

    async def bulk_stop_nodes(self, device_ids: list[uuid.UUID], *, caller: str = "bulk") -> dict[str, Any]:
        return await _run_per_device_action(
            self._session_factory,
            device_ids,
            operation="stop_nodes",
            action_fn=partial(_bulk_stop_one, operator=self._operator),
            caller=caller,
            publisher=self._publisher,
        )

    async def bulk_restart_nodes(self, device_ids: list[uuid.UUID], *, caller: str = "bulk") -> dict[str, Any]:
        return await _run_per_device_action(
            self._session_factory,
            device_ids,
            operation="restart_nodes",
            action_fn=partial(_bulk_restart_one, operator=self._operator),
            caller=caller,
            publisher=self._publisher,
        )

    async def bulk_enter_maintenance(self, device_ids: list[uuid.UUID]) -> dict[str, Any]:
        return await _run_per_device_action(
            self._session_factory,
            device_ids,
            operation="enter_maintenance",
            action_fn=self._enter_maintenance_one,
            caller="bulk",
            publisher=self._publisher,
        )

    async def _enter_maintenance_one(self, db: AsyncSession, locked: LockedDevice, _caller: str) -> None:
        await self._maintenance.enter_maintenance_locked(db, locked)

    async def bulk_delete(self, device_ids: list[uuid.UUID]) -> dict[str, Any]:
        # One transaction per device: a shared transaction would let one failure
        # abort every other device's delete. ``delete_device_txn`` takes the Device
        # lock itself, so there is no pre-lock here.
        existing_device_ids = await _load_existing_device_ids(self._session_factory, device_ids)
        sem = asyncio.Semaphore(MAX_CONCURRENCY)

        async def _one(device_id: uuid.UUID) -> BulkItemResult:
            try:
                async with sem, self._session_factory.begin() as db:
                    deleted = await self._crud.delete_device_txn(db, device_id)
            except NoResultFound:
                return BulkItemResult(device_id, "Device not found")
            except Exception as exc:  # noqa: BLE001 — per-device error accumulation; bulk delete must continue past one failure
                return BulkItemResult(device_id, str(exc))
            # The pre-filter above already dropped unknown ids, so a False here is
            # a lost race against a concurrent delete.
            return BulkItemResult(device_id, None if deleted else "Device not found")

        results = await asyncio.gather(*[_one(device_id) for device_id in existing_device_ids])
        return await _publish_summary(self._publisher, "delete", list(results))

    async def bulk_exit_maintenance(self, device_ids: list[uuid.UUID]) -> dict[str, Any]:
        existing_device_ids = await _load_existing_device_ids(self._session_factory, device_ids)
        sem = asyncio.Semaphore(MAX_CONCURRENCY)

        async def _one(device_id: uuid.UUID) -> BulkItemResult:
            try:
                async with sem, self._session_factory.begin() as db:
                    locked = await device_locking.lock_device_handle(db, device_id)
                    recovery = await self._maintenance.exit_maintenance_locked(db, locked)
            except NoResultFound:
                return BulkItemResult(device_id, "Device not found")
            except Exception as exc:  # noqa: BLE001 — per-device error accumulation; bulk exit_maintenance must continue past one failure
                return BulkItemResult(device_id, str(exc))
            if recovery is not None:
                # After this device's own transaction committed: create_job owns its
                # commit, so it must not run inside the state mutation. No guard
                # here — schedule_device_recovery is contractually best-effort and
                # swallows its own failures.
                await self._maintenance.schedule_device_recovery(recovery.device_id)
            return BulkItemResult(device_id, None)

        results = await asyncio.gather(*[_one(device_id) for device_id in existing_device_ids])
        return await _publish_summary(self._publisher, "exit_maintenance", list(results))

    async def bulk_reconnect(self, device_ids: list[uuid.UUID]) -> dict[str, Any]:
        """Reconnect network-connected ADB devices.

        Two phases: one short read session resolves eligibility into immutable
        ``ReconnectTarget`` scalars, then the agent calls run with no session and
        no row lock held. The read takes no ``FOR UPDATE`` — the effect is remote
        and there is no DB write to protect.

        Takes no ``caller``: unlike the node actions, a reconnect writes no
        desired state, so there is nothing for the caller label to reach.
        """
        targets, results = await self._load_reconnect_targets(device_ids)
        sem = asyncio.Semaphore(MAX_CONCURRENCY)

        async def _reconnect_one(target: ReconnectTarget) -> BulkItemResult:
            async with sem:
                try:
                    data = await pack_device_lifecycle_action(
                        target.host_ip,
                        target.agent_port,
                        target.connection_target,
                        pack_id=target.pack_id,
                        platform_id=target.platform_id,
                        action="reconnect",
                        args={"ip_address": target.ip_address, "port": 5555},
                        http_client_factory=httpx.AsyncClient,
                        circuit_breaker=self._circuit_breaker,
                        pool=self._pool,
                    )
                except AgentCallError as exc:
                    return BulkItemResult(target.device_id, str(exc))
            if not data.get("success"):
                return BulkItemResult(target.device_id, "Reconnect failed")
            return BulkItemResult(target.device_id, None)

        effects = await asyncio.gather(*[_reconnect_one(target) for target in targets])
        return await _publish_summary(self._publisher, "reconnect", [*results, *effects])

    async def _load_reconnect_targets(
        self, device_ids: list[uuid.UUID]
    ) -> tuple[list[ReconnectTarget], list[BulkItemResult]]:
        """Resolve eligibility on one read session and copy out scalars only."""
        if not device_ids:
            return [], []
        targets: list[ReconnectTarget] = []
        ineligible: list[BulkItemResult] = []
        lifecycle_cache: dict[tuple[str, str], list[dict[str, Any]]] = {}
        async with self._session_factory() as db:
            devices = (
                (
                    await db.execute(
                        select(Device)
                        .where(Device.id.in_(sorted(set(device_ids))))
                        .options(selectinload(Device.host))
                        .order_by(Device.id)
                    )
                )
                .scalars()
                .all()
            )
            for device in devices:
                if not await _supports_reconnect(db, device, lifecycle_cache):
                    ineligible.append(BulkItemResult(device.id, "Not a network-connected Android device"))
                    continue
                if not (
                    device.connection_type
                    and device.connection_type.value == "network"
                    and device.ip_address
                    and device.host
                ):
                    ineligible.append(BulkItemResult(device.id, "Not a network-connected Android device"))
                    continue
                # Mirrors the assert the effect phase used to carry: no eligibility
                # rule covers connection_target, so a NULL here is a data bug and
                # stays a 500 — it just surfaces before any agent call now.
                assert device.connection_target is not None
                targets.append(
                    ReconnectTarget(
                        device_id=device.id,
                        host_ip=device.host.ip,
                        agent_port=device.host.agent_port,
                        connection_target=device.connection_target,
                        pack_id=device.pack_id,
                        platform_id=device.platform_id,
                        ip_address=device.ip_address,
                    )
                )
        return targets, ineligible


async def _supports_reconnect(
    db: AsyncSession, device: Device, lifecycle_cache: dict[tuple[str, str], list[dict[str, Any]]]
) -> bool:
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
