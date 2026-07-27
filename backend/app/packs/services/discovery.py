from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, cast

from sqlalchemy import select
from sqlalchemy.exc import NoResultFound
from sqlalchemy.orm import joinedload

from app.core.timeutil import now_utc
from app.devices import locking as device_locking
from app.devices.models import ConnectionType, Device
from app.devices.schemas.device import DeviceVerificationCreate, DeviceVerificationUpdate
from app.devices.services import platform_label as platform_label_service
from app.devices.services import write as device_write
from app.devices.services.identity import is_host_scoped_identity
from app.devices.services.read_projection import load_device_read_projections
from app.hosts.models import Host
from app.hosts.schemas import DiscoveredDevice, DiscoveryConfirmResult, DiscoveryResult, IntakeCandidateRead

if TYPE_CHECKING:
    import uuid
    from collections.abc import Iterable, Mapping, Sequence

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.agent_comm.http_pool import AgentHttpPool
    from app.agent_comm.protocols import CircuitBreakerProtocol
    from app.devices.services.platform_label import PackPlatformKey
    from app.hosts.service import HostTarget
    from app.packs.protocols import DeviceIdentityGuard, DeviceSerializer


class PackDevicesFetcher(Protocol):
    async def __call__(
        self,
        host: str,
        agent_port: int,
        *,
        circuit_breaker: CircuitBreakerProtocol,
        pool: AgentHttpPool | None = None,
    ) -> dict[str, object]: ...


IdentityKey = tuple[str, str, str]
# (identity_scheme, identity_value, host_scoped) — the tuple the intake route
# matches a candidate against, with the host/non-host scope split folded in as
# the boolean the two scope clauses used to express in SQL.
IntakeIdentityKey = tuple[str, str, bool]


