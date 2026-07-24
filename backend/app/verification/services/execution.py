from __future__ import annotations

import asyncio
import logging
import time
from copy import deepcopy
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from app.agent_comm.node_poke import NodeRefreshTarget, poke_node_refresh_target
from app.agent_comm.operations import pack_device_health as fetch_pack_device_health
from app.agent_comm.probe_result import ProbeResult
from app.appium_nodes.exceptions import NodeManagerError
from app.appium_nodes.models import AppiumDesiredState, AppiumNode
from app.appium_nodes.services.desired_state_writer import DesiredStateWrite, write_desired_state
from app.core.database import async_session
from app.core.errors import AgentCallError
from app.core.timeutil import now_utc
from app.devices import locking as device_locking
from app.devices.locking import lock_device_handle
from app.devices.models.intent import DeviceIntent
from app.devices.services.claims import device_has_live_session
from app.devices.services.intent import IntentService
from app.devices.services.intent_types import (
    VERIFICATION_OPERATION_ID_KEY,
    VERIFICATION_OUTCOME_FAILED,
    VERIFICATION_OUTCOME_KEY,
    VERIFICATION_OUTCOME_PASSED,
    verification_intent_source,
)
from app.grid.allocation import node_target
from app.lifecycle.services import remediation_log
from app.lifecycle.services.operator_node import operator_start_source, operator_stop_sources
from app.sessions.service_probes import (
    ProbeSource,
    claim_probe_session,
    confirm_probe_session,
    finalize_probe_session,
)
from app.sessions.service_viability import build_probe_capabilities, grid_probe_response_to_result
from app.sessions.viability_types import SessionViabilityCheckedBy, SessionViabilityProbeInProgressError
from app.verification.services.job_state import set_stage

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.agent_comm.client import AgentClientFactory
    from app.agent_comm.http_pool import AgentHttpPool
    from app.agent_comm.protocols import CircuitBreakerProtocol
    from app.core.protocols import SettingsReader
    from app.core.type_defs import SessionFactory
    from app.devices.locking import LockedDevice
    from app.devices.models import Device
    from app.devices.protocols import (
        DeviceCapabilityProtocol,
        DeviceCrudProtocol,
        NodeConvergence,
        RemoteNodeManager,
        ReviewProtocol,
        SessionViabilityProbe,
    )
    from app.events.protocols import EventPublisher
    from app.verification.services.preparation import PreparedVerificationEffect

logger = logging.getLogger(__name__)


@dataclass
class VerificationExecutionOutcome:
    status: str
    error: str | None = None
    device_id: str | None = None
    superseded: bool = False


@dataclass(frozen=True, slots=True)
class NodeEffectSnapshot:
    node_id: uuid.UUID
    active_connection_target: str | None


@dataclass(frozen=True, slots=True)
class AgentCallContext:
    """Transport plumbing the verification flow forwards to agent-comm operations."""

    settings: SettingsReader
    circuit_breaker: CircuitBreakerProtocol
    pool: AgentHttpPool | None = None


@dataclass(frozen=True, slots=True)
class FailureFinalizers:
    """Collaborators the verification failure-finalization path drives together."""

    crud: DeviceCrudProtocol
    review: ReviewProtocol


async def lock_verification_operation(
    db: AsyncSession,
    *,
    device_id: uuid.UUID,
    operation_id: uuid.UUID,
) -> tuple[LockedDevice, DeviceIntent] | None:
    """Lock the device and its verification lease, fenced by the operation token.

    Returns ``None`` when the lease is absent or its ``operation_id`` no longer
    matches — the caller has been superseded by a newer verification and must
    make no writes.
    """
    locked = await lock_device_handle(db, device_id)
    lease = await db.scalar(
        select(DeviceIntent)
        .where(
            DeviceIntent.device_id == device_id,
            DeviceIntent.source == verification_intent_source(device_id),
        )
        .with_for_update()
    )
    if lease is None or lease.payload.get(VERIFICATION_OPERATION_ID_KEY) != str(operation_id):
        return None
    return locked, lease


