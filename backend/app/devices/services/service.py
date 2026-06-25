from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import Select, asc, case, desc, func, select
from sqlalchemy.exc import IntegrityError, NoResultFound
from sqlalchemy.orm import selectinload

from app.core.leader import state_store as control_plane_state_store
from app.devices import locking as device_locking
from app.devices.models import (
    Device,
    DeviceOperationalState,
    device_search_vector_expression,
)
from app.devices.schemas.device import (
    DevicePatch,
    DeviceVerificationCreate,
    DeviceVerificationUpdate,
)
from app.devices.services import attention as device_attention
from app.devices.services import health as device_health
from app.devices.services import link_repair
from app.devices.services import readiness as device_readiness
from app.devices.services import write as device_write
from app.devices.services.connectivity import (
    CONNECTIVITY_NAMESPACE,
    IP_PING_NAMESPACE,
    PROBE_FAILED_NAMESPACE,
    PROBE_UNANSWERED_NAMESPACE,
)
from app.devices.services.reservation_query import active_reservation_exists
from app.hosts import service_hardware_telemetry as hardware_telemetry
from app.hosts.models import Host

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.core.protocols import SettingsReader
    from app.devices.schemas.filters import DeviceQueryFilters
    from app.devices.services.identity_conflicts import DeviceIdentityConflictService
    from app.events.protocols import EventPublisher

DeviceListStatement = Select[tuple[Device]]
DeviceCountStatement = Select[tuple[int]]
DeviceQueryStatement = DeviceListStatement | DeviceCountStatement


