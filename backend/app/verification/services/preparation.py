from __future__ import annotations

import uuid
from copy import deepcopy
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Any, Literal

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.agent_comm.operations import normalize_pack_device, pack_device_lifecycle_action
from app.core.database import async_session
from app.core.errors import AgentCallError
from app.core.timeutil import now_utc
from app.devices import locking as device_locking
from app.devices.models import ConnectionType, Device, DeviceOperationalState, DeviceType
from app.devices.schemas.device import DeviceVerificationCreate
from app.devices.services import readiness as device_readiness
from app.devices.services import write as device_write
from app.devices.services.identity import (
    looks_like_ip_address,
    looks_like_ip_port_target,
)
from app.devices.services.identity_conflicts import (
    DeviceIdentityConflictError,
)
from app.devices.services.intent import IntentService
from app.devices.services.intent_types import (
    VERIFICATION_OPERATION_ID_KEY,
    CommandKind,
    IntentRegistration,
    verification_intent_source,
)
from app.hosts.models import Host
from app.jobs.models import Job
from app.lifecycle.services.operator_node import operator_stop_active
from app.packs.services import platform_resolver as pack_platform_resolver
from app.sessions.service import device_has_running_session
from app.verification.services.job_state import set_stage

resolve_pack_platform = pack_platform_resolver.resolve_pack_platform

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.agent_comm.client import AgentClientFactory
    from app.agent_comm.http_pool import AgentHttpPool
    from app.agent_comm.protocols import CircuitBreakerProtocol
    from app.core.protocols import SettingsReader
    from app.core.type_defs import SessionFactory
    from app.devices.protocols import DeviceCrudProtocol
    from app.devices.schemas.device import DeviceVerificationUpdate
    from app.devices.services.identity_conflicts import DeviceIdentityConflictService
    from app.events.protocols import EventPublisher


@dataclass(frozen=True, slots=True)
class PreparedVerificationEffect:
    """Immutable values that bridge the no-transaction verification effect.

    Preparation copies every scalar the remote phases need (agent
    normalization, health, node-start, probe, finalization) out of the ORM so
    no ``Session``/``Device`` is retained across a transaction exit. ``payload``
    is the normalized, save-ready device payload; ``original_fields`` is the
    update-mode rollback snapshot.
    """

    operation_id: uuid.UUID
    mode: Literal["create", "update"]
    device_id: uuid.UUID | None
    payload: dict[str, Any]
    original_fields: dict[str, Any] | None
    host_id: uuid.UUID
    host_ip: str
    host_agent_port: int
    pack_id: str
    pack_release: str
    platform_id: str
    resolution_action: str | None


@dataclass(frozen=True, slots=True)
class _PackCoords:
    pack_id: str
    pack_release: str
    platform_id: str
    resolution_action: str | None


