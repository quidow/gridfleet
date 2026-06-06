"""Assemble device diagnostic bundles for export and persistence.

Bundle assembly runs in a single read-only transaction. No row locks
and no commits. Adequate for a diagnostic artifact; this is not a
serializable global snapshot.
"""

from __future__ import annotations

import copy
import hashlib
import logging
import secrets
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import select

from app.agent_comm.models import AgentReconfigureOutbox
from app.appium_nodes.models.node import AppiumNode
from app.core.leader import state_store as control_plane_state_store
from app.devices.models import DeviceEvent, DeviceIntent, DeviceReservation
from app.diagnostics.models import DeviceDiagnosticSnapshot
from app.diagnostics.schemas import DIAGNOSTIC_BUNDLE_SCHEMA_VERSION
from app.runs.models import TestRun
from app.sessions.models import Session, SessionStatus

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.devices.models import Device

logger = logging.getLogger(__name__)

_REDACTION_NAMESPACE = "diagnostic_redaction"
_REDACTION_SALT_KEY = "salt"
_INTENT_CAP = 200
_RECENT_ENDED_SESSIONS_CAP = 20
_EVENTS_CAP = 50
_OUTBOX_DELIVERED_CAP = 5
_UUID_LIKE_LENGTH = 36
_EVENT_DETAILS_KEY_SET = frozenset(
    {
        "identity_value",
        "connection_target",
        "ip_address",
        "host_ip",
        "active_connection_target",
        "session_id",
    }
)


def _utcnow_iso() -> str:
    return datetime.now(UTC).isoformat()


def _project_device(device: Device) -> dict[str, Any]:
    return {
        "id": str(device.id),
        "name": device.name,
        "pack_id": device.pack_id,
        "platform_id": device.platform_id,
        "host_id": str(device.host_id),
        "identity_scheme": device.identity_scheme,
        "identity_scope": device.identity_scope,
        "identity_value": device.identity_value,
        "connection_target": device.connection_target,
        "os_version": device.os_version,
        "os_version_display": device.os_version_display,
        "manufacturer": device.manufacturer,
        "model": device.model,
        "model_number": device.model_number,
        "software_versions": device.software_versions,
        "device_type": device.device_type.value if device.device_type else None,
        "connection_type": device.connection_type.value if device.connection_type else None,
        "ip_address": device.ip_address,
        "operational_state": device.operational_state.value if device.operational_state else None,
        "tags": device.tags,
        "verified_at": device.verified_at.isoformat() if device.verified_at else None,
        "emulator_state": device.emulator_state,
        "battery_level_percent": device.battery_level_percent,
        "battery_temperature_c": device.battery_temperature_c,
        "charging_state": device.charging_state.value if device.charging_state else None,
        "hardware_health_status": (device.hardware_health_status.value if device.hardware_health_status else None),
        "hardware_telemetry_support_status": (
            device.hardware_telemetry_support_status.value if device.hardware_telemetry_support_status else None
        ),
        "hardware_telemetry_reported_at": (
            device.hardware_telemetry_reported_at.isoformat() if device.hardware_telemetry_reported_at else None
        ),
        "device_checks_healthy": device.device_checks_healthy,
        "device_checks_summary": device.device_checks_summary,
        "device_checks_checked_at": (
            device.device_checks_checked_at.isoformat() if device.device_checks_checked_at else None
        ),
        "session_viability_status": device.session_viability_status,
        "session_viability_error": device.session_viability_error,
        "session_viability_checked_at": (
            device.session_viability_checked_at.isoformat() if device.session_viability_checked_at else None
        ),
        "recovery_allowed": device.recovery_allowed,
        "recovery_blocked_reason": device.recovery_blocked_reason,
        "review_required": device.review_required,
        "review_reason": device.review_reason,
        "review_set_at": device.review_set_at.isoformat() if device.review_set_at else None,
        "lifecycle_policy_state": device.lifecycle_policy_state,
        "device_config": device.device_config,
        "created_at": device.created_at.isoformat() if device.created_at else None,
        "updated_at": device.updated_at.isoformat() if device.updated_at else None,
    }