class VerificationExecutionService:
    def __init__(  # noqa: PLR0913 — cohesive verification-execution collaborators
        self,
        *,
        publisher: EventPublisher,
        agent: AgentCallContext,
        crud: DeviceCrudProtocol,
        viability: SessionViabilityProbe,
        capability: DeviceCapabilityProtocol,
        reconciler: NodeConvergence,
        node_manager: RemoteNodeManager,
        review: ReviewProtocol,
        session_factory: SessionFactory = async_session,
    ) -> None:
        self._publisher = publisher
        self._agent = agent
        self._crud = crud
        self._viability = viability
        self._capability = capability
        self._reconciler = reconciler
        self._node_manager = node_manager
        self._review = review
        self._session_factory = session_factory
        self._failure_finalizers = FailureFinalizers(
            crud=crud,
            review=review,
        )

    async def execute_verification_effect(
        self,
        job: dict[str, Any],
        effect: PreparedVerificationEffect,
        *,
        http_client_factory: AgentClientFactory,
    ) -> VerificationExecutionOutcome:
        if effect.device_id is None:
            raise NodeManagerError("Verification effect has no persisted device id")

        node_id: uuid.UUID | None = None
        try:
            health_error = await self.run_device_health(job, effect, http_client_factory=http_client_factory)
            if health_error is not None:
                return await self._finalize_failure(effect, error=health_error, job=job, node_id=None)

            prepared = await self._prepare_node(job, effect)
            if isinstance(prepared, VerificationExecutionOutcome):
                return prepared
            if isinstance(prepared, str):
                return await self._finalize_failure(effect, error=prepared, job=job, node_id=None)
            node_id = prepared

            timeout = self._agent.settings.get_int("appium.startup_timeout_sec")
            snapshot = await self.wait_for_node_running(node_id, timeout_sec=timeout)
            if snapshot is None:
                detail = "Verification node did not reach running state within timeout"
                await set_stage(job, "node_start", "failed", detail=detail)
                await set_stage(job, "cleanup", "skipped", detail="Node startup failed before cleanup was needed")
                return await self._finalize_failure(effect, error=detail, job=job, node_id=node_id)

            await set_stage(job, "node_start", "passed", detail="Verification node started")

            probe_error = await self._run_probe_phase(job, effect, snapshot)
            if probe_error is not None:
                return await self._finalize_failure(effect, error=probe_error, job=job, node_id=node_id)

            return await self._finalize_success(effect, job=job, node_id=node_id)
        except Exception:
            await self._finalize_failure(effect, error="Verification crashed unexpectedly", job=job, node_id=node_id)
            raise

    async def run_device_health(
        self, job: dict[str, Any], effect: PreparedVerificationEffect, *, http_client_factory: AgentClientFactory
    ) -> str | None:
        await set_stage(job, "device_health", "running")
        payload = effect.payload
        device_type = payload.get("device_type")
        connection_type = payload.get("connection_type")
        try:
            result = await fetch_pack_device_health(
                effect.host_ip,
                effect.host_agent_port,
                _connection_target_from_payload(payload),
                pack_id=effect.pack_id,
                platform_id=effect.platform_id,
                device_type=_enum_str(device_type) or "real_device",
                connection_type=_enum_str(connection_type),
                ip_address=payload.get("ip_address"),
                http_client_factory=http_client_factory,
                timeout=_device_health_timeout(),
                circuit_breaker=self._agent.circuit_breaker,
                pool=self._agent.pool,
            )
        except AgentCallError as exc:
            detail = f"Agent health check failed: {exc}"
            await set_stage(job, "device_health", "failed", detail=detail)
            return detail

        if result.get("healthy"):
            await set_stage(job, "device_health", "passed", detail="Device health checks passed")
            return None

        detail = _health_failure_detail(result)
        await set_stage(job, "device_health", "failed", detail=detail)
        return detail

    async def _prepare_node(
        self, job: dict[str, Any], effect: PreparedVerificationEffect
    ) -> uuid.UUID | VerificationExecutionOutcome | str:
        assert effect.device_id is not None
        await set_stage(job, "node_start", "running")
        if effect.mode == "update":
            stop_error = await self._stop_existing_node(job, effect)
            if stop_error is not None:
                return stop_error

        try:
            async with self._session_factory() as db:
                lock = await lock_verification_operation(
                    db, device_id=effect.device_id, operation_id=effect.operation_id
                )
                if lock is None:
                    return VerificationExecutionOutcome(
                        status="failed",
                        error="Verification superseded",
                        device_id=str(effect.device_id),
                        superseded=True,
                    )
                locked, _lease = lock
                node = await self._node_manager.start_node(db, locked.device, caller="verification")
                node_id = node.id
        except NodeManagerError as exc:
            detail = str(exc)
            await set_stage(job, "node_start", "failed", detail=detail)
            await set_stage(job, "cleanup", "skipped", detail="Node startup failed before cleanup was needed")
            return detail

        await poke_node_refresh_target(
            NodeRefreshTarget(effect.host_ip, effect.host_agent_port),
            circuit_breaker=self._agent.circuit_breaker,
            pool=self._agent.pool,
        )
        return node_id

    async def _stop_existing_node(self, job: dict[str, Any], effect: PreparedVerificationEffect) -> str | None:
        assert effect.device_id is not None
        async with self._session_factory.begin() as db:
            locked = await device_locking.lock_device(db, effect.device_id)
            node = locked.appium_node
            if node is None or not node.observed_running:
                return None
            await set_stage(
                job,
                "node_start",
                "running",
                detail="Stopping existing managed node before starting updated verification node",
            )
            try:
                await _stop_managed_node_for_verification(db, locked)
            except NodeManagerError as exc:
                detail = f"Failed to stop existing managed node before verification: {exc}"
                await set_stage(job, "node_start", "failed", detail=detail)
                await set_stage(
                    job, "cleanup", "skipped", detail="Existing node stop failed before verification node startup"
                )
                return detail
        return None

    async def wait_for_node_running(self, node_id: uuid.UUID, *, timeout_sec: int) -> NodeEffectSnapshot | None:
        deadline = time.monotonic() + timeout_sec
        while time.monotonic() < deadline:
            async with self._session_factory() as db:
                row = await db.get(AppiumNode, node_id)
                if row is not None and row.observed_running:
                    return NodeEffectSnapshot(row.id, row.active_connection_target)
            await asyncio.sleep(0.5)
        return None

    async def _run_probe_phase(
        self, job: dict[str, Any], effect: PreparedVerificationEffect, snapshot: NodeEffectSnapshot
    ) -> str | None:
        assert effect.device_id is not None
        device_id = effect.device_id
        await set_stage(job, "session_probe", "running")
        timeout_sec = self._agent.settings.get_int("general.session_viability_timeout_sec")

        async with self._session_factory() as db:
            device = await device_locking.lock_device(db, device_id)
            # Probe the device with its pending (agent-normalized) fields so the
            # capabilities reflect what verification is validating. This read
            # session never commits, so the write is in-memory only; the durable
            # apply happens in ``_finalize_success``.
            _restore_create_payload_fields(device, effect.payload)
            capabilities = await self._capability.get_device_capabilities(
                db, device, active_connection_target=snapshot.active_connection_target
            )
            target = node_target(device)

        try:
            async with self._session_factory.begin() as db:
                locked = await lock_device_handle(db, device_id)
                probe_row = await claim_probe_session(
                    db,
                    device=locked.device,
                    source=ProbeSource.verification,
                    capabilities=capabilities,
                    router_target=target,
                )
                probe_id = probe_row.id
        except SessionViabilityProbeInProgressError as exc:
            detail = str(exc)
            await set_stage(job, "session_probe", "failed", detail=detail)
            return detail

        probe_result = ProbeResult(status="indeterminate", detail="Session create request failed: probe aborted")
        try:

            async def _promote(appium_session_id: str) -> None:
                async with self._session_factory.begin() as db:
                    await lock_device_handle(db, device_id)
                    await confirm_probe_session(db, probe_id, appium_session_id=appium_session_id)

            ok, error = await self._viability.probe_session_direct(
                build_probe_capabilities(capabilities), timeout_sec, target=target, on_created=_promote
            )
            probe_result = grid_probe_response_to_result((ok, error))
        finally:
            async with self._session_factory.begin() as db:
                await lock_device_handle(db, effect.device_id)
                await finalize_probe_session(db, probe_id, result=probe_result)

        if probe_result.status == "ack":
            await set_stage(job, "session_probe", "passed", detail="Grid-routed Appium probe session passed")
            return None
        failure = probe_result.detail or "Session probe failed"
        await set_stage(job, "session_probe", "failed", detail=failure)
        return failure

    async def _finalize_success(
        self,
        effect: PreparedVerificationEffect,
        *,
        job: dict[str, Any],
        node_id: uuid.UUID | None,
    ) -> VerificationExecutionOutcome:
        assert effect.device_id is not None
        await set_stage(job, "save_device", "running")
        async with self._session_factory.begin() as db:
            lock = await lock_verification_operation(db, device_id=effect.device_id, operation_id=effect.operation_id)
            if lock is None:
                return VerificationExecutionOutcome(status="failed", device_id=str(effect.device_id), superseded=True)
            locked, _lease = lock
            device = locked.device
            if effect.mode == "update" and await device_has_live_session(db, effect.device_id):
                return VerificationExecutionOutcome(
                    status="failed",
                    error="Device acquired a live session during verification",
                    device_id=str(effect.device_id),
                )
            # ``effect.payload`` is the fully prepared save payload (agent-normalized,
            # device_config already resolved by preparation). Apply it directly under
            # the lock rather than re-entering the self-committing ``update_device``,
            # which would close this ``begin()`` transaction mid-finalization.
            _restore_create_payload_fields(device, effect.payload)
            await set_stage(
                job,
                "cleanup",
                "passed",
                detail="Verified node retained as the managed Appium node",
            )

            device.verified_at = now_utc()
            ladder = await remediation_log.load_ladder(db, device.id)
            if ladder.episode_active:
                await remediation_log.append_reset(db, device.id, source="verification", action="verification_passed")
            await self._viability.record_session_viability_result(
                db,
                device,
                status="passed",
                checked_by=SessionViabilityCheckedBy.verification,
            )
            await _stamp_verification_outcome(db, device, outcome=VERIFICATION_OUTCOME_PASSED)
            await _revoke_verification_node_intent(db, device, publisher=self._publisher)
        detail = "Device saved after verification" if effect.mode == "create" else "Device updated after verification"
        await set_stage(job, "save_device", "passed", detail=detail)
        return VerificationExecutionOutcome(status="completed", device_id=str(effect.device_id))

    async def _finalize_failure(
        self,
        effect: PreparedVerificationEffect,
        *,
        error: str,
        job: dict[str, Any],
        node_id: uuid.UUID | None = None,
    ) -> VerificationExecutionOutcome:
        assert effect.device_id is not None
        async with self._session_factory.begin() as db:
            lock = await lock_verification_operation(db, device_id=effect.device_id, operation_id=effect.operation_id)
            if lock is None:
                return VerificationExecutionOutcome(
                    status="failed", error=error, device_id=str(effect.device_id), superseded=True
                )
            locked, _lease = lock
            device = locked.device
            node = await db.get(AppiumNode, node_id) if node_id is not None else None

            if effect.mode == "create":
                cleanup_error = await _stop_verification_node_if_running(job, db, device, node)
                # Device deletion cascades to DeviceIntent rows, so the lease dies with it.
                await self._failure_finalizers.crud.delete_device(db, effect.device_id)
                if cleanup_error is not None:
                    return VerificationExecutionOutcome(status="failed", error=cleanup_error, device_id=None)
                return VerificationExecutionOutcome(status="failed", error=error, device_id=None)

            _restore_update_original_fields(device, effect.original_fields)
            await self._failure_finalizers.review.mark_review_required(
                db,
                device,
                reason=f"verification failed: {error}",
                source="verification",
            )
            await _stamp_verification_outcome(db, device, outcome=VERIFICATION_OUTCOME_FAILED)
            await _stop_verification_node_if_running(job, db, device, node)
            await IntentService(db).revoke_intents_and_reconcile(
                device_id=device.id,
                sources=[
                    verification_intent_source(device.id),
                    *operator_stop_sources(device.id),
                    operator_start_source(device.id),
                ],
                publisher=self._publisher,
            )
        return VerificationExecutionOutcome(status="failed", error=error, device_id=str(effect.device_id))