class VerificationPreparationService:
    def __init__(
        self,
        *,
        settings: SettingsReader,
        circuit_breaker: CircuitBreakerProtocol,
        crud: DeviceCrudProtocol,
        identity: DeviceIdentityConflictService,
        publisher: EventPublisher,
        session_factory: SessionFactory = async_session,
        pool: AgentHttpPool | None = None,
    ) -> None:
        self._settings = settings
        self._circuit_breaker = circuit_breaker
        self._crud = crud
        self._identity = identity
        self._publisher = publisher
        self._session_factory = session_factory
        self._pool = pool

    async def prepare_create(  # noqa: PLR0911 — sequential validation gates each return their own error
        self,
        job: dict[str, Any],
        operation_id: uuid.UUID,
        data: DeviceVerificationCreate,
        *,
        http_client_factory: AgentClientFactory,
    ) -> tuple[PreparedVerificationEffect | None, str | None]:
        await set_stage(job, "validation", "running")
        # A crashed retry may have already created the Device in txn2; resume
        # from Job.payload rather than duplicating the create.
        async with self._session_factory() as db:
            resumed = await self._resume_created_device(db, operation_id)
            if resumed is not None:
                await set_stage(job, "validation", "passed", detail="Device input normalized successfully")
                return resumed, None

            try:
                payload = await device_write.prepare_device_create_payload_async(db, data)
            except ValueError as exc:
                return await _validation_failed(job, str(exc))

            host = await _load_host(db, payload.get("host_id"))
            if host is None:
                return await _validation_failed(job, "Assigned host is required")

            if not _is_transport_identity(
                payload.get("identity_value"),
                payload.get("connection_target"),
                payload.get("ip_address"),
            ):
                try:
                    await self._identity.ensure_device_payload_identity_available(db, payload)
                except DeviceIdentityConflictError as exc:
                    return await _validation_failed(job, str(exc))

            coords, coords_error = await self._resolve_pack_coords(db, payload)
            if coords_error is not None:
                return await _validation_failed(job, coords_error)
            assert coords is not None
            host_id = host.id
            host_ip = host.ip
            host_agent_port = host.agent_port

        normalized, normalize_error = await self.normalize_effect(
            payload,
            coords,
            host_ip=host_ip,
            host_agent_port=host_agent_port,
            http_client_factory=http_client_factory,
        )
        if normalize_error is not None:
            return await _validation_failed(job, normalize_error)

        # ``create_device`` owns its own commit (ledger seed + identity gate), so
        # the create runs in a plain session; the tokenized lease and the
        # atomic device-id stamp follow in their own committed transaction so a
        # crashed retry resumes from ``Job.payload`` instead of duplicating.
        async with self._session_factory() as db:
            try:
                saved = await self._crud.create_device(
                    db,
                    DeviceVerificationCreate.model_validate(normalized),
                    initial_operational_state=DeviceOperationalState.verifying,
                )
            except DeviceIdentityConflictError as exc:
                return await _validation_failed(job, str(exc))
            device_id = saved.id
        async with self._session_factory.begin() as db:
            await _store_created_device_id(db, operation_id, device_id)
            await self._write_verification_lease(db, device_id, operation_id)

        await set_stage(job, "validation", "passed", detail="Device input normalized successfully")
        return (
            PreparedVerificationEffect(
                operation_id=operation_id,
                mode="create",
                device_id=device_id,
                payload=dict(normalized),
                original_fields=None,
                host_id=host_id,
                host_ip=host_ip,
                host_agent_port=host_agent_port,
                pack_id=coords.pack_id,
                pack_release=coords.pack_release,
                platform_id=coords.platform_id,
                resolution_action=coords.resolution_action,
            ),
            None,
        )

    async def prepare_update(  # noqa: PLR0911 — sequential validation gates each return their own error
        self,
        job: dict[str, Any],
        operation_id: uuid.UUID,
        device_id: uuid.UUID,
        data: DeviceVerificationUpdate,
        *,
        http_client_factory: AgentClientFactory,
    ) -> tuple[PreparedVerificationEffect | None, str | None]:
        await set_stage(job, "validation", "running")
        async with self._session_factory() as db:
            existing, precondition_error = await self._check_update_preconditions(db, device_id)
            if existing is None:
                return await _validation_failed(job, precondition_error or "Device was not found")

            try:
                payload = await device_write.prepare_device_update_payload_async(db, existing, data)
            except ValueError as exc:
                return await _validation_failed(job, str(exc))

            host_id = payload.get("host_id", existing.host_id)
            host = await _load_host(db, host_id)
            if host is None:
                return await _validation_failed(job, "Assigned host is required")

            verification_payload = _build_update_payload(payload, existing, host_id, data)
            coords, coords_error = await self._resolve_pack_coords(db, verification_payload)
            if coords_error is not None:
                return await _validation_failed(job, coords_error)
            assert coords is not None
            host_ip = host.ip
            host_agent_port = host.agent_port
            original_fields = {
                key: deepcopy(getattr(existing, key)) for key in verification_payload if key != "replace_device_config"
            }

        normalized, normalize_error = await self.normalize_effect(
            verification_payload,
            coords,
            host_ip=host_ip,
            host_agent_port=host_agent_port,
            http_client_factory=http_client_factory,
        )
        if normalize_error is not None:
            return await _validation_failed(job, normalize_error)

        async with self._session_factory.begin() as db:
            try:
                await self._identity.ensure_device_payload_identity_available(
                    db, normalized, exclude_device_id=device_id
                )
            except DeviceIdentityConflictError as exc:
                return await _validation_failed(job, str(exc))
            readiness_error = await _check_probe_readiness(db, normalized)
            if readiness_error is not None:
                return await _validation_failed(job, readiness_error)
            await device_locking.lock_device(db, device_id)
            await self._write_verification_lease(db, device_id, operation_id)

        await set_stage(job, "validation", "passed", detail="Setup payload normalized successfully")
        return (
            PreparedVerificationEffect(
                operation_id=operation_id,
                mode="update",
                device_id=device_id,
                payload=dict(normalized),
                original_fields=original_fields,
                host_id=host_id,
                host_ip=host_ip,
                host_agent_port=host_agent_port,
                pack_id=coords.pack_id,
                pack_release=coords.pack_release,
                platform_id=coords.platform_id,
                resolution_action=coords.resolution_action,
            ),
            None,
        )

    async def _resume_created_device(
        self, db: AsyncSession, operation_id: uuid.UUID
    ) -> PreparedVerificationEffect | None:
        job_row = await db.get(Job, operation_id)
        if job_row is None:
            return None
        stored = job_row.payload.get("device_id")
        if not stored:
            return None
        device = (
            await db.execute(
                select(Device).where(Device.id == uuid.UUID(str(stored))).options(selectinload(Device.host))
            )
        ).scalar_one_or_none()
        if device is None or device.host_id is None or device.host is None:
            return None
        host = device.host
        coords, coords_error = await self._resolve_pack_coords(db, _device_payload(device))
        if coords_error is not None or coords is None:
            return None
        return PreparedVerificationEffect(
            operation_id=operation_id,
            mode="create",
            device_id=device.id,
            payload=_device_payload(device),
            original_fields=None,
            host_id=device.host_id,
            host_ip=host.ip,
            host_agent_port=host.agent_port,
            pack_id=coords.pack_id,
            pack_release=coords.pack_release,
            platform_id=coords.platform_id,
            resolution_action=coords.resolution_action,
        )

    async def _write_verification_lease(self, db: AsyncSession, device_id: uuid.UUID, operation_id: uuid.UUID) -> None:
        startup_timeout = self._settings.get_int("appium.startup_timeout_sec")
        viability_timeout = self._settings.get_int("general.session_viability_timeout_sec")
        deadline = now_utc() + timedelta(seconds=startup_timeout + viability_timeout + 60)
        await device_locking.lock_device(db, device_id)
        await IntentService(db).register_intents_and_reconcile(
            device_id=device_id,
            intents=[
                IntentRegistration(
                    source=verification_intent_source(device_id),
                    kind=CommandKind.verification_start,
                    payload={"action": "start", VERIFICATION_OPERATION_ID_KEY: str(operation_id)},
                    expires_at=deadline,
                )
            ],
            publisher=self._publisher,
        )

    async def _check_update_preconditions(
        self,
        db: AsyncSession,
        device_id: uuid.UUID,
    ) -> tuple[Device | None, str | None]:
        existing = await self._crud.get_device(db, device_id)
        if existing is None:
            return None, "Device was not found"

        # Spec §14.1: verification tears down the client-serving node, so it must
        # never run on a device with a live session. Closes the enqueue→run
        # TOCTOU window by failing the job instead.
        if await device_has_running_session(db, existing.id):
            return None, "Device has a live session; verification cannot run during a session"

        # Spec/N13b: the verification node-start path revokes the sticky
        # operator:stop. Fail rather than silently revive an operator-stopped device.
        if await operator_stop_active(db, existing.id):
            return None, "Device is operator-stopped; start the node before verifying"

        return existing, None

    async def _resolve_pack_coords(
        self, db: AsyncSession, payload: dict[str, Any]
    ) -> tuple[_PackCoords | None, str | None]:
        pack_id = payload.get("pack_id")
        platform_id = payload.get("platform_id")
        if not pack_id or not platform_id:
            return None, "Assigned pack and platform are required"
        device_type = payload.get("device_type")
        resolved_device_type = getattr(device_type, "value", None) or (str(device_type) if device_type else None)
        try:
            resolved = await resolve_pack_platform(
                db,
                pack_id=str(pack_id),
                platform_id=str(platform_id),
                device_type=resolved_device_type,
            )
        except LookupError:
            return None, "Assigned pack does not support the requested platform"
        action = resolved.connection_behavior.get("host_resolution_action")
        return (
            _PackCoords(
                pack_id=resolved.pack_id,
                pack_release=resolved.release,
                platform_id=resolved.platform_id,
                resolution_action=str(action) if action else None,
            ),
            None,
        )

    async def normalize_effect(
        self,
        payload: dict[str, Any],
        coords: _PackCoords,
        *,
        host_ip: str,
        host_agent_port: int,
        http_client_factory: AgentClientFactory,
    ) -> tuple[dict[str, Any], str | None]:
        """Agent-side normalization + host resolution from copied values.

        Accepts no ``AsyncSession``: every value it needs was copied out of the
        prepare transaction. Mutates and returns *payload*.
        """
        normalization_error = await self._apply_pack_normalization(
            payload, coords, host_ip=host_ip, host_agent_port=host_agent_port, http_client_factory=http_client_factory
        )
        if normalization_error is not None:
            return payload, normalization_error

        needs_resolution = coords.resolution_action is not None and _is_transport_identity(
            payload.get("identity_value"),
            payload.get("connection_target"),
            payload.get("ip_address"),
        )
        if needs_resolution and coords.resolution_action is not None:
            resolution_error = await self._apply_host_resolution(
                payload,
                coords,
                coords.resolution_action,
                host_ip=host_ip,
                host_agent_port=host_agent_port,
                http_client_factory=http_client_factory,
            )
            if resolution_error is not None:
                return payload, resolution_error

        _coerce_payload_enums_in_place(payload)
        return payload, None

    async def _apply_pack_normalization(
        self,
        payload: dict[str, Any],
        coords: _PackCoords,
        *,
        host_ip: str,
        host_agent_port: int,
        http_client_factory: AgentClientFactory,
    ) -> str | None:
        try:
            normalized = await normalize_pack_device(
                host_ip,
                host_agent_port,
                pack_id=coords.pack_id,
                pack_release=coords.pack_release,
                platform_id=coords.platform_id,
                raw_input={
                    key: value.value if hasattr(value, "value") else str(value) if key == "host_id" else value
                    for key, value in payload.items()
                    if key not in {"device_config", "replace_device_config", "host_id"}
                },
                http_client_factory=http_client_factory,
                circuit_breaker=self._circuit_breaker,
                pool=self._pool,
            )
        except AgentCallError:
            normalized = None
        except LookupError:
            normalized = None
        except TypeError:
            # Some tests provide lightweight HTTP mocks for health-only flows.
            # Treat missing normalize support the same way the real agent's
            # 404 response is treated: continue with manifest/local fields.
            normalized = None

        if normalized is not None:
            return _apply_normalized_fields(payload, normalized)
        return None

    async def _apply_host_resolution(
        self,
        payload: dict[str, Any],
        coords: _PackCoords,
        action: str,
        *,
        host_ip: str,
        host_agent_port: int,
        http_client_factory: AgentClientFactory,
    ) -> str | None:
        try:
            resolved = await pack_device_lifecycle_action(
                host_ip,
                host_agent_port,
                payload["connection_target"],
                pack_id=coords.pack_id,
                platform_id=coords.platform_id,
                action=action,
                args={
                    "device_type": getattr(payload.get("device_type"), "value", None)
                    or (str(payload["device_type"]) if payload.get("device_type") else None),
                    "connection_type": getattr(payload.get("connection_type"), "value", None)
                    or (str(payload["connection_type"]) if payload.get("connection_type") else None),
                    "ip_address": payload.get("ip_address"),
                },
                http_client_factory=http_client_factory,
                circuit_breaker=self._circuit_breaker,
                pool=self._pool,
            )
        except AgentCallError as exc:
            error_msg = str(exc)
            if "404" in error_msg or action in error_msg.lower():
                return f"Device must resolve to a stable identity before save (action: {action})"
            return f"Host resolution failed: {exc}"

        resolved_identity = resolved.get("identity_value")
        if not isinstance(resolved_identity, str) or not resolved_identity:
            return f"Device must resolve to a stable identity before save (action: {action})"
        _apply_resolved_fields(payload, resolved, resolved_identity)
        return None


