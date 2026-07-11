from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from app.devices.models import Device
from app.hosts.models import Host, HostStatus

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.core.protocols import SettingsReader
    from app.events.catalog import EventSeverity
    from app.events.protocols import EventPublisher
    from app.hosts.schemas import HostCreate, HostHardwareInfo, HostRegister

_LEGACY_GLOBAL_TOOL_KEYS = {"appium"}
MIN_ORCHESTRATION_CONTRACT_VERSION = 6
# Fallback for hosts created without a port; enrollment overwrites it with the
# agent's real AGENT_AGENT_PORT on the first registration refresh.
DEFAULT_AGENT_PORT = 5100


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
    def __init__(self, *, publisher: EventPublisher, settings: SettingsReader) -> None:
        self._publisher: EventPublisher = publisher
        self._settings: SettingsReader = settings

    async def create_host(self, db: AsyncSession, data: HostCreate) -> Host:
        payload = data.model_dump()
        payload["agent_port"] = payload["agent_port"] or DEFAULT_AGENT_PORT
        host = Host(**payload)
        db.add(host)
        await db.commit()
        await db.refresh(host)
        return host

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

    async def delete_host(self, db: AsyncSession, host_id: uuid.UUID) -> bool:
        host = await self.get_host(db, host_id)
        if host is None:
            return False
        if host.devices:
            raise ValueError("Cannot delete host while devices are still assigned")
        await db.delete(host)
        await db.commit()
        return True

    async def _apply_reregister(self, db: AsyncSession, host: Host, data: HostRegister) -> Host:
        host.ip = data.ip
        host.os_type = data.os_type
        if data.agent_port is not None:
            host.agent_port = data.agent_port
        # agent_version / capabilities are push-owned runtime facts; registration
        # never writes them (capabilities is only the 426 gate input above).
        _apply_host_info(host, data.host_info)
        await db.commit()
        await db.refresh(host)
        return host

    async def register_host(self, db: AsyncSession, data: HostRegister) -> tuple[Host, bool]:
        """Register or re-register a host. Returns (host, is_new)."""
        validate_orchestration_contract(data.capabilities, host_label=data.hostname)
        stmt = select(Host).where(Host.hostname == data.hostname)
        result = await db.execute(stmt)
        host = result.scalar_one_or_none()

        if host is not None:
            return await self._apply_reregister(db, host, data), False

        # New registration
        status = HostStatus.online if self._settings.get("agent.auto_accept_hosts") else HostStatus.pending
        agent_port = data.agent_port or DEFAULT_AGENT_PORT
        host = Host(
            hostname=data.hostname,
            ip=data.ip,
            os_type=data.os_type,
            agent_port=agent_port,
            status=status,
        )
        _apply_host_info(host, data.host_info)
        db.add(host)
        try:
            await db.flush()
        except IntegrityError:
            # A concurrent peer (e.g. a heartbeat-driven re-register racing the
            # operator-initiated registration) committed the same hostname
            # between our unlocked SELECT and our INSERT. Drop the
            # half-written transient row, refetch the existing host, and
            # degrade to the re-register branch.
            await db.rollback()
            result = await db.execute(select(Host).where(Host.hostname == data.hostname))
            existing = result.scalar_one_or_none()
            if existing is None:
                raise
            return await self._apply_reregister(db, existing, data), False
        self._publisher.queue_for_session(
            db,
            "host.registered",
            {
                "host_id": str(host.id),
                "hostname": host.hostname,
                "status": host.status.value,
            },
        )
        await db.commit()
        await db.refresh(host)
        return host, True

    async def approve_host(self, db: AsyncSession, host_id: uuid.UUID) -> Host | None:
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
        await db.commit()
        await db.refresh(host)
        return host

    async def reject_host(self, db: AsyncSession, host_id: uuid.UUID) -> bool:
        """Reject a pending host (deletes it). Returns False if not found or not pending."""
        stmt = select(Host).where(Host.id == host_id).with_for_update()
        result = await db.execute(stmt)
        host = result.scalar_one_or_none()
        if host is None or host.status != HostStatus.pending:
            return False
        await db.delete(host)
        await db.commit()
        return True