def _health_failure_detail(result: dict[str, Any]) -> str:
    detail = result.get("detail")
    if isinstance(detail, str) and detail:
        return detail
    checks = result.get("checks")
    if isinstance(checks, list):
        for check in checks:
            if not isinstance(check, dict):
                continue
            if not check.get("ok"):
                check_id = check.get("check_id", "unknown")
                message = check.get("message", "")
                suffix = f" ({message})" if message else ""
                return f"{check_id.replace('_', ' ')} failed{suffix}"
    return "Device health checks failed"


def _connection_target_from_payload(payload: dict[str, Any]) -> str:
    connection_target = payload.get("connection_target")
    if isinstance(connection_target, str) and connection_target:
        return connection_target
    identity_value = payload.get("identity_value")
    if isinstance(identity_value, str) and identity_value:
        return identity_value
    raise ValueError("Verification payload has no connection target or identity value")


def _enum_str(value: object) -> str | None:
    if value is None:
        return None
    return str(getattr(value, "value", None) or value)


def _device_health_timeout() -> float | int:
    return 10


async def _stop_managed_node_for_verification(db: AsyncSession, device: Device) -> AppiumNode:
    """Write stopped desired state for the update path under the row lock."""
    locked_device = await device_locking.lock_device(db, device.id)
    node: AppiumNode | None = locked_device.appium_node
    if node is None or not node.observed_running:
        raise NodeManagerError(f"No running node for device {device.id}")
    await write_desired_state(
        db,
        node=node,
        caller="verification",
        write=DesiredStateWrite(target=AppiumDesiredState.stopped),
    )
    node.pid = None
    node.active_connection_target = None
    await db.flush()
    return node