class PackDiscoveryService:
    def __init__(
        self,
        *,
        agent_get_pack_devices: PackDevicesFetcher,
        circuit_breaker: CircuitBreakerProtocol,
        serializer: DeviceSerializer,
        identity_guard: DeviceIdentityGuard,
        pool: AgentHttpPool | None = None,
    ) -> None:
        self._agent_get_pack_devices = agent_get_pack_devices
        self._circuit_breaker = circuit_breaker
        self._serializer = serializer
        self._identity_guard = identity_guard
        self._pool = pool

    async def fetch_pack_candidates(self, target: HostTarget) -> tuple[Mapping[str, Any], ...]:
        """Dial the agent. No session, no lock, no database — just the raw list.

        This is the only phase that touches the network, and it is deliberately
        unable to answer ``new``/``updated``/``already_registered``: those are
        classifications against existing ``Device`` rows, and computing them
        here would mean holding a session across the dial.
        """
        raw = await self._agent_get_pack_devices(
            target.ip, target.agent_port, circuit_breaker=self._circuit_breaker, pool=self._pool
        )
        return tuple(cast("list[dict[str, Any]]", raw.get("candidates", [])))

    async def classify_discovery(
        self, db: AsyncSession, host_id: uuid.UUID, candidates: Sequence[Mapping[str, Any]]
    ) -> DiscoveryResult:
        """Split fetched candidates against this host's rows. Transaction-local."""
        label_map = await self._load_label_map(db, candidates)
        existing_devices = await self._load_host_devices(db, host_id)
        return _classify(candidates, existing_devices, label_map)

    async def build_intake_candidates(
        self, db: AsyncSession, host_id: uuid.UUID, candidates: Sequence[Mapping[str, Any]]
    ) -> list[IntakeCandidateRead]:
        """Decorate fetched candidates with labels and registration state.

        One identity lookup for the whole batch: the per-candidate
        ``select(Device)`` this replaced made the route N+1 in the number of
        devices the agent reports. The host/non-host scope split that used to be
        two SQL clauses is now the boolean in :data:`IntakeIdentityKey`.
        """
        label_map = await self._load_label_map(db, candidates)
        existing_devices = await self._load_host_devices(db, host_id)
        by_identity: dict[IntakeIdentityKey, Device] = {}
        for device in existing_devices:
            by_identity.setdefault(_device_intake_key(device), device)

        result: list[IntakeCandidateRead] = []
        for c in candidates:
            props: dict[str, Any] = c.get("detected_properties") or {}
            identity_value: str = c["identity_value"]
            platform_id: str = c.get("platform_id", "")
            identity_scope = c.get("identity_scope") or (props.get("identity_scope") if props else None)
            existing = by_identity.get(
                (
                    c["identity_scheme"],
                    identity_value,
                    is_host_scoped_identity(identity_scope=identity_scope),
                )
            )
            result.append(
                IntakeCandidateRead(
                    pack_id=c["pack_id"],
                    platform_id=platform_id,
                    platform_label=label_map.get((c["pack_id"], platform_id)),
                    identity_scheme=c["identity_scheme"],
                    identity_scope=c.get("identity_scope", "host"),
                    identity_value=identity_value,
                    connection_target=props.get("connection_target") or None,
                    name=c.get("suggested_name") or identity_value,
                    os_version=props.get("os_version", ""),
                    manufacturer=props.get("manufacturer", ""),
                    model=props.get("model", ""),
                    model_number=props.get("model_number", ""),
                    software_versions=props.get("software_versions") or None,
                    detected_properties=props if props else None,
                    device_type=props.get("device_type"),
                    connection_type=props.get("connection_type") or None,
                    ip_address=props.get("ip_address") or None,
                    already_registered=existing is not None,
                    registered_device_id=existing.id if existing is not None else None,
                )
            )
        return result

    async def _load_label_map(
        self, db: AsyncSession, candidates: Iterable[Mapping[str, Any]]
    ) -> dict[PackPlatformKey, str | None]:
        return await platform_label_service.load_platform_label_map(
            db,
            ((str(c.get("pack_id", "")), str(c.get("platform_id", ""))) for c in candidates),
        )

    async def _load_host_devices(self, db: AsyncSession, host_id: uuid.UUID) -> list[Device]:
        stmt = select(Device).where(Device.host_id == host_id)
        return list((await db.execute(stmt)).scalars().all())

    async def apply_pack_device_properties(
        self, session: AsyncSession, device: Device, data: dict[str, object]
    ) -> None:
        """Fold refreshed properties onto *device*. Mutation only, no boundary.

        ``PropertyRefreshService.fold_host_device_properties`` owns the
        transaction now — it opens ``session_factory.begin()`` per device — so
        this leaves the row dirty and lets the caller decide.
        """
        props_raw = data.get("detected_properties")
        props = cast("dict[str, Any]", props_raw) if isinstance(props_raw, dict) else {}

        new_os_version: str | None = props.get("os_version") or None
        if new_os_version and device.os_version != new_os_version:
            device.os_version = new_os_version

        new_os_version_display = props.get("os_version_display")
        new_display_str: str | None = new_os_version_display if isinstance(new_os_version_display, str) else None
        if new_display_str and device.os_version_display != new_display_str:
            device.os_version_display = new_display_str

        new_software_versions = props.get("software_versions") or None
        if isinstance(new_software_versions, dict) and device.software_versions != new_software_versions:
            device.software_versions = new_software_versions

        # The agent only returns a candidate whose identity matched the requested
        # identity_value; guard again here so a stale or mismatched payload can
        # never repoint the device row at another device's address. Network
        # devices only (the DHCP-move case): emulator/USB targets are owned by
        # intake/verification, and the android pack reports different target
        # forms from discover (live serial) vs normalize (AVD name) — writing
        # both would make the row oscillate every refresh cycle.
        new_connection_target = props.get("connection_target")
        if (
            device.connection_type == ConnectionType.network
            and isinstance(new_connection_target, str)
            and new_connection_target
            and data.get("identity_value") == device.identity_value
            and device.connection_target != new_connection_target
        ):
            device.connection_target = new_connection_target

    async def _lock_host_for_confirm(self, db: AsyncSession, target: HostTarget) -> Host:
        """Take the aggregate root lock and revalidate the prepared target.

        ``target`` was copied before the agent dial, so between the copy and here
        the host may have been deleted or a restarted agent may have registered a
        new boot. Either way the candidate list describes a host generation that
        is no longer current, and the confirmation declines exactly as it does
        for a host that is simply gone.
        """
        stmt = select(Host).where(Host.id == target.host_id).with_for_update()
        host = (await db.execute(stmt)).scalar_one_or_none()
        if host is None or host.current_boot_id != target.current_boot_id:
            raise NoResultFound
        return host

    async def confirm_discovery(
        self,
        db: AsyncSession,
        target: HostTarget,
        candidates: Sequence[Mapping[str, Any]],
        add_identity_values: list[str],
        remove_identity_values: list[str],
    ) -> DiscoveryConfirmResult:
        """Apply an operator's intake decision. Transaction-local.

        Lock order is Host, then the affected ``Device`` rows in ascending id
        order (``lock_devices``). Classification runs *after* the Host lock, so
        the new/updated/removed split is computed against the state the write
        actually lands on rather than the pre-dial read.
        """
        host = await self._lock_host_for_confirm(db, target)
        label_map = await self._load_label_map(db, candidates)
        existing_devices = await self._load_host_devices(db, target.host_id)
        discovery_result = _classify(candidates, existing_devices, label_map)

        added: list[str] = []
        removed: list[str] = []
        updated: list[str] = []
        added_devices: list[Device] = []

        # The public confirm payload still identifies rows by identity_value. Internally,
        # matching must use the full identity tuple so different schemes do not collide.
        discovered_by_value: dict[str, list[DiscoveredDevice]] = {}
        for discovered in discovery_result.new_devices:
            discovered_by_value.setdefault(discovered.identity_value, []).append(discovered)
        discovered_keys = {
            _discovered_identity_key(discovered)
            for discovered in [*discovery_result.new_devices, *discovery_result.updated_devices]
        }
        existing_by_key = {_device_identity_key(device): device for device in existing_devices}
        removal_values = set(remove_identity_values)

        # One sorted Device lock covering every row this confirmation may touch,
        # taken before the first mutation. Rows created below are new and have no
        # peer that could hold them.
        await device_locking.lock_devices(
            db,
            [
                *(
                    existing_by_key[key].id
                    for key in (_discovered_identity_key(d) for d in discovery_result.updated_devices)
                    if key in existing_by_key
                ),
                *(
                    device.id
                    for device in existing_devices
                    if device.identity_value in removal_values and _device_identity_key(device) not in discovered_keys
                ),
            ],
        )

        for identity_value in add_identity_values:
            for discovered in discovered_by_value.get(identity_value, []):
                create_request = _build_discovery_create_request(discovered, host)
                payload = device_write.prepare_device_create_payload(create_request)
                await self._identity_guard.ensure_device_payload_identity_available(db, payload)
                payload["verified_at"] = None
                device = device_write.stage_device_record(db, payload)
                added_devices.append(device)
                added.append(identity_value)

        # Auto-apply os_version + tags updates for existing devices
        # name, platform, model, manufacturer, and device_type are immutable — only changeable manually
        for discovered in discovery_result.updated_devices:
            existing_device = existing_by_key.get(_discovered_identity_key(discovered))
            if existing_device is not None:
                update_request = _build_discovery_update_request(existing_device, discovered)
                payload = device_write.prepare_device_update_payload(existing_device, update_request)
                device_write.apply_device_payload(existing_device, payload)
                updated.append(discovered.identity_value)

        for identity_value in remove_identity_values:
            devices_to_remove = [
                device
                for device in existing_devices
                if device.identity_value == identity_value and _device_identity_key(device) not in discovered_keys
            ]
            for device_to_remove in devices_to_remove:
                await db.delete(device_to_remove)
            if devices_to_remove:
                removed.append(identity_value)

        await db.flush()
        return DiscoveryConfirmResult(
            added=added,
            removed=removed,
            updated=updated,
            added_devices=await self._project_added_devices(db, added_devices),
        )

    async def _project_added_devices(self, db: AsyncSession, devices: list[Device]) -> list[dict[str, Any]]:
        """Serialise the rows just created, inside the caller's transaction.

        One Phase 5 batch projection for the whole list rather than a
        per-device ``refresh`` + ``serialize_device`` after a commit.
        """
        if not devices:
            return []
        # The projection's node-viability reads touch ``Device.appium_node``, and a
        # row that has only just been flushed still has that relationship unloaded —
        # a lazy load there is a MissingGreenlet under asyncio. One joined read
        # populates the whole batch (every one of them is nodeless by construction).
        await db.execute(
            select(Device)
            .where(Device.id.in_([device.id for device in devices]))
            .options(joinedload(Device.appium_node))
            .execution_options(populate_existing=True)
        )
        projections = await load_device_read_projections(db, devices, now=now_utc())
        return [self._serializer.serialize_projected_device(device, projections[device.id]) for device in devices]