async def _read_appium_node(db: AsyncSession, device: Device) -> dict[str, Any] | None:
    result = await db.execute(select(AppiumNode).where(AppiumNode.device_id == device.id))
    node = result.scalar_one_or_none()
    if node is None:
        return None
    return {
        "id": str(node.id),
        "device_id": str(node.device_id),
        "port": node.port,
        "pid": node.pid,
        "container_id": node.container_id,
        "active_connection_target": node.active_connection_target,
        "desired_state": node.desired_state.value if node.desired_state else None,
        "desired_port": node.desired_port,
        "desired_grid_run_id": str(node.desired_grid_run_id) if node.desired_grid_run_id else None,
        "accepting_new_sessions": node.accepting_new_sessions,
        "stop_pending": node.stop_pending,
        "generation": node.generation,
        "transition_token": str(node.transition_token) if node.transition_token else None,
        "transition_deadline": node.transition_deadline.isoformat() if node.transition_deadline else None,
        "last_observed_at": node.last_observed_at.isoformat() if node.last_observed_at else None,
        "grid_run_id": str(node.grid_run_id) if node.grid_run_id else None,
        "started_at": node.started_at.isoformat() if node.started_at else None,
        "consecutive_health_failures": node.consecutive_health_failures,
        "last_health_checked_at": (node.last_health_checked_at.isoformat() if node.last_health_checked_at else None),
        "health_running": node.health_running,
        "health_state": node.health_state,
        "observed_running": node.observed_running,
    }


async def _read_reservations(db: AsyncSession, device: Device) -> list[dict[str, Any]]:
    result = await db.execute(
        select(DeviceReservation)
        .where(DeviceReservation.device_id == device.id)
        .order_by(DeviceReservation.created_at.desc())
    )
    return [
        {
            "id": str(row.id),
            "run_id": str(row.run_id),
            "device_id": str(row.device_id),
            "identity_value": row.identity_value,
            "connection_target": row.connection_target,
            "pack_id": row.pack_id,
            "platform_id": row.platform_id,
            "platform_label": row.platform_label,
            "os_version": row.os_version,
            "host_ip": row.host_ip,
            "excluded": row.excluded,
            "exclusion_reason": row.exclusion_reason,
            "excluded_at": row.excluded_at.isoformat() if row.excluded_at else None,
            "excluded_until": row.excluded_until.isoformat() if row.excluded_until else None,
            "cooldown_count": row.cooldown_count,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "released_at": row.released_at.isoformat() if row.released_at else None,
        }
        for row in result.scalars().all()
    ]


async def _read_intents(db: AsyncSession, device: Device) -> tuple[list[dict[str, Any]], list[str]]:
    result = await db.execute(
        select(DeviceIntent)
        .where(DeviceIntent.device_id == device.id)
        .order_by(DeviceIntent.created_at.desc())
        .limit(_INTENT_CAP + 1)
    )
    intents = result.scalars().all()
    warnings: list[str] = []
    truncated = intents[:_INTENT_CAP]
    if len(intents) > _INTENT_CAP:
        warnings.append(f"intents truncated at {_INTENT_CAP} rows")
        logger.warning("device %s diagnostic bundle truncated intents at %d", device.id, _INTENT_CAP)
    return [
        {
            "id": str(intent.id),
            "device_id": str(intent.device_id),
            "source": intent.source,
            "axis": intent.axis,
            "run_id": str(intent.run_id) if intent.run_id else None,
            "payload": intent.payload,
            "precondition": intent.precondition,
            "expires_at": intent.expires_at.isoformat() if intent.expires_at else None,
            "created_at": intent.created_at.isoformat() if intent.created_at else None,
            "updated_at": intent.updated_at.isoformat() if intent.updated_at else None,
        }
        for intent in truncated
    ], warnings


async def _read_sessions(db: AsyncSession, device: Device) -> dict[str, list[dict[str, Any]]]:
    running_result = await db.execute(
        select(Session).where(
            Session.device_id == device.id,
            Session.status == SessionStatus.running,
            Session.ended_at.is_(None),
        )
    )
    recent_ended_result = await db.execute(
        select(Session)
        .where(Session.device_id == device.id, Session.ended_at.is_not(None))
        .order_by(Session.ended_at.desc())
        .limit(_RECENT_ENDED_SESSIONS_CAP)
    )

    def project(session: Session) -> dict[str, Any]:
        return {
            "id": str(session.id),
            "session_id": session.session_id,
            "device_id": str(session.device_id) if session.device_id else None,
            "run_id": str(session.run_id) if session.run_id else None,
            "status": session.status.value if session.status else None,
            "test_name": session.test_name,
            "started_at": session.started_at.isoformat() if session.started_at else None,
            "ended_at": session.ended_at.isoformat() if session.ended_at else None,
            "error_type": session.error_type,
            "error_message": session.error_message,
            "requested_pack_id": session.requested_pack_id,
            "requested_platform_id": session.requested_platform_id,
        }

    return {
        "running": [project(session) for session in running_result.scalars().all()],
        "recent_ended": [project(session) for session in recent_ended_result.scalars().all()],
    }


