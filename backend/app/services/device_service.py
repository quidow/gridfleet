import logging
import uuid
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import Select, asc, case, desc, func, or_, select
from sqlalchemy.exc import IntegrityError, NoResultFound
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.appium_node import AppiumDesiredState, AppiumNode
from app.models.device import (
    ConnectionType,
    Device,
    DeviceHold,
    DeviceOperationalState,
    DeviceType,
    HardwareHealthStatus,
)
from app.models.host import Host
from app.observability import sanitize_log_value
from app.schemas.device import (
    DevicePatch,
    DeviceVerificationCreate,
    DeviceVerificationUpdate,
    HardwareTelemetryState,
)
from app.schemas.device_filters import ChipStatus, DeviceQueryFilters
from app.services import (
    control_plane_state_store,
    device_attention,
    device_health,
    device_locking,
    device_readiness,
    device_write,
    hardware_telemetry,
    lifecycle_policy,
    run_service,
)
from app.services.desired_state_writer import DesiredStateCaller, write_desired_state
from app.services.device_connectivity import CONNECTIVITY_NAMESPACE, IP_PING_NAMESPACE
from app.services.device_identity_conflicts import (
    ensure_device_payload_identity_available,
)
from app.services.node_service_types import NodeManagerError

logger = logging.getLogger(__name__)
DeviceListStatement = Select[tuple[Device]]
DeviceCountStatement = Select[tuple[int]]
DeviceQueryStatement = DeviceListStatement | DeviceCountStatement


async def prepare_device_create_payload(
    db: AsyncSession,
    data: DeviceVerificationCreate,
) -> dict[str, Any]:
    return await device_write.prepare_device_create_payload_async(db, data)


async def prepare_device_update_payload(
    db: AsyncSession,
    device: Device,
    data: DeviceVerificationUpdate | DevicePatch,
) -> dict[str, Any]:
    return await device_write.prepare_device_update_payload_async(db, device, data)


async def create_device(
    db: AsyncSession,
    data: DeviceVerificationCreate,
    *,
    mark_verified: bool = False,
    initial_operational_state: DeviceOperationalState = DeviceOperationalState.offline,
) -> Device:
    payload = await prepare_device_create_payload(db, data)
    if mark_verified:
        payload["verified_at"] = datetime.now(UTC)
    payload["operational_state"] = initial_operational_state
    await ensure_device_payload_identity_available(db, payload)
    try:
        return await device_write.create_device_record(db, payload)
    except IntegrityError:
        await db.rollback()
        await ensure_device_payload_identity_available(db, payload)
        raise


async def list_devices(
    db: AsyncSession,
    pack_id: str | None = None,
    platform_id: str | None = None,
    status: ChipStatus | None = None,
    host_id: uuid.UUID | None = None,
    identity_value: str | None = None,
    connection_target: str | None = None,
    device_type: DeviceType | None = None,
    connection_type: ConnectionType | None = None,
    os_version: str | None = None,
    search: str | None = None,
    hardware_health_status: HardwareHealthStatus | None = None,
    hardware_telemetry_state: HardwareTelemetryState | None = None,
    tags: dict[str, str] | None = None,
    sort_by: str = "created_at",
    sort_dir: str = "desc",
) -> list[Device]:
    filters = DeviceQueryFilters(
        pack_id=pack_id,
        platform_id=platform_id,
        status=status,
        host_id=host_id,
        identity_value=identity_value,
        connection_target=connection_target,
        device_type=device_type,
        connection_type=connection_type,
        os_version=os_version,
        hardware_health_status=hardware_health_status,
        hardware_telemetry_state=hardware_telemetry_state,
        search=search,
        tags=tags,
        sort_by=sort_by,
        sort_dir=sort_dir,
    )
    return await list_devices_by_filters(db, filters)