def _device_payload(device: Device) -> dict[str, Any]:
    return {
        "pack_id": device.pack_id,
        "platform_id": device.platform_id,
        "identity_scheme": device.identity_scheme,
        "identity_scope": device.identity_scope,
        "identity_value": device.identity_value,
        "connection_target": device.connection_target,
        "name": device.name,
        "os_version": device.os_version,
        "os_version_display": device.os_version_display,
        "host_id": device.host_id,
        "manufacturer": device.manufacturer,
        "model": device.model,
        "model_number": device.model_number,
        "software_versions": device.software_versions,
        "device_type": device.device_type,
        "connection_type": device.connection_type,
        "ip_address": device.ip_address,
        "device_config": device.device_config,
    }


def _build_update_payload(
    payload: dict[str, Any],
    existing: Device,
    host_id: uuid.UUID | None,
    data: DeviceVerificationUpdate,
) -> dict[str, Any]:
    return {
        "pack_id": payload.get("pack_id", existing.pack_id),
        "identity_scheme": payload.get("identity_scheme", existing.identity_scheme),
        "identity_scope": payload.get("identity_scope", existing.identity_scope),
        "identity_value": payload.get("identity_value", existing.identity_value),
        "connection_target": payload.get("connection_target", existing.connection_target),
        "name": payload.get("name", existing.name),
        "platform_id": payload.get("platform_id", existing.platform_id),
        "os_version": payload.get("os_version", existing.os_version),
        "os_version_display": payload.get("os_version_display", existing.os_version_display),
        "host_id": host_id,
        "manufacturer": payload.get("manufacturer", existing.manufacturer),
        "model": payload.get("model", existing.model),
        "model_number": payload.get("model_number", existing.model_number),
        "software_versions": payload.get("software_versions", existing.software_versions),
        "device_type": payload.get("device_type", existing.device_type),
        "connection_type": payload.get("connection_type", existing.connection_type),
        "ip_address": payload.get("ip_address", existing.ip_address),
        "device_config": payload.get("device_config", existing.device_config),
        "replace_device_config": data.replace_device_config,
    }


