from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import Select, asc, case, desc, func, or_, select
from sqlalchemy.exc import IntegrityError, NoResultFound
from sqlalchemy.orm import selectinload

from app.core.leader import state_store as control_plane_state_store
from app.core.timeutil import now_utc
from app.devices import locking as device_locking
from app.devices.models import (
    Device,
    DeviceGroup,
    DeviceOperationalState,
    GroupType,
    device_search_vector_expression,
)
from app.devices.schemas.device import (
    DevicePatch,
    DeviceVerificationCreate,
    DeviceVerificationUpdate,
)
from app.devices.schemas.filters import DeviceQueryFilters
from app.devices.services import attention as device_attention
from app.devices.services import health as device_health
from app.devices.services import link_repair
from app.devices.services import readiness as device_readiness
from app.devices.services import write as device_write
from app.devices.services.claims import active_reservation_exists
from app.devices.services.connectivity import (
    CONNECTIVITY_NAMESPACE,
    IP_PING_NAMESPACE,
    PROBE_FAILED_NAMESPACE,
    PROBE_UNANSWERED_NAMESPACE,
)
from app.devices.services.group_membership import (
    load_group_membership_index,
    load_groups_by_keys,
    static_group_membership_exists,
)
from app.devices.services.state import (
    derive_operational_state,
    is_available_sql,
    is_busyish_sql,
    is_maintenance_sql,
    is_offline_sql,
    is_verifying_sql,
    operational_state_rank_sql,
)
from app.hosts.models import Host
from app.lifecycle.services import remediation_log

if TYPE_CHECKING:
    import uuid
    from collections.abc import Sequence

    from sqlalchemy import ColumnElement
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.devices.schemas.filters import DeviceGroupFilters
    from app.devices.services.identity_conflicts import DeviceIdentityConflictService
    from app.events.protocols import EventPublisher

DeviceListStatement = Select[tuple[Device]]
DeviceCountStatement = Select[tuple[int]]
DeviceQueryStatement = DeviceListStatement | DeviceCountStatement


class UnknownGroupKeysError(ValueError):
    """Raised when a device query references device group keys that do not exist."""

    def __init__(self, keys: list[str]) -> None:
        self.keys = keys
        super().__init__(f"unknown device groups: {', '.join(keys)}")