def _apply_device_filters(stmt: DeviceQueryStatement, filters: DeviceQueryFilters) -> DeviceQueryStatement:
    if filters.pack_id is not None:
        stmt = stmt.where(Device.pack_id == filters.pack_id)
    if filters.platform_id is not None:
        stmt = stmt.where(Device.platform_id == filters.platform_id)
    if filters.status is not None:
        if filters.status == "available":
            stmt = stmt.where(Device.operational_state == DeviceOperationalState.available, Device.hold.is_(None))
        elif filters.status == "busy":
            stmt = stmt.where(
                Device.operational_state.in_([DeviceOperationalState.busy, DeviceOperationalState.verifying])
            )
        elif filters.status == "offline":
            stmt = stmt.where(Device.operational_state == DeviceOperationalState.offline, Device.hold.is_(None))
        elif filters.status == "maintenance":
            stmt = stmt.where(
                Device.hold == DeviceHold.maintenance,
                Device.operational_state != DeviceOperationalState.busy,
                Device.operational_state != DeviceOperationalState.verifying,
            )
        elif filters.status == "reserved":
            stmt = stmt.where(
                Device.hold == DeviceHold.reserved,
                Device.operational_state != DeviceOperationalState.busy,
                Device.operational_state != DeviceOperationalState.verifying,
            )
        elif filters.status == "verifying":
            stmt = stmt.where(Device.operational_state == DeviceOperationalState.verifying)
    if filters.host_id is not None:
        stmt = stmt.where(Device.host_id == filters.host_id)
    if filters.identity_value is not None:
        stmt = stmt.where(Device.identity_value == filters.identity_value)
    if filters.connection_target is not None:
        stmt = stmt.where(Device.connection_target == filters.connection_target)
    if filters.device_type is not None:
        stmt = stmt.where(Device.device_type == filters.device_type)
    if filters.connection_type is not None:
        stmt = stmt.where(Device.connection_type == filters.connection_type)
    if filters.os_version is not None:
        stmt = stmt.where(Device.os_version == filters.os_version)
    if filters.hardware_health_status is not None:
        stmt = stmt.where(Device.hardware_health_status == filters.hardware_health_status)
    if filters.tags:
        for key, value in filters.tags.items():
            stmt = stmt.where(Device.tags[key].astext == value)
    if filters.search:
        term = f"%{filters.search}%"
        stmt = stmt.where(
            or_(
                Device.name.ilike(term),
                Device.identity_value.ilike(term),
                Device.connection_target.ilike(term),
            )
        )
    return stmt


def _device_order_clause(filters: DeviceQueryFilters) -> list[Any]:
    direction = asc if filters.sort_dir == "asc" else desc
    chip_case = case(
        (Device.operational_state == DeviceOperationalState.busy, 4),
        (Device.hold == DeviceHold.maintenance, 3),
        (Device.hold == DeviceHold.reserved, 2),
        (Device.operational_state == DeviceOperationalState.offline, 1),
        else_=0,
    )
    order_map: dict[str, Any] = {
        "name": func.lower(Device.name),
        "platform": Device.platform_id,
        "device_type": Device.device_type,
        "connection_type": Device.connection_type,
        "os_version": Device.os_version,
        "host": func.lower(func.coalesce(Host.hostname, "")),
        "status": chip_case,
        "operational_state": Device.operational_state,
        "hold": Device.hold,
        "created_at": Device.created_at,
    }
    primary = order_map.get(filters.sort_by, Device.created_at)
    # Stable secondary so paging is deterministic when the primary key ties.
    return [direction(primary), direction(Device.created_at), direction(Device.id)]


def _build_device_list_stmt(filters: DeviceQueryFilters) -> DeviceListStatement:
    stmt = (
        select(Device)
        .outerjoin(Host, Host.id == Device.host_id)
        .options(selectinload(Device.appium_node))
        .execution_options(populate_existing=True)
    )
    stmt = cast("DeviceListStatement", _apply_device_filters(stmt, filters))
    return stmt.order_by(*_device_order_clause(filters))


def _build_device_count_stmt(filters: DeviceQueryFilters) -> DeviceCountStatement:
    stmt = select(func.count()).select_from(Device)
    return cast("DeviceCountStatement", _apply_device_filters(stmt, filters))


async def list_devices_by_filters(
    db: AsyncSession,
    filters: DeviceQueryFilters,
) -> list[Device]:
    stmt = _build_device_list_stmt(filters)
    result = await db.execute(stmt)
    devices = list(result.scalars().all())
    if filters.needs_attention is not None:
        wanted = filters.needs_attention
        kept: list[Device] = []
        reservation_map = await run_service.get_device_reservation_map(db, [device.id for device in devices])
        for device in devices:
            reservation_context = run_service.get_reservation_context_for_device(
                reservation_map.get(device.id), device.id
            )
            readiness = await device_readiness.assess_device_async(db, device)
            policy = await lifecycle_policy.build_lifecycle_policy(db, device, reservation_context=reservation_context)
            summary = lifecycle_policy.build_lifecycle_policy_summary(policy)
            health_summary = device_health.build_public_summary(device)
            if (
                device_attention.compute_needs_attention(
                    summary["state"],
                    readiness.readiness_state,
                    health_healthy=(health_summary or {}).get("healthy"),
                    hardware_health_status=hardware_telemetry.current_hardware_health_status(device),
                )
                is wanted
            ):
                kept.append(device)
        devices = kept
    if filters.hardware_telemetry_state is not None:
        devices = [
            device
            for device in devices
            if hardware_telemetry.hardware_telemetry_state_for_device(device) == filters.hardware_telemetry_state
        ]
    return devices


