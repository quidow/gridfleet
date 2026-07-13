from __future__ import annotations

from dataclasses import dataclass
from time import perf_counter
from typing import TYPE_CHECKING, Any

from app.core.leader import state_store as control_plane_state_store
from app.core.metrics_recorders import (
    HOST_PUSH_OBSERVATION_FAILURES,
    record_host_push_boot_fence_rejection,
    record_host_push_token_anomaly,
    record_host_status_push,
)
from app.core.observability import get_logger
from app.core.observation_revision import next_observation_revision
from app.core.timeutil import now_utc
from app.hosts.models import Host
from app.hosts.observation_token import canonical_section_hash, extract_token
from app.hosts.service import normalize_capabilities, update_missing_prerequisites_from_health

logger = get_logger(__name__)

if TYPE_CHECKING:
    import uuid
    from collections.abc import Awaitable, Callable

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.core.type_defs import SessionFactory
    from app.events.protocols import EventPublisher
    from app.hosts.schemas import HostStatusPush


class BootFenceError(Exception):
    """A status push carried a boot_id that does not match the host's registered
    boot: a superseded or split-brain boot. The endpoint maps this to HTTP 409."""

    def __init__(self, *, host_id: uuid.UUID, current: uuid.UUID, incoming: uuid.UUID) -> None:
        super().__init__(f"boot_id {incoming} superseded by {current} for host {host_id}")
        self.host_id = host_id


# One snapshot per host: the latest consolidated status push. Read by the
# host_sweep liveness/convergence stages, host diagnostics, and the resource
# telemetry stage — the single snapshot source (no second fetch path).
HOST_STATUS_NAMESPACE = "status_push.host_status"

# The two moved health folds (node_health, device_health) that carry an
# ingest-stamped observation revision for the two-axis write-ordering guard.
GUARDED_SECTIONS = ("node_health", "device_health")

# Key under which the ingest-time revision is stamped onto each guarded snapshot
# section. The inline folds read it and pass it to the guarded health writers.
OBSERVATION_REVISION_KEY = "observation_revision"


@dataclass(frozen=True)
class ObservationFold:
    """One push section folded into durable device or host facts."""

    section: str
    fold: Callable[[AsyncSession, uuid.UUID, dict[str, Any]], Awaitable[None]]


def push_sections(push: HostStatusPush) -> dict[str, Any]:
    return {
        "appium_processes": push.appium_processes,
        "host_telemetry": push.host_telemetry,
        "node_health": push.node_health,
        "device_health": push.device_health,
        "device_telemetry": push.device_telemetry,
        "device_properties": push.device_properties,
    }