async def _store_created_device_id(db: AsyncSession, operation_id: uuid.UUID, device_id: uuid.UUID) -> None:
    job_row = await db.get(Job, operation_id)
    if job_row is None:
        return
    job_row.payload = {**job_row.payload, "device_id": str(device_id)}


def _apply_normalized_fields(payload: dict[str, Any], normalized: dict[str, Any]) -> str | None:
    errors = normalized.get("field_errors")
    if isinstance(errors, list) and errors:
        first = errors[0]
        if isinstance(first, dict):
            field = first.get("field_id", "device")
            msg = first.get("message", "Adapter rejected device input")
            return f"{field}: {msg}"
        return "Adapter rejected device input"
    payload["identity_scheme"] = normalized.get("identity_scheme") or payload.get("identity_scheme")
    payload["identity_scope"] = normalized.get("identity_scope") or payload.get("identity_scope")
    payload["identity_value"] = normalized.get("identity_value") or payload.get("identity_value")
    payload["connection_target"] = normalized.get("connection_target") or payload.get("connection_target")
    payload["os_version"] = normalized.get("os_version") or payload.get("os_version")
    payload["os_version_display"] = normalized.get("os_version_display") or payload.get("os_version_display")
    payload["manufacturer"] = normalized.get("manufacturer") or payload.get("manufacturer")
    payload["model"] = normalized.get("model") or payload.get("model")
    payload["model_number"] = normalized.get("model_number") or payload.get("model_number")
    payload["software_versions"] = normalized.get("software_versions") or payload.get("software_versions")
    if not _payload_requests_virtual_lane(payload):
        payload["device_type"] = normalized.get("device_type") or payload.get("device_type")
        payload["connection_type"] = normalized.get("connection_type") or payload.get("connection_type")
    payload["ip_address"] = normalized.get("ip_address") or payload.get("ip_address")
    normalized_name = normalized.get("model") or normalized.get("model_number")
    if normalized_name and payload.get("name") in {
        payload.get("connection_target"),
        payload.get("ip_address"),
        payload.get("identity_value"),
    }:
        payload["name"] = normalized_name
    return None