async def list_devices_paginated(
    db: AsyncSession,
    filters: DeviceQueryFilters,
    limit: int,
    offset: int,
) -> tuple[list[Device], int]:
    has_post_filters = filters.needs_attention is not None or filters.hardware_telemetry_state is not None

    if has_post_filters:
        all_devices = await list_devices_by_filters(db, filters)
        total = len(all_devices)
        page = all_devices[offset : offset + limit]
        return page, total

    count_result = await db.execute(_build_device_count_stmt(filters))
    total = int(count_result.scalar() or 0)

    stmt = _build_device_list_stmt(filters).limit(limit).offset(offset)
    result = await db.execute(stmt)
    page = list(result.scalars().all())
    return page, total


async def count_devices_by_filters(
    db: AsyncSession,
    filters: DeviceQueryFilters,
) -> int:
    if filters.needs_attention is not None or filters.hardware_telemetry_state is not None:
        return len(await list_devices_by_filters(db, filters))

    result = await db.execute(_build_device_count_stmt(filters))
    return int(result.scalar() or 0)


async def get_device(db: AsyncSession, device_id: uuid.UUID) -> Device | None:
    stmt = (
        select(Device)
        .where(Device.id == device_id)
        .options(selectinload(Device.appium_node), selectinload(Device.sessions), selectinload(Device.host))
        .execution_options(populate_existing=True)
    )
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def update_device(
    db: AsyncSession,
    device_id: uuid.UUID,
    data: DevicePatch | DeviceVerificationUpdate,
    *,
    enforce_patch_contract: bool = True,
) -> Device | None:
    try:
        device = await device_locking.lock_device(db, device_id)
    except NoResultFound:
        return None

    if enforce_patch_contract:
        if not isinstance(data, DevicePatch):
            raise ValueError("PATCH /api/devices/{id} requires the generic device patch contract")
        device_write.validate_patch_contract(device, data)

    payload = await prepare_device_update_payload(db, device, data)
    await ensure_device_payload_identity_available(db, payload, exclude_device_id=device.id)
    if device_readiness.payload_requires_reverification(device, payload):
        device.verified_at = None

    device_write.apply_device_payload(device, payload)
    try:
        return await device_write.persist_device_record(db, device)
    except IntegrityError:
        await db.rollback()
        await ensure_device_payload_identity_available(db, payload, exclude_device_id=device.id)
        raise


async def _lock_device_for_delete(db: AsyncSession, device_id: uuid.UUID) -> Device | None:
    try:
        return await device_locking.lock_device(db, device_id)
    except NoResultFound:
        return None


async def _stop_node(db: AsyncSession, device: Device, *, caller: DesiredStateCaller = "device_delete") -> AppiumNode:
    """Write stopped desired state for a single device."""
    node: AppiumNode | None = device.appium_node
    if node is None or not node.observed_running:
        raise NodeManagerError(f"No running node for device {device.id}")
    await write_desired_state(
        db,
        node=node,
        target=AppiumDesiredState.stopped,
        caller=caller,
    )
    await db.commit()
    await db.refresh(node)
    return node


async def _stop_running_node_for_delete(db: AsyncSession, device: Device, device_id: uuid.UUID) -> Device | None:
    while device.appium_node and device.appium_node.observed_running:
        try:
            await _stop_node(db, device, caller="device_delete")
        except Exception as e:
            logger.warning(
                "Failed to stop node for device %s before delete: %s",
                sanitize_log_value(device_id),
                sanitize_log_value(e),
            )
            return await _lock_device_for_delete(db, device_id)
        relocked = await _lock_device_for_delete(db, device_id)
        if relocked is None:
            return None
        device = relocked
    return device


async def delete_device(db: AsyncSession, device_id: uuid.UUID) -> bool:
    device = await _lock_device_for_delete(db, device_id)
    if device is None:
        return False

    # Stop the running Appium node on the agent before deleting
    if device.appium_node and device.appium_node.observed_running:
        device = await _stop_running_node_for_delete(db, device, device_id)
        if device is None:
            return True

    # Clean up control_plane_state rows keyed by identity_value before deleting
    # the device row, so the cleanup stays in the same transaction.
    await control_plane_state_store.delete_value(db, IP_PING_NAMESPACE, device.identity_value)
    await control_plane_state_store.delete_value(db, CONNECTIVITY_NAMESPACE, device.identity_value)

    await db.delete(device)
    await db.commit()
    return True