class DeviceCrudService:
    def __init__(self, *, identity: DeviceIdentityConflictService, publisher: EventPublisher) -> None:
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
        commit: bool = True,
    ) -> Device:
        payload = await self.prepare_device_create_payload(db, data)
        if mark_verified:
            payload["verified_at"] = now_utc()
        payload["operational_state_last_emitted"] = initial_operational_state
        await self._identity.ensure_device_payload_identity_available(db, payload)
        if not commit:
            device = device_write.stage_device_record(db, payload)
            await db.flush()  # apply the uuid4 PK default and surface IntegrityError inside the caller's txn
            return device
        try:
            return await device_write.create_device_record(db, payload)
        except IntegrityError:
            await db.rollback()
            await self._identity.ensure_device_payload_identity_available(db, payload)
            raise

    async def list_devices_by_filters(self, db: AsyncSession, filters: DeviceQueryFilters) -> list[Device]:
        static_keys, dynamic_groups = await self._partition_group_filters(db, filters)
        return await self._list_devices(db, filters, static_group_keys=static_keys, dynamic_groups=dynamic_groups)

    async def _list_devices(
        self,
        db: AsyncSession,
        filters: DeviceQueryFilters,
        *,
        static_group_keys: list[str],
        dynamic_groups: list[DeviceGroup],
    ) -> list[Device]:
        stmt = _build_device_list_stmt(filters, static_group_keys=static_group_keys)
        result = await db.execute(stmt)
        devices = list(result.scalars().all())
        if dynamic_groups:
            devices = await self._apply_dynamic_groups_filter(db, dynamic_groups, devices)
        if filters.needs_attention is not None:
            wanted = filters.needs_attention
            kept: list[Device] = []
            readiness_map = await device_readiness.assess_devices_async(db, devices)
            for device in devices:
                readiness = readiness_map[device.id]
                operational_state = await derive_operational_state(db, device, now=now_utc())
                if (
                    device_attention.compute_needs_attention(
                        operational_state,
                        readiness.readiness_state,
                        review_required=bool(device.review_required),
                    )
                    is wanted
                ):
                    kept.append(device)
            devices = kept
        if filters.device_health is not None or filters.node_health is not None or filters.viability is not None:
            kept_health: list[Device] = []
            ladders = await remediation_log.load_ladders(db, [device.id for device in devices])
            for device in devices:
                health_summary = device_health.build_public_summary(
                    device,
                    policy_view=remediation_log.build_policy_view(ladders[device.id], device.lifecycle_policy_state),
                )
                if filters.device_health is not None and health_summary["device"]["status"] != filters.device_health:
                    continue
                if filters.node_health is not None and health_summary["node"]["status"] != filters.node_health:
                    continue
                if filters.viability is not None and health_summary["viability"]["status"] != filters.viability:
                    continue
                kept_health.append(device)
            devices = kept_health
        return devices

    async def _partition_group_filters(
        self, db: AsyncSession, filters: DeviceQueryFilters
    ) -> tuple[list[str], list[DeviceGroup]]:
        """Split ``filters.groups`` into SQL-expressible and evaluator-only keys.

        Static membership is a join on ``device_group_memberships``, so those
        keys become WHERE predicates and the query keeps paginating in SQL. Only
        dynamic keys need the in-memory evaluator (dynamic membership is never
        materialised), so only they force the load-everything-then-slice branch.

        One read validates the whole key set and raises
        :class:`UnknownGroupKeysError` for any missing key so the router
        surfaces HTTP 422.
        """
        keys = list(filters.groups)
        if not keys:
            return [], []
        groups = await load_groups_by_keys(db, keys)
        by_key = {group.key: group for group in groups}
        missing = [key for key in keys if key not in by_key]
        if missing:
            raise UnknownGroupKeysError(missing)
        static_keys = [key for key in keys if by_key[key].group_type == GroupType.static]
        dynamic_groups = [by_key[key] for key in keys if by_key[key].group_type == GroupType.dynamic]
        return static_keys, dynamic_groups

    async def _apply_dynamic_groups_filter(
        self, db: AsyncSession, dynamic_groups: list[DeviceGroup], devices: list[Device]
    ) -> list[Device]:
        """AND membership across the dynamic keys, evaluated live over the batch."""
        index = await load_group_membership_index(db, groups=dynamic_groups, devices=devices)
        keys = [group.key for group in dynamic_groups]
        return [device for device in devices if index.matches_all(device.id, keys)]

    async def list_devices_paginated(
        self, db: AsyncSession, filters: DeviceQueryFilters, limit: int, offset: int
    ) -> tuple[list[Device], int]:
        static_keys, dynamic_groups = await self._partition_group_filters(db, filters)

        if _has_post_filters(filters) or dynamic_groups:
            all_devices = await self._list_devices(
                db, filters, static_group_keys=static_keys, dynamic_groups=dynamic_groups
            )
            total = len(all_devices)
            page = all_devices[offset : offset + limit]
            return page, total

        count_result = await db.execute(_build_device_count_stmt(filters, static_group_keys=static_keys))
        total = int(count_result.scalar() or 0)

        stmt = _build_device_list_stmt(filters, static_group_keys=static_keys).limit(limit).offset(offset)
        result = await db.execute(stmt)
        page = list(result.scalars().all())
        return page, total

    async def count_devices_by_filters(self, db: AsyncSession, filters: DeviceQueryFilters) -> int:
        static_keys, dynamic_groups = await self._partition_group_filters(db, filters)
        if _has_post_filters(filters) or dynamic_groups:
            return len(
                await self._list_devices(db, filters, static_group_keys=static_keys, dynamic_groups=dynamic_groups)
            )

        result = await db.execute(_build_device_count_stmt(filters, static_group_keys=static_keys))
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


def _has_post_filters(filters: DeviceQueryFilters) -> bool:
    return (
        filters.needs_attention is not None
        or filters.device_health is not None
        or filters.node_health is not None
        or filters.viability is not None
    )


def _status_condition(status: str) -> ColumnElement[bool] | None:
    if status == "available":
        return is_available_sql(now=now_utc())
    if status == "busy":
        return or_(is_busyish_sql(), is_verifying_sql(now=now_utc()))
    if status == "offline":
        return is_offline_sql(now=now_utc())
    if status == "maintenance":
        return is_maintenance_sql(now=now_utc())
    if status == "verifying":
        return is_verifying_sql(now=now_utc())
    return None


def _identity_conditions(filters: DeviceQueryFilters) -> list[ColumnElement[bool]]:
    conditions: list[ColumnElement[bool]] = []
    if filters.host_id is not None:
        conditions.append(Device.host_id == filters.host_id)
    if filters.identity_value is not None:
        conditions.append(Device.identity_value == filters.identity_value)
    if filters.connection_target is not None:
        conditions.append(Device.connection_target == filters.connection_target)
    if filters.device_type is not None:
        conditions.append(Device.device_type == filters.device_type)
    if filters.connection_type is not None:
        conditions.append(Device.connection_type == filters.connection_type)
    return conditions


def _version_and_text_conditions(filters: DeviceQueryFilters) -> list[ColumnElement[bool]]:
    conditions: list[ColumnElement[bool]] = []
    if filters.os_version is not None:
        conditions.append(Device.os_version == filters.os_version)
    if filters.os_version_display is not None:
        conditions.append(func.coalesce(Device.os_version_display, Device.os_version) == filters.os_version_display)
    if filters.search:
        query = func.websearch_to_tsquery("simple", filters.search)
        conditions.append(device_search_vector_expression().op("@@")(query))
    return conditions