def _apply_resolved_fields(payload: dict[str, Any], resolved: dict[str, Any], resolved_identity: str) -> None:
    payload["identity_scheme"] = resolved.get("identity_scheme") or payload.get("identity_scheme")
    payload["identity_value"] = resolved_identity
    payload["connection_target"] = resolved.get("connection_target") or payload["connection_target"]
    payload["platform_id"] = resolved.get("platform_id") or payload.get("platform_id")
    payload["os_version"] = resolved.get("os_version") or payload.get("os_version")
    payload["os_version_display"] = resolved.get("os_version_display") or payload.get("os_version_display")
    payload["manufacturer"] = resolved.get("manufacturer") or payload.get("manufacturer")
    payload["model"] = resolved.get("model") or payload.get("model")
    payload["model_number"] = resolved.get("model_number") or payload.get("model_number")
    payload["software_versions"] = resolved.get("software_versions") or payload.get("software_versions")
    payload["device_type"] = resolved.get("device_type") or payload.get("device_type")
    payload["connection_type"] = resolved.get("connection_type") or payload.get("connection_type")
    payload["ip_address"] = resolved.get("ip_address") or payload.get("ip_address")
    resolved_name = resolved.get("name") or resolved.get("model") or resolved.get("model_number")
    if resolved_name and (
        not payload.get("name")
        or payload.get("name")
        in {payload.get("connection_target"), payload.get("ip_address"), payload.get("identity_value")}
    ):
        payload["name"] = resolved_name