async def _revoke_verification_node_intent(db: AsyncSession, device: Device, *, publisher: EventPublisher) -> None:
    """Revoke the standing verification node_process intent (idempotent)."""
    await IntentService(db).revoke_intents_and_reconcile(
        device_id=device.id,
        sources=[verification_intent_source(device.id)],
        publisher=publisher,
    )


async def _stamp_verification_outcome(db: AsyncSession, device: Device, *, outcome: str) -> None:
    """Stamp the terminal outcome on the verification lease row (WS-15.3)."""
    lease = (
        await db.execute(
            select(DeviceIntent).where(
                DeviceIntent.device_id == device.id,
                DeviceIntent.source == verification_intent_source(device.id),
            )
        )
    ).scalar_one_or_none()
    if lease is None:
        return
    lease.payload = {**lease.payload, VERIFICATION_OUTCOME_KEY: outcome}
    await db.flush()


async def _stop_verification_node_if_running(
    job: dict[str, Any],
    db: AsyncSession,
    device: Device,
    node: AppiumNode | None,
) -> str | None:
    """Stop the verification node via the commit-free desired-state writer and
    clear its observation columns.

    The finalizer owns the transaction (``session_factory.begin()``), so the
    node stop must NOT route through the self-committing operator ``stop_node``
    path; it writes ``stopped`` desired state directly under the device row lock
    the finalizer already holds. No operator:stop intents are registered, so the
    failure-path revoke of ``operator_stop_sources`` is a harmless no-op.

    PRECONDITION: the caller MUST already hold the device row lock.
    """
    if node is None:
        return None
    try:
        await write_desired_state(
            db,
            node=node,
            caller="verification",
            write=DesiredStateWrite(target=AppiumDesiredState.stopped),
        )
        node.pid = None
        node.active_connection_target = None
        await db.flush()
    except NodeManagerError:
        return None
    except Exception as exc:  # noqa: BLE001 — catch-all in cleanup path; set failure detail and return it to caller
        node.pid = None
        node.active_connection_target = None
        await db.flush()
        detail = f"Failed to stop verification node: {exc}"
        await set_stage(job, "cleanup", "failed", detail=detail)
        return detail
    return None


def _restore_create_payload_fields(device: Device, payload: dict[str, Any]) -> None:
    for key in (
        "pack_id",
        "platform_id",
        "identity_scheme",
        "identity_scope",
        "identity_value",
        "connection_target",
        "name",
        "host_id",
        "os_version",
        "os_version_display",
        "manufacturer",
        "model",
        "model_number",
        "software_versions",
        "device_type",
        "connection_type",
        "ip_address",
        "device_config",
    ):
        if key in payload:
            setattr(device, key, payload[key])


def _restore_update_original_fields(device: Device, original_fields: dict[str, Any] | None) -> None:
    if original_fields is None:
        return
    for key, value in original_fields.items():
        setattr(device, key, deepcopy(value))