async def _read_events(db: AsyncSession, device: Device) -> list[dict[str, Any]]:
    result = await db.execute(
        select(DeviceEvent)
        .where(DeviceEvent.device_id == device.id)
        .order_by(DeviceEvent.created_at.desc())
        .limit(_EVENTS_CAP)
    )
    return [
        {
            "id": str(event.id),
            "device_id": str(event.device_id),
            # ``DeviceEvent.event_type`` is a NOT NULL enum column — direct
            # ``.value`` access. The previous ``if event_type else None`` was
            # unreachable.
            "event_type": event.event_type.value,
            "details": event.details,
            "created_at": event.created_at.isoformat() if event.created_at else None,
        }
        for event in result.scalars().all()
    ]


async def _read_related_runs(db: AsyncSession, run_ids: set[uuid.UUID]) -> list[dict[str, Any]]:
    if not run_ids:
        return []
    result = await db.execute(select(TestRun).where(TestRun.id.in_(run_ids)))
    return [
        {
            "id": str(row.id),
            "name": row.name,
            "state": row.state.value if row.state else None,
            "ttl_minutes": row.ttl_minutes,
            "heartbeat_timeout_sec": row.heartbeat_timeout_sec,
            "last_heartbeat": row.last_heartbeat.isoformat() if row.last_heartbeat else None,
            "error": row.error,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "started_at": row.started_at.isoformat() if row.started_at else None,
            "completed_at": row.completed_at.isoformat() if row.completed_at else None,
            "created_by": row.created_by,
        }
        for row in result.scalars().all()
    ]


async def _read_outbox(db: AsyncSession, device: Device) -> list[dict[str, Any]]:
    pending = await db.execute(
        select(AgentReconfigureOutbox)
        .where(
            AgentReconfigureOutbox.device_id == device.id,
            AgentReconfigureOutbox.delivered_at.is_(None),
            AgentReconfigureOutbox.abandoned_at.is_(None),
        )
        .order_by(AgentReconfigureOutbox.created_at.desc())
    )
    delivered = await db.execute(
        select(AgentReconfigureOutbox)
        .where(
            AgentReconfigureOutbox.device_id == device.id,
            (AgentReconfigureOutbox.delivered_at.is_not(None) | AgentReconfigureOutbox.abandoned_at.is_not(None)),
        )
        .order_by(AgentReconfigureOutbox.created_at.desc())
        .limit(_OUTBOX_DELIVERED_CAP)
    )

    def project(row: AgentReconfigureOutbox) -> dict[str, Any]:
        return {
            "id": str(row.id),
            "device_id": str(row.device_id),
            "port": row.port,
            "accepting_new_sessions": row.accepting_new_sessions,
            "stop_pending": row.stop_pending,
            "grid_run_id": str(row.grid_run_id) if row.grid_run_id else None,
            "reconciled_generation": row.reconciled_generation,
            "delivery_attempts": row.delivery_attempts,
            "abandoned_reason": row.abandoned_reason,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "delivered_at": row.delivered_at.isoformat() if row.delivered_at else None,
            "abandoned_at": row.abandoned_at.isoformat() if row.abandoned_at else None,
        }

    return [project(row) for row in pending.scalars().all()] + [project(row) for row in delivered.scalars().all()]


async def _get_or_create_redaction_salt(db: AsyncSession) -> str:
    stored = await control_plane_state_store.get_value(db, _REDACTION_NAMESPACE, _REDACTION_SALT_KEY)
    if isinstance(stored, str) and stored:
        return stored
    fresh = secrets.token_hex(32)
    inserted = await control_plane_state_store.try_claim_value(db, _REDACTION_NAMESPACE, _REDACTION_SALT_KEY, fresh)
    if inserted:
        # The salt is a per-deployment one-shot write. The commit here also
        # flushes any co-pending writes already in the caller's transaction
        # (e.g. the export route's rate-limit set_value and the in-flight
        # ``capture_snapshot`` insert when this path is reached from
        # ``POST /export?redact=true``). That is intentional and benign — all
        # three writes belong to the same successful operator action — but
        # callers must not rely on this commit being isolated.
        await db.commit()
        return fresh
    again = await control_plane_state_store.get_value(db, _REDACTION_NAMESPACE, _REDACTION_SALT_KEY)
    if not isinstance(again, str) or not again:
        raise RuntimeError("Failed to materialise diagnostic redaction salt")
    return again