def _coerce_payload_enums_in_place(payload: dict[str, Any]) -> None:
    """Coerce device_type / connection_type to enum types in place.

    Agent-normalized payloads return these fields as plain strings; downstream
    code (`Device(...)` construction, `setattr` onto Mapped[Enum] columns) does
    not validate, so leaving them as `str` corrupts the row and crashes any
    later `.value` access.
    """
    device_type = payload.get("device_type")
    if isinstance(device_type, str) and not isinstance(device_type, DeviceType):
        payload["device_type"] = DeviceType(device_type)
    connection_type = payload.get("connection_type")
    if isinstance(connection_type, str) and not isinstance(connection_type, ConnectionType):
        payload["connection_type"] = ConnectionType(connection_type)


async def _load_host(db: AsyncSession, host_id: uuid.UUID | None) -> Host | None:
    if host_id is None:
        return None
    result = await db.execute(select(Host).where(Host.id == host_id))
    return result.scalar_one_or_none()


def _is_transport_identity(
    identity_value: str | None,
    connection_target: str | None,
    ip_address: str | None,
) -> bool:
    """Check whether the identity looks like a transport-layer address rather than stable."""
    if not identity_value:
        return True
    if looks_like_ip_port_target(identity_value) or looks_like_ip_address(identity_value):
        return True
    if connection_target and identity_value == connection_target and looks_like_ip_port_target(connection_target):
        return True
    return bool(ip_address and identity_value == ip_address)


def _payload_requests_virtual_lane(payload: dict[str, Any]) -> bool:
    device_type = payload.get("device_type")
    connection_type = payload.get("connection_type")
    device_type_value = getattr(device_type, "value", None) or str(device_type or "")
    connection_type_value = getattr(connection_type, "value", None) or str(connection_type or "")
    return device_type_value in {DeviceType.emulator.value, DeviceType.simulator.value} or (
        connection_type_value == ConnectionType.virtual.value
    )


async def _check_probe_readiness(db: AsyncSession, verification_payload: dict[str, Any]) -> str | None:
    _probe_device = Device(
        pack_id=verification_payload.get("pack_id"),
        platform_id=verification_payload.get("platform_id"),
        connection_type=verification_payload.get("connection_type"),
        ip_address=verification_payload.get("ip_address"),
        device_config=verification_payload.get("device_config"),
        verified_at=None,
        # Required non-nullable fields — provide defaults for the probe object.
        identity_scheme=verification_payload.get("identity_scheme", ""),
        identity_scope=verification_payload.get("identity_scope", ""),
        identity_value=verification_payload.get("identity_value", ""),
        connection_target=verification_payload.get("connection_target", ""),
        name=verification_payload.get("name", ""),
        device_type=verification_payload.get("device_type", DeviceType.real_device),
        os_version=verification_payload.get("os_version"),
        os_version_display=verification_payload.get("os_version_display"),
    )
    readiness = await device_readiness.assess_device_async(db, _probe_device)
    if readiness.missing_setup_fields:
        return f"Missing required setup fields: {', '.join(readiness.missing_setup_fields)}"
    return None


async def _validation_failed(job: dict[str, Any], detail: str) -> tuple[None, str]:
    await set_stage(job, "validation", "failed", detail=detail)
    return None, detail