def _classify(
    candidates: Sequence[Mapping[str, Any]],
    existing_devices: Sequence[Device],
    label_map: Mapping[PackPlatformKey, str | None],
) -> DiscoveryResult:
    """The new / updated / removed split, against rows already loaded."""
    existing_by_identity = {_device_identity_key(d): d for d in existing_devices}
    seen_identity_keys: set[IdentityKey] = set()

    new_devices: list[DiscoveredDevice] = []
    updated_devices: list[DiscoveredDevice] = []

    for c in candidates:
        identity_key = _candidate_identity_key(c)
        seen_identity_keys.add(identity_key)
        discovered = _candidate_to_discovered(c, platform_label=label_map.get((c["pack_id"], c["platform_id"])))
        if identity_key in existing_by_identity:
            updated_devices.append(discovered)
        else:
            new_devices.append(discovered)

    removed_identity_values = [
        d.identity_value for d in existing_devices if _device_identity_key(d) not in seen_identity_keys
    ]
    return DiscoveryResult(
        new_devices=new_devices,
        updated_devices=updated_devices,
        removed_identity_values=removed_identity_values,
    )


def _identity_key(*, identity_scope: str | None, identity_scheme: str, identity_value: str) -> IdentityKey:
    return (identity_scope or "host", identity_scheme, identity_value)


