from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime  # Pydantic needs the runtime type.
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.devices.models import Device
from app.devices.services.groups import constraint_name as integrity_constraint_name
from app.hosts.models import Host, HostStatus, OSType  # Pydantic needs the runtime types.

if TYPE_CHECKING:
    from sqlalchemy.exc import IntegrityError
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.core.protocols import SettingsReader
    from app.events.catalog import EventSeverity
    from app.events.protocols import EventPublisher
    from app.hosts.schemas import HostCreate, HostHardwareInfo, HostRegister

_LEGACY_GLOBAL_TOOL_KEYS = {"appium"}
MIN_ORCHESTRATION_CONTRACT_VERSION = 7
# Fallback for hosts created without a port; enrollment overwrites it with the
# agent's real AGENT_AGENT_PORT on the first registration refresh.
DEFAULT_AGENT_PORT = 5100
# ``Host.hostname`` is declared unique=True, index=True, and the Postgres naming
# convention ("ix": "%(column_0_label)s_idx") renders that index as
# ``hosts_hostname_idx``. Registration discriminates on this name so an
# unrelated integrity failure propagates instead of degrading to a re-register.
# ``test_hostname_conflict_constant_matches_the_live_database`` pins the value
# against what the running database actually reports.
HOSTNAME_UNIQUE_INDEX = "hosts_hostname_idx"


def is_hostname_conflict(exc: IntegrityError) -> bool:
    return integrity_constraint_name(exc) == HOSTNAME_UNIQUE_INDEX


class HostCommandSnapshot(BaseModel):
    """Immutable projection of the Host columns a router response reads.

    A host command's transaction is closed by the time its route serialises, so
    the response is built from this rather than from the ORM row the command
    session owned. It carries exactly what ``_serialize_host`` touches — the
    ``HostRead`` column set plus the three liveness facts — and no relationship.
    """

    model_config = ConfigDict(frozen=True, from_attributes=True)

    id: uuid.UUID
    hostname: str
    ip: str
    os_type: OSType
    agent_port: int
    status: HostStatus
    agent_version: str | None
    capabilities: dict[str, Any] | None
    tool_env: dict[str, str] | None
    missing_prerequisites: list[str]
    last_heartbeat: datetime | None
    created_at: datetime
    os_version: str | None
    kernel_version: str | None
    cpu_arch: str | None
    cpu_model: str | None
    cpu_cores: int | None
    total_memory_mb: int | None
    total_disk_gb: int | None


@dataclass(frozen=True, slots=True)
class HostTarget:
    """The only value a doctor / tool-status / discovery remote call receives.

    Scalars, copied inside a short transaction that then ends: no agent call in
    this domain ever runs while a session is open or a row lock is held.
    ``current_boot_id`` travels with it so a write phase can recheck that the
    boot which produced the remote payload is still the registered one.
    """

    host_id: uuid.UUID
    hostname: str
    ip: str
    agent_port: int
    current_boot_id: uuid.UUID | None

    @classmethod
    def from_host(cls, host: Host) -> HostTarget:
        return cls(
            host_id=host.id,
            hostname=host.hostname,
            ip=host.ip,
            agent_port=host.agent_port,
            current_boot_id=host.current_boot_id,
        )


def _apply_host_info(host: Host, host_info: HostHardwareInfo | None) -> None:
    if host_info is None:
        return
    for field, value in host_info.model_dump(exclude_none=True).items():
        assert hasattr(Host, field), f"HostHardwareInfo field {field!r} not on Host model"
        setattr(host, field, value)


def _host_status_severity(old_status: str | None, new_status: str) -> EventSeverity:
    if new_status == "offline" and old_status not in (None, "offline"):
        return "warning"
    if new_status == "online" and old_status not in (None, "online"):
        return "success"
    return "info"


def _coerce_missing_prerequisites(value: object) -> list[str] | None:
    if not isinstance(value, list):
        return None
    missing: list[str] = []
    for item in value:
        if isinstance(item, str) and item not in missing:
            missing.append(item)
    return missing


def normalize_capabilities(capabilities: dict[str, Any] | None) -> dict[str, Any] | None:
    if capabilities is None:
        return None
    normalized = dict(capabilities)
    tools = normalized.get("tools")
    if isinstance(tools, dict):
        normalized["tools"] = {name: version for name, version in tools.items() if name not in _LEGACY_GLOBAL_TOOL_KEYS}
    if "missing_prerequisites" in normalized:
        missing = _coerce_missing_prerequisites(normalized["missing_prerequisites"])
        normalized["missing_prerequisites"] = missing or []
    return normalized