def _hash_value(value: str, salt: str) -> str:
    digest = hashlib.sha256((value + salt).encode("utf-8")).hexdigest()
    return f"redacted:{digest[:8]}"


def _looks_uuid(value: str) -> bool:
    if len(value) != _UUID_LIKE_LENGTH:
        return False
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True


def _hash_if_str(obj: dict[str, Any], key: str, salt: str) -> None:
    value = obj.get(key)
    if isinstance(value, str) and value:
        obj[key] = _hash_value(value, salt)


def _collect_sensitive_ids(bundle: dict[str, Any]) -> set[str]:
    ids: set[str] = set()
    device = bundle.get("device")
    if isinstance(device, dict):
        for key in ("id", "host_id"):
            value = device.get(key)
            if isinstance(value, str) and value:
                ids.add(value)
    for reservation in bundle.get("reservations") or []:
        if isinstance(reservation, dict):
            for key in ("id", "run_id"):
                value = reservation.get(key)
                if isinstance(value, str) and value:
                    ids.add(value)
    for intent in bundle.get("intents") or []:
        if isinstance(intent, dict):
            value = intent.get("run_id")
            if isinstance(value, str) and value:
                ids.add(value)
    sessions = bundle.get("sessions")
    if isinstance(sessions, dict):
        for bucket in ("running", "recent_ended"):
            for session in sessions.get(bucket) or []:
                if isinstance(session, dict):
                    for key in ("id", "run_id"):
                        value = session.get(key)
                        if isinstance(value, str) and value:
                            ids.add(value)
    for run in bundle.get("related_runs") or []:
        if isinstance(run, dict):
            value = run.get("id")
            if isinstance(value, str) and value:
                ids.add(value)
    return ids


def _redact_event_details(
    obj: object,
    *,
    salt: str,
    sensitive_ids: set[str],
) -> object:
    if isinstance(obj, dict):
        details = cast("dict[object, object]", obj)
        for key, value in list(details.items()):
            if isinstance(value, str):
                if (isinstance(key, str) and key in _EVENT_DETAILS_KEY_SET and value) or (
                    _looks_uuid(value) and value in sensitive_ids
                ):
                    details[key] = _hash_value(value, salt)
            elif isinstance(value, (dict, list)):
                _redact_event_details(value, salt=salt, sensitive_ids=sensitive_ids)
    elif isinstance(obj, list):
        values = cast("list[object]", obj)
        for index, value in enumerate(values):
            if isinstance(value, str) and _looks_uuid(value) and value in sensitive_ids:
                values[index] = _hash_value(value, salt)
            elif isinstance(value, (dict, list)):
                _redact_event_details(value, salt=salt, sensitive_ids=sensitive_ids)
    return obj