def _device_intake_key(device: Device) -> IntakeIdentityKey:
    return (
        device.identity_scheme,
        device.identity_value,
        is_host_scoped_identity(identity_scope=device.identity_scope),
    )


def _candidate_identity_key(candidate: Mapping[str, Any]) -> IdentityKey:
    return _identity_key(
        identity_scope=candidate.get("identity_scope"),
        identity_scheme=candidate["identity_scheme"],
        identity_value=candidate["identity_value"],
    )


def _discovered_identity_key(discovered: DiscoveredDevice) -> IdentityKey:
    return _identity_key(
        identity_scope=discovered.identity_scope,
        identity_scheme=discovered.identity_scheme,
        identity_value=discovered.identity_value,
    )


def _device_identity_key(device: Device) -> IdentityKey:
    return _identity_key(
        identity_scope=device.identity_scope,
        identity_scheme=device.identity_scheme,
        identity_value=device.identity_value,
    )


def _candidate_to_discovered(c: Mapping[str, Any], *, platform_label: str | None = None) -> DiscoveredDevice:
    props: dict[str, Any] = c.get("detected_properties") or {}
    return DiscoveredDevice(
        pack_id=c["pack_id"],
        platform_id=c["platform_id"],
        platform_label=platform_label,
        identity_scheme=c["identity_scheme"],
        identity_scope=c.get("identity_scope", "host"),
        identity_value=c["identity_value"],
        connection_target=props.get("connection_target") or c.get("identity_value"),
        name=c.get("suggested_name") or c["identity_value"],
        os_version=props.get("os_version", ""),
        manufacturer=props.get("manufacturer", ""),
        model=props.get("model", ""),
        model_number=props.get("model_number", ""),
        software_versions=props.get("software_versions") or None,
        detected_properties=props if props else None,
        device_type=props.get("device_type") or None,
        connection_type=props.get("connection_type") or None,
        ip_address=props.get("ip_address") or None,
        readiness_state="verification_required",
        can_verify_now=bool(c.get("runnable", False)),
    )


def _build_discovery_create_request(discovered: DiscoveredDevice, host: Host) -> DeviceVerificationCreate:
    return DeviceVerificationCreate(
        pack_id=discovered.pack_id,
        platform_id=discovered.platform_id,
        identity_scheme=discovered.identity_scheme,
        identity_scope=discovered.identity_scope,
        identity_value=discovered.identity_value,
        connection_target=discovered.connection_target,
        name=discovered.name,
        os_version=discovered.os_version,
        host_id=host.id,
        manufacturer=discovered.manufacturer or None,
        model=discovered.model or None,
        model_number=discovered.model_number or None,
        software_versions=discovered.software_versions or None,
        device_type=discovered.device_type or None,
        connection_type=discovered.connection_type or None,
        ip_address=discovered.ip_address or None,
    )


def _build_discovery_update_request(device: Device, discovered: DiscoveredDevice) -> DeviceVerificationUpdate:
    payload: dict[str, Any] = {
        "host_id": device.host_id,
    }
    if discovered.os_version and discovered.os_version != "unknown":
        payload["os_version"] = discovered.os_version
    if discovered.software_versions:
        payload["software_versions"] = discovered.software_versions
    return DeviceVerificationUpdate.model_validate(payload)