def _device_filter_conditions(filters: DeviceQueryFilters) -> list[ColumnElement[bool]]:
    """Every SQL predicate the filter set contributes, as composable conditions.

    Split out of :func:`_apply_device_filters` so the same predicates can be
    ORed across group definitions (see :func:`device_scope_conditions`) instead
    of only ANDed onto one statement.
    """
    conditions: list[ColumnElement[bool]] = []
    if filters.pack_id is not None:
        conditions.append(Device.pack_id == filters.pack_id)
    if filters.platform_id is not None:
        conditions.append(Device.platform_id == filters.platform_id)
    if filters.status is not None:
        status_condition = _status_condition(filters.status)
        if status_condition is not None:
            conditions.append(status_condition)
    if filters.reserved is not None:
        conditions.append(active_reservation_exists() if filters.reserved else ~active_reservation_exists())
    conditions.extend(_identity_conditions(filters))
    conditions.extend(_version_and_text_conditions(filters))
    return conditions


def _apply_device_filters(stmt: DeviceQueryStatement, filters: DeviceQueryFilters) -> DeviceQueryStatement:
    return stmt.where(*_device_filter_conditions(filters))


# Group-filter axes that map to a plain ``devices`` column predicate. The
# fact-derived axes (``status``, ``reserved``, ``needs_attention``) are
# deliberately excluded: their SQL twins are evaluated at a different instant
# than the in-memory facts the evaluator uses, so narrowing a *candidate*
# load on them could drop a device the evaluator would have included. Those
# axes stay in the pure evaluator.
_COLUMN_SCOPE_AXES = frozenset(
    {
        "pack_id",
        "platform_id",
        "host_id",
        "identity_value",
        "connection_target",
        "device_type",
        "connection_type",
        "os_version",
        "os_version_display",
    }
)


def device_scope_conditions(filters: DeviceGroupFilters) -> list[ColumnElement[bool]]:
    """Conditions that bound the candidate devices a dynamic group can contain.

    A superset filter, never an exact one: it reuses the device-list column
    predicates for the axes that are plain columns plus a static-membership
    EXISTS per ``member_of`` key, and leaves every fact-derived axis to
    :func:`evaluate_group_memberships`. An empty list means "the whole fleet is
    in scope" — the group pins nothing a query can narrow on.
    """
    column_filters = DeviceQueryFilters.model_validate(
        {key: value for key, value in filters.model_dump().items() if key in _COLUMN_SCOPE_AXES}
    )
    conditions = _device_filter_conditions(column_filters)
    conditions.extend(static_group_membership_exists(key) for key in filters.member_of)
    return conditions


def _device_order_clause(filters: DeviceQueryFilters) -> list[Any]:
    direction = asc if filters.sort_dir == "asc" else desc
    now = now_utc()
    chip_case = case(
        (is_busyish_sql(), 4),
        (is_maintenance_sql(now=now), 3),
        (active_reservation_exists(), 2),
        (is_offline_sql(now=now), 1),
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
        "operational_state": operational_state_rank_sql(now=now),
        "created_at": Device.created_at,
    }
    primary = order_map.get(filters.sort_by, Device.created_at)
    # Stable secondary so paging is deterministic when the primary key ties.
    return [direction(primary), direction(Device.created_at), direction(Device.id)]


def _static_group_conditions(static_group_keys: Sequence[str]) -> list[ColumnElement[bool]]:
    """AND semantics: a device must be a member of every requested static group.

    One correlated EXISTS per key inside a single statement, so the statement
    count stays at one however many keys are requested.
    """
    return [static_group_membership_exists(key) for key in static_group_keys]


def _build_device_list_stmt(
    filters: DeviceQueryFilters, *, static_group_keys: Sequence[str] = ()
) -> DeviceListStatement:
    stmt = (
        select(Device)
        .outerjoin(Host, Host.id == Device.host_id)
        .options(selectinload(Device.appium_node))
        .execution_options(populate_existing=True)
    )
    stmt = cast("DeviceListStatement", _apply_device_filters(stmt, filters))
    stmt = stmt.where(*_static_group_conditions(static_group_keys))
    return stmt.order_by(*_device_order_clause(filters))


def _build_device_count_stmt(
    filters: DeviceQueryFilters, *, static_group_keys: Sequence[str] = ()
) -> DeviceCountStatement:
    stmt = select(func.count()).select_from(Device)
    stmt = cast("DeviceCountStatement", _apply_device_filters(stmt, filters))
    return stmt.where(*_static_group_conditions(static_group_keys))


async def _lock_device_for_delete(db: AsyncSession, device_id: uuid.UUID) -> Device | None:
    try:
        return await device_locking.lock_device(db, device_id)
    except NoResultFound:
        return None