class HostStatusPushService:
    """Persist liveness first, then contain push-time observation processing."""

    def __init__(
        self,
        *,
        publisher: EventPublisher,
        session_factory: SessionFactory | None = None,
        observation_folds: tuple[ObservationFold, ...] = (),
        converge_host: Callable[..., Awaitable[None]] | None = None,
        ingest_restart_events: Callable[[AsyncSession, Host, dict[str, Any]], Awaitable[None]] | None = None,
    ) -> None:
        self._publisher = publisher
        self._session_factory = session_factory
        self._observation_folds = observation_folds
        self._converge_host = converge_host
        self._ingest_restart_events = ingest_restart_events

    async def apply_status_push(self, db: AsyncSession, host: Host, push: HostStatusPush) -> dict[str, Any]:
        """Fence the boot, persist liveness, publish the snapshot, and return the
        payload the inline observation folds should process this push. The host
        row is locked by the caller so the fence and per-section cursor advance
        are atomic against a concurrent push for the same host.

        The returned fold payload omits a moved section that did not advance a
        generation (a re-delivery of the same gather), so its inline fold is
        skipped; the stored snapshot always carries the current revision.
        """
        # Fence before any write: a superseded/split-brain boot must not update
        # liveness or the snapshot.
        self._apply_boot_fence(host, push.boot_id)
        host.last_heartbeat = now_utc()
        if push.agent_version and host.agent_version != push.agent_version:
            host.agent_version = push.agent_version
        if push.capabilities is not None:
            host.capabilities = normalize_capabilities(push.capabilities)
        # After the capabilities update so a pushed top-level list wins over
        # whatever the snapshot carried (mirrors the old health-poll order).
        if push.missing_prerequisites is not None:
            update_missing_prerequisites_from_health(host, push.missing_prerequisites)

        sections = push_sections(push)
        prior = await control_plane_state_store.get_value(db, HOST_STATUS_NAMESPACE, str(host.id))
        prior_sections = prior.get("payload") if isinstance(prior, dict) else None
        if not isinstance(prior_sections, dict):
            prior_sections = {}
        fold_payload = dict(sections)
        for name in GUARDED_SECTIONS:
            section = sections.get(name)
            if not isinstance(section, dict):
                continue
            revision, advanced = await self._advance_section_cursor(db, host, name, section, boot_id=push.boot_id)
            if advanced:
                # New generation: stamp the fresh revision and fold it inline.
                section[OBSERVATION_REVISION_KEY] = revision
            else:
                # Re-delivery / stale: keep the prior snapshot body (which already
                # carries the current revision) and skip the inline re-fold.
                prior_section = prior_sections.get(name)
                if isinstance(prior_section, dict):
                    sections[name] = prior_section
                else:
                    section[OBSERVATION_REVISION_KEY] = revision
                fold_payload.pop(name, None)

        await control_plane_state_store.set_value(
            db,
            HOST_STATUS_NAMESPACE,
            str(host.id),
            {"received_at": now_utc().isoformat(), "payload": sections},
        )
        record_host_status_push(host_id=str(host.id))
        return fold_payload

    @staticmethod
    def _apply_boot_fence(host: Host, boot_id: uuid.UUID | None) -> None:
        current = host.current_boot_id
        if boot_id is None:
            # Legacy/tokenless push. A tokenless push cannot be fenced once the
            # host has adopted a boot; accept it (logged) rather than drop liveness.
            if current is not None:
                record_host_push_token_anomaly("tokenless_after_boot")
            return
        if current is None:
            host.current_boot_id = boot_id  # first tokened push: adopt as current
            return
        if boot_id == current:
            return
        record_host_push_boot_fence_rejection()
        raise BootFenceError(host_id=host.id, current=current, incoming=boot_id)

    async def _advance_section_cursor(
        self, db: AsyncSession, host: Host, name: str, section: dict[str, Any], *, boot_id: uuid.UUID | None
    ) -> tuple[int, bool]:
        """Compare the section's token against the host's per-section cursor.

        Returns ``(revision, advanced)``. On a genuine advance draw and stamp a
        fresh revision and move the cursor; on a re-delivery/stale token reuse the
        cursor's revision so the guard sees an already-applied generation.
        """
        token = extract_token(section, boot_id=boot_id)
        if token is None:
            # Tokenless section: fresh revision every push (no dedup, at-least-once).
            return await next_observation_revision(db), True
        computed = canonical_section_hash(section)
        if computed != token.payload_sha256:
            record_host_push_token_anomaly("hash_mismatch")
        cursors = host.observation_cursors if isinstance(host.observation_cursors, dict) else {}
        cursor = cursors.get(name)
        advanced = True
        if isinstance(cursor, dict):
            cursor_seq = cursor.get("section_sequence")
            if token.boot_id == cursor.get("boot_id") and isinstance(cursor_seq, int):
                if token.section_sequence < cursor_seq:
                    advanced = False  # stale/out-of-order delivery
                elif token.section_sequence == cursor_seq:
                    if computed == cursor.get("payload_sha256"):
                        advanced = False  # exact re-delivery of the same gather
                    else:
                        # Same sequence, different payload: a contract violation.
                        # Process it (latest wins) rather than suppress.
                        record_host_push_token_anomaly("same_sequence_different_hash")
        if not advanced:
            reused = cursor.get("revision") if isinstance(cursor, dict) else None
            if isinstance(reused, int):
                return reused, False
        revision = await next_observation_revision(db)
        new_cursors = dict(cursors)
        new_cursors[name] = {
            "boot_id": token.boot_id,
            "section_sequence": token.section_sequence,
            "payload_sha256": computed,
            "revision": revision,
        }
        host.observation_cursors = new_cursors
        return revision, True

    async def process_observations(
        self, *, host_id: uuid.UUID, host_ip: str, agent_port: int, payload: dict[str, Any]
    ) -> None:
        """Run restart ingest, convergence, and folds without raising to the endpoint."""
        if self._session_factory is None:
            return
        if self._ingest_restart_events is not None:
            started = perf_counter()
            try:
                async with self._session_factory() as db:
                    host = await db.get(Host, host_id)
                    if host is not None:
                        await self._ingest_restart_events(db, host, payload)
                        await db.commit()
            except Exception:  # noqa: BLE001 - observation stages must never starve liveness
                HOST_PUSH_OBSERVATION_FAILURES.labels(stage="restart_events").inc()
            self._log_stage("restart_events", host_id, started)
        if self._converge_host is not None:
            started = perf_counter()
            try:
                await self._converge_host(
                    host_id=host_id,
                    host_ip=host_ip,
                    agent_port=agent_port,
                    payload=payload,
                )
            except Exception:  # noqa: BLE001 - observation stages must never starve liveness
                HOST_PUSH_OBSERVATION_FAILURES.labels(stage="convergence").inc()
            self._log_stage("convergence", host_id, started)
        for entry in self._observation_folds:
            section = payload.get(entry.section)
            if not isinstance(section, dict):
                continue
            started = perf_counter()
            try:
                async with self._session_factory() as fold_db:
                    await entry.fold(fold_db, host_id, section)
                    await fold_db.commit()
            except Exception:  # noqa: BLE001 - observation stages must never starve liveness
                HOST_PUSH_OBSERVATION_FAILURES.labels(stage=f"fold:{entry.section}").inc()
            self._log_stage(f"fold:{entry.section}", host_id, started)

    @staticmethod
    def _log_stage(stage: str, host_id: uuid.UUID, started: float) -> None:
        """Per-stage timing for the consolidated push ingest (diagnostic: which
        stage dominates the handler's CPU). Emitted per push per stage."""
        logger.info(
            "status_push_stage",
            stage=stage,
            host_id=str(host_id),
            duration_ms=round((perf_counter() - started) * 1000, 1),
        )