class DiagnosticExportService:
    """Assemble, redact, persist, and read device diagnostic bundles."""

    async def assemble_bundle(
        self,
        db: AsyncSession,
        device: Device,
        *,
        redact: bool,
    ) -> dict[str, Any]:
        """Assemble a diagnostic bundle for ``device``. Read-only. No commits."""
        warnings: list[str] = []
        node = await _read_appium_node(db, device)
        reservations = await _read_reservations(db, device)
        intents, intent_warnings = await _read_intents(db, device)
        warnings.extend(intent_warnings)
        sessions = await _read_sessions(db, device)
        events = await _read_events(db, device)

        run_ids: set[uuid.UUID] = set()
        for intent in intents:
            if intent.get("run_id"):
                run_ids.add(uuid.UUID(intent["run_id"]))
        for reservation in reservations:
            if reservation.get("run_id"):
                run_ids.add(uuid.UUID(reservation["run_id"]))
        for session in (*sessions["running"], *sessions["recent_ended"]):
            if session.get("run_id"):
                run_ids.add(uuid.UUID(session["run_id"]))

        related_runs = await _read_related_runs(db, run_ids)
        outbox = await _read_outbox(db, device)

        bundle: dict[str, Any] = {
            "schema_version": DIAGNOSTIC_BUNDLE_SCHEMA_VERSION,
            "captured_at": _utcnow_iso(),
            "redacted": False,
            "device": _project_device(device),
            "appium_node": node,
            "reservations": reservations,
            "intents": intents,
            "sessions": sessions,
            "events": events,
            "related_runs": related_runs,
            "agent_reconfigure_outbox": outbox,
        }
        if warnings:
            bundle["warnings"] = warnings
        if redact:
            bundle = await self.redact_bundle(db, bundle)
        return bundle

    async def redact_bundle(self, db: AsyncSession, bundle: dict[str, Any]) -> dict[str, Any]:
        """Apply the diagnostic bundle redaction rules."""
        salt = await _get_or_create_redaction_salt(db)
        sensitive_ids = _collect_sensitive_ids(bundle)
        redacted = copy.deepcopy(bundle)

        device = redacted.get("device")
        if isinstance(device, dict):
            _hash_if_str(device, "identity_value", salt)
            _hash_if_str(device, "connection_target", salt)
            _hash_if_str(device, "ip_address", salt)

        node = redacted.get("appium_node")
        if isinstance(node, dict):
            _hash_if_str(node, "active_connection_target", salt)

        for reservation in redacted.get("reservations") or []:
            if isinstance(reservation, dict):
                _hash_if_str(reservation, "identity_value", salt)
                _hash_if_str(reservation, "connection_target", salt)
                _hash_if_str(reservation, "host_ip", salt)

        sessions = redacted.get("sessions")
        if isinstance(sessions, dict):
            for bucket in ("running", "recent_ended"):
                for session in sessions.get(bucket) or []:
                    if isinstance(session, dict):
                        _hash_if_str(session, "session_id", salt)

        for run in redacted.get("related_runs") or []:
            if isinstance(run, dict):
                _hash_if_str(run, "name", salt)

        for event in redacted.get("events") or []:
            if isinstance(event, dict):
                details = event.get("details")
                if isinstance(details, (dict, list)):
                    _redact_event_details(details, salt=salt, sensitive_ids=sensitive_ids)

        redacted["redacted"] = True
        return redacted

    async def capture_snapshot(
        self,
        db: AsyncSession,
        device: Device,
        *,
        trigger: str,
        reason: str | None,
    ) -> uuid.UUID:
        """Assemble an unredacted bundle and insert a snapshot row.

        Does not commit; the caller owns the transaction boundary.
        """
        payload = await self.assemble_bundle(db, device, redact=False)
        row = DeviceDiagnosticSnapshot(
            device_id=device.id,
            trigger=trigger,
            reason=reason,
            payload=payload,
        )
        db.add(row)
        await db.flush()
        return row.id

    async def list_snapshots(
        self, db: AsyncSession, device_id: uuid.UUID, *, limit: int, before: uuid.UUID | None
    ) -> tuple[list[DeviceDiagnosticSnapshot], uuid.UUID | None]:
        """Return a page of snapshot summaries for ``device_id`` plus the next cursor.

        Raises ``ValueError`` when ``before`` does not resolve to a known snapshot.
        """
        stmt = (
            select(DeviceDiagnosticSnapshot)
            .where(DeviceDiagnosticSnapshot.device_id == device_id)
            .order_by(DeviceDiagnosticSnapshot.captured_at.desc(), DeviceDiagnosticSnapshot.id.desc())
        )
        if before is not None:
            cursor_row = (
                await db.execute(
                    select(DeviceDiagnosticSnapshot.captured_at).where(
                        DeviceDiagnosticSnapshot.id == before,
                        DeviceDiagnosticSnapshot.device_id == device_id,
                    )
                )
            ).scalar_one_or_none()
            if cursor_row is None:
                raise ValueError("Unknown before cursor")
            stmt = stmt.where(DeviceDiagnosticSnapshot.captured_at < cursor_row)
        stmt = stmt.limit(limit + 1)
        rows = list((await db.execute(stmt)).scalars().all())
        next_before: uuid.UUID | None = None
        if len(rows) > limit:
            rows = rows[:limit]
            next_before = rows[-1].id
        return rows, next_before

    async def get_snapshot(
        self, db: AsyncSession, device_id: uuid.UUID, snapshot_id: uuid.UUID
    ) -> DeviceDiagnosticSnapshot | None:
        """Return a single snapshot row for ``device_id``/``snapshot_id`` or ``None``."""
        return (
            await db.execute(
                select(DeviceDiagnosticSnapshot).where(
                    DeviceDiagnosticSnapshot.id == snapshot_id,
                    DeviceDiagnosticSnapshot.device_id == device_id,
                )
            )
        ).scalar_one_or_none()