class DeviceCrudService:
    def __init__(
        self, *, settings: SettingsReader, identity: DeviceIdentityConflictService, publisher: EventPublisher
    ) -> None:
        self._settings = settings
        self._identity = identity
        self._publisher = publisher

    async def prepare_device_create_payload(self, db: AsyncSession, data: DeviceVerificationCreate) -> dict[str, Any]:
        return await device_write.prepare_device_create_payload_async(db, data)

    async def prepare_device_update_payload(
        self, db: AsyncSession, device: Device, data: DevicePatch | DeviceVerificationUpdate
    ) -> dict[str, Any]:
        return await device_write.prepare_device_update_payload_async(db, device, data)

    async def create_device(
        self,
        db: AsyncSession,
        data: DeviceVerificationCreate,
        *,
        mark_verified: bool = False,
        initial_operational_state: DeviceOperationalState = DeviceOperationalState.offline,
    ) -> Device:
        payload = await self.prepare_device_create_payload(db, data)
        if mark_verified:
            payload["verified_at"] = datetime.now(UTC)
        payload["operational_state"] = initial_operational_state
        await self._identity.ensure_device_payload_identity_available(db, payload)
        try:
            return await device_write.create_device_record(db, payload)
        except IntegrityError:
            await db.rollback()
            await self._identity.ensure_device_payload_identity_available(db, payload)
            raise

    async def list_devices_by_filters(self, db: AsyncSession, filters: DeviceQueryFilters) -> list[Device]:
        stmt = _build_device_list_stmt(filters)
        result = await db.execute(stmt)
        devices = list(result.scalars().all())
        if filters.needs_attention is not None:
            wanted = filters.needs_attention
            kept: list[Device] = []
            readiness_map = await device_readiness.assess_devices_async(db, devices)
            for device in devices:
                readiness = readiness_map[device.id]
                if (
                    device_attention.compute_needs_attention(
                        device.operational_state,
                        readiness.readiness_state,
                        hardware_health_status=hardware_telemetry.current_hardware_health_status(device),
                        review_required=bool(device.review_required),
                    )
                    is wanted
                ):
                    kept.append(device)
            devices = kept
        if filters.device_health is not None or filters.node_health is not None or filters.viability is not None:
            kept_health: list[Device] = []
            for device in devices:
                health_summary = device_health.build_public_summary(device)
                if filters.device_health is not None and health_summary["device"]["status"] != filters.device_health:
                    continue
                if filters.node_health is not None and health_summary["node"]["status"] != filters.node_health:
                    continue
                if filters.viability is not None and health_summary["viability"]["status"] != filters.viability:
                    continue
                kept_health.append(device)
            devices = kept_health
        if filters.hardware_telemetry_state is not None:
            devices = [
                device
                for device in devices
                if hardware_telemetry.hardware_telemetry_state_for_device(device, settings=self._settings)
                == filters.hardware_telemetry_state
            ]
        return devices

    async def list_devices_paginated(
        self, db: AsyncSession, filters: DeviceQueryFilters, limit: int, offset: int
    ) -> tuple[list[Device], int]:
        has_post_filters = (
            filters.needs_attention is not None
            or filters.hardware_telemetry_state is not None
            or filters.device_health is not None
            or filters.node_health is not None
            or filters.viability is not None
        )

        if has_post_filters:
            all_devices = await self.list_devices_by_filters(db, filters)
            total = len(all_devices)
            page = all_devices[offset : offset + limit]
            return page, total

        count_result = await db.execute(_build_device_count_stmt(filters))
        total = int(count_result.scalar() or 0)

        stmt = _build_device_list_stmt(filters).limit(limit).offset(offset)
        result = await db.execute(stmt)
        page = list(result.scalars().all())
        return page, total

    async def count_devices_by_filters(self, db: AsyncSession, filters: DeviceQueryFilters) -> int:
        if (
            filters.needs_attention is not None
            or filters.hardware_telemetry_state is not None
            or filters.device_health is not None
            or filters.node_health is not None
            or filters.viability is not None
        ):
            return len(await self.list_devices_by_filters(db, filters))

        result = await db.execute(_build_device_count_stmt(filters))
        return int(result.scalar() or 0)

    async def get_device(self, db: AsyncSession, device_id: uuid.UUID) -> Device | None:
        stmt = (
            select(Device)
            .where(Device.id == device_id)
            .options(selectinload(Device.appium_node), selectinload(Device.sessions), selectinload(Device.host))
            .execution_options(populate_existing=True)
        )
        result = await db.execute(stmt)
        return result.scalar_one_or_none()

    async def update_device(
        self,
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

        payload = await self.prepare_device_update_payload(db, device, data)
        await self._identity.ensure_device_payload_identity_available(db, payload, exclude_device_id=device.id)
        if device_readiness.payload_requires_reverification(device, payload):
            device.verified_at = None

        device_write.apply_device_payload(device, payload)
        try:
            return await device_write.persist_device_record(db, device)
        except IntegrityError:
            await db.rollback()
            await self._identity.ensure_device_payload_identity_available(db, payload, exclude_device_id=device.id)
            raise

    async def delete_device(self, db: AsyncSession, device_id: uuid.UUID) -> bool:
        device = await _lock_device_for_delete(db, device_id)
        if device is None:
            return False

        # Deleting the device row cascade-removes its AppiumNode row. We do NOT
        # wait for the agent's Appium process to stop here: that stop is async
        # (agent poll + observation), so blocking on it would hang the request
        # until an unrelated background loop converged — or forever if the agent
        # is unreachable. The leftover process is reaped by the appium_reconciler
        # `no_db_row` orphan sweep once it has no DB row to back it.

        # Clean up control_plane_state rows keyed by identity_value before deleting
        # the device row, so the cleanup stays in the same transaction.
        await control_plane_state_store.delete_value(db, IP_PING_NAMESPACE, device.identity_value)
        await control_plane_state_store.delete_value(db, CONNECTIVITY_NAMESPACE, device.identity_value)
        await control_plane_state_store.delete_value(db, PROBE_UNANSWERED_NAMESPACE, device.identity_value)
        await control_plane_state_store.delete_value(db, PROBE_FAILED_NAMESPACE, device.identity_value)
        await link_repair.reset_repair_attempts(db, device.identity_value)

        await db.delete(device)
        await db.commit()
        return True


def _apply_status_filter(stmt: DeviceQueryStatement, status: str) -> DeviceQueryStatement:
    if status == "available":
        return stmt.where(Device.operational_state == DeviceOperationalState.available)
    if status == "busy":
        return stmt.where(Device.operational_state.in_([DeviceOperationalState.busy, DeviceOperationalState.verifying]))
    if status == "offline":
        return stmt.where(Device.operational_state == DeviceOperationalState.offline)
    if status == "maintenance":
        return stmt.where(Device.operational_state == DeviceOperationalState.maintenance)
    if status == "verifying":
        return stmt.where(Device.operational_state == DeviceOperationalState.verifying)
    return stmt


def _apply_identity_filters(stmt: DeviceQueryStatement, filters: DeviceQueryFilters) -> DeviceQueryStatement:
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
    return stmt


def _apply_version_and_text_filters(stmt: DeviceQueryStatement, filters: DeviceQueryFilters) -> DeviceQueryStatement:
    if filters.os_version is not None:
        stmt = stmt.where(Device.os_version == filters.os_version)
    if filters.os_version_display is not None:
        stmt = stmt.where(func.coalesce(Device.os_version_display, Device.os_version) == filters.os_version_display)
    if filters.hardware_health_status is not None:
        stmt = stmt.where(Device.hardware_health_status == filters.hardware_health_status)
    if filters.tags:
        stmt = stmt.where(Device.tags.contains(filters.tags))
    if filters.search:
        query = func.websearch_to_tsquery("simple", filters.search)
        stmt = stmt.where(device_search_vector_expression().op("@@")(query))
    return stmt


def _apply_device_filters(stmt: DeviceQueryStatement, filters: DeviceQueryFilters) -> DeviceQueryStatement:
    if filters.pack_id is not None:
        stmt = stmt.where(Device.pack_id == filters.pack_id)
    if filters.platform_id is not None:
        stmt = stmt.where(Device.platform_id == filters.platform_id)
    if filters.status is not None:
        stmt = _apply_status_filter(stmt, filters.status)
    if filters.reserved is not None:
        stmt = stmt.where(active_reservation_exists() if filters.reserved else ~active_reservation_exists())
    stmt = _apply_identity_filters(stmt, filters)
    stmt = _apply_version_and_text_filters(stmt, filters)
    return stmt


def _device_order_clause(filters: DeviceQueryFilters) -> list[Any]:
    direction = asc if filters.sort_dir == "asc" else desc
    chip_case = case(
        (Device.operational_state == DeviceOperationalState.busy, 4),
        (Device.operational_state == DeviceOperationalState.maintenance, 3),
        (active_reservation_exists(), 2),
        (Device.operational_state == DeviceOperationalState.offline, 1),
        else_=0,
    )
    order_map: dict[str, Any] = {
        "name": func.lower(Device.name),
        "platform": Device.platform_id,
        "device_type": Device.device_type,
        "connection_type": Device.connection_type,
        "os_version": Device.os_version,
        "os_version_display": func.coalesce(Device.os_version_display, Device.os_version),
        "host": func.lower(func.coalesce(Host.hostname, "")),
        "status": chip_case,
        "operational_state": Device.operational_state,
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


async def _lock_device_for_delete(db: AsyncSession, device_id: uuid.UUID) -> Device | None:
    try:
        return await device_locking.lock_device(db, device_id)
    except NoResultFound:
        return None