def orchestration_contract_version(capabilities: dict[str, Any] | None) -> int | None:
    if capabilities is None:
        return None
    value = capabilities.get("orchestration_contract_version")
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def validate_orchestration_contract(capabilities: dict[str, Any] | None, *, host_label: str) -> None:
    version = orchestration_contract_version(capabilities)
    if version is None or version < MIN_ORCHESTRATION_CONTRACT_VERSION:
        raise ValueError(
            f"Host {host_label} reports unsupported orchestration contract; "
            f"expected orchestration_contract_version >= {MIN_ORCHESTRATION_CONTRACT_VERSION}"
        )


def update_missing_prerequisites_from_health(host: Host, missing_prerequisites: object) -> None:
    missing = _coerce_missing_prerequisites(missing_prerequisites)
    if missing is None:
        return
    capabilities = normalize_capabilities(dict(host.capabilities or {})) or {}
    capabilities["missing_prerequisites"] = missing
    host.capabilities = capabilities


class HostCrudService:
    """Transaction-local host mutators. Every method assumes the caller's
    transaction is already open and leaves the boundary to it: mutate, flush,
    queue events, and return an immutable snapshot built before the caller's
    transaction ends."""

    def __init__(self, *, publisher: EventPublisher, settings: SettingsReader) -> None:
        self._publisher: EventPublisher = publisher
        self._settings: SettingsReader = settings

    async def create_host(self, db: AsyncSession, data: HostCreate) -> HostCommandSnapshot:
        payload = data.model_dump()
        payload["agent_port"] = payload["agent_port"] or DEFAULT_AGENT_PORT
        host = Host(**payload)
        db.add(host)
        await db.flush()
        return HostCommandSnapshot.model_validate(host)

    async def list_hosts(self, db: AsyncSession) -> list[Host]:
        stmt = select(Host).order_by(Host.hostname)
        result = await db.execute(stmt)
        return list(result.scalars().all())

    async def get_host(self, db: AsyncSession, host_id: uuid.UUID) -> Host | None:
        stmt = (
            select(Host).where(Host.id == host_id).options(selectinload(Host.devices).selectinload(Device.appium_node))
        )
        result = await db.execute(stmt)
        return result.scalar_one_or_none()

    async def load_host_target(self, db: AsyncSession, host_id: uuid.UUID) -> HostTarget | None:
        """Copy the scalars a remote call needs, so the session can close first."""
        host = (await db.execute(select(Host).where(Host.id == host_id))).scalar_one_or_none()
        return None if host is None else HostTarget.from_host(host)

    async def delete_host(self, db: AsyncSession, host_id: uuid.UUID) -> bool:
        # Lock before the dependent read so a device cannot be attached between
        # the check and the delete. ``devices`` stays eager-loaded: the
        # relationship cascades in Python, and an unloaded collection would be
        # lazy-loaded during flush.
        stmt = (
            select(Host)
            .where(Host.id == host_id)
            .options(selectinload(Host.devices))
            .with_for_update(of=Host)
            .execution_options(populate_existing=True)
        )
        host = (await db.execute(stmt)).scalar_one_or_none()
        if host is None:
            return False
        if host.devices:
            raise ValueError("Cannot delete host while devices are still assigned")
        await db.delete(host)
        await db.flush()
        return True

    def _apply_reregister(self, host: Host, data: HostRegister) -> None:
        host.ip = data.ip
        host.os_type = data.os_type
        if data.agent_port is not None:
            host.agent_port = data.agent_port
        # Boot fence: a re-registering agent that carries a boot_id supersedes the
        # previous boot, so an in-flight push from the old boot is fenced. A legacy
        # agent (no boot_id) leaves the fence untouched (mixed-version safe).
        #
        # Diagnosing a surprise rotation: a fence rotation has been observed
        # landing with no matching access-log line behind it, which reads as a
        # phantom write. A client disconnecting after the handler commits but
        # before the response is sent is one way to get there — FastAPI's
        # TestClient is one such short-lived client. Look for a short-lived
        # client or a test suite on that host, not only for a running agent.
        if data.boot_id is not None:
            host.current_boot_id = data.boot_id
        # agent_version / capabilities are push-owned runtime facts; registration
        # never writes them (capabilities is only the 426 gate input above).
        _apply_host_info(host, data.host_info)

    async def register_host(self, db: AsyncSession, data: HostRegister) -> tuple[HostCommandSnapshot, bool]:
        """One registration attempt. Returns (snapshot, is_new).

        The insert can still lose a race to a concurrent peer that commits the
        same hostname; that ``IntegrityError`` leaves the caller's transaction
        unusable, so it propagates and the caller retries through
        :meth:`reregister_host` on a *fresh* transaction.

        ``hosts/router.py`` also calls :func:`validate_orchestration_contract`
        ahead of both this attempt and the conflict fallback — deliberately, as
        defence-in-depth for direct callers of this method. Neither copy guards
        the other; do not delete one on the assumption that it does.
        """
        validate_orchestration_contract(data.capabilities, host_label=data.hostname)
        # FOR UPDATE: the boot-fence write below must serialize against a
        # concurrent status push for the same host (which also locks the row),
        # so the fence check and its update cannot interleave.
        stmt = select(Host).where(Host.hostname == data.hostname).with_for_update()
        result = await db.execute(stmt)
        host = result.scalar_one_or_none()

        if host is not None:
            self._apply_reregister(host, data)
            await db.flush()
            return HostCommandSnapshot.model_validate(host), False

        # New registration
        status = HostStatus.online if self._settings.get("agent.auto_accept_hosts") else HostStatus.pending
        agent_port = data.agent_port or DEFAULT_AGENT_PORT
        host = Host(
            hostname=data.hostname,
            ip=data.ip,
            os_type=data.os_type,
            agent_port=agent_port,
            status=status,
            current_boot_id=data.boot_id,
        )
        _apply_host_info(host, data.host_info)
        db.add(host)
        await db.flush()
        self._publisher.queue_for_session(
            db,
            "host.registered",
            {
                "host_id": str(host.id),
                "hostname": host.hostname,
                "status": host.status.value,
            },
        )
        return HostCommandSnapshot.model_validate(host), True

    async def reregister_host(self, db: AsyncSession, data: HostRegister) -> HostCommandSnapshot | None:
        """Reapply a registration onto the row a racing peer won.

        Runs in a transaction the caller opened *after* the losing attempt's
        transaction was fully rolled back and closed. The lock is retaken here:
        nothing carries over from the failed attempt. ``None`` means the winner
        has since disappeared, which the caller reports as the same conflict.

        PRECONDITION: *data* has already passed
        :func:`validate_orchestration_contract`. This method does not re-run the
        426 gate — it is only reachable after an attempt on the same payload
        cleared it, and ``hosts/router.py`` runs the gate before either
        transaction so that ordering is visible at the call site rather than
        implied here.
        """
        stmt = select(Host).where(Host.hostname == data.hostname).with_for_update()
        host = (await db.execute(stmt)).scalar_one_or_none()
        if host is None:
            return None
        self._apply_reregister(host, data)
        await db.flush()
        return HostCommandSnapshot.model_validate(host)

    async def approve_host(self, db: AsyncSession, host_id: uuid.UUID) -> HostCommandSnapshot | None:
        """Approve a pending host. Returns None if not found or not pending."""
        # Acquire SELECT ... FOR UPDATE so a concurrent reject_host (which
        # deletes the row) cannot land between the predicate check and the
        # commit. Without the lock, the UPDATE on a deleted row affects zero
        # rows but ``return host`` would still hand the caller a phantom
        # success.
        stmt = select(Host).where(Host.id == host_id).with_for_update()
        result = await db.execute(stmt)
        host = result.scalar_one_or_none()
        if host is None or host.status != HostStatus.pending:
            return None
        old_status = host.status.value
        host.status = HostStatus.online
        self._publisher.queue_for_session(
            db,
            "host.status_changed",
            {
                "host_id": str(host.id),
                "hostname": host.hostname,
                "old_status": old_status,
                "new_status": "online",
            },
            severity=_host_status_severity(old_status, "online"),
        )
        await db.flush()
        return HostCommandSnapshot.model_validate(host)

    async def reject_host(self, db: AsyncSession, host_id: uuid.UUID) -> bool:
        """Reject a pending host (deletes it). Returns False if not found or not pending."""
        stmt = select(Host).where(Host.id == host_id).with_for_update()
        result = await db.execute(stmt)
        host = result.scalar_one_or_none()
        if host is None or host.status != HostStatus.pending:
            return False
        await db.delete(host)
        await db.flush()
        return True

    async def update_tool_env(self, db: AsyncSession, host_id: uuid.UUID, env: dict[str, str]) -> dict[str, str]:
        """Replace the per-host tool environment under the Host row lock.

        Raises ``NoResultFound`` when the host is gone; the route translates it.
        """
        stmt = select(Host).where(Host.id == host_id).with_for_update()
        host = (await db.execute(stmt)).scalar_one()
        host.tool_env = env or None
        await db.flush()
        return dict(host.tool_env or {})
