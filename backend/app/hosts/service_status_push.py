from __future__ import annotations

import asyncio
import copy
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from time import perf_counter
from typing import TYPE_CHECKING, Any

from app.core.config import settings as core_settings
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
    from collections.abc import AsyncIterator, Awaitable, Callable

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


class SectionHashMismatchError(Exception):
    """A complete section token did not match the canonical section body."""

    def __init__(self, *, section: str) -> None:
        super().__init__(f"payload_sha256 does not match canonical {section} body")
        self.section = section


# One snapshot per host: the latest consolidated status push. Read back by
# StatusFoldLoop (stamped guarded health sections), host diagnostics, and the
# device-capability active-target fill — the single snapshot source.
HOST_STATUS_NAMESPACE = "status_push.host_status"

# Both health axes now move to the async facts-only reconciler (StatusFoldLoop).
# They are stamped in Txn B and folded off the request path; the synchronous
# ObservationFold tuple keeps only the cheap, lock-free folds.
GUARDED_SECTIONS = ("node_health", "device_health")
HEALTH_SECTIONS = ("node_health", "device_health")

# Only sections read back from the store are persisted: the guarded sections
# consumed by the async StatusFoldLoop, plus appium_processes for host
# diagnostics and the device-capability active-target fill. Telemetry and
# properties sections fold synchronously from the in-memory payload and are
# never read from the store, so they are not written to it.
PERSISTED_SECTIONS = ("appium_processes", *GUARDED_SECTIONS)

# Key under which Txn B publishes the Txn-A-reserved revision on each guarded
# snapshot section. The async node fold passes it to the guarded health writer.
OBSERVATION_REVISION_KEY = "observation_revision"
OBSERVATION_RECEIVED_AT_KEY = "observation_received_at"

# A Txn-B owner holds one pooled connection while convergence uses one nested
# session at a time. Cap owners at half of this worker's configured capacity so
# status pushes alone cannot occupy every connection and deadlock waiting for
# their nested convergence sessions.
MIN_STATUS_PUSH_DB_CAPACITY = 2
CONFIGURED_DB_POOL_CAPACITY: int | None = (
    None
    if core_settings.db_pool_size == 0 or core_settings.db_max_overflow < 0
    else core_settings.db_pool_size + core_settings.db_max_overflow
)
STATUS_PUSH_PUBLICATION_CONCURRENCY = max(
    1,
    (CONFIGURED_DB_POOL_CAPACITY or max(MIN_STATUS_PUSH_DB_CAPACITY, core_settings.db_pool_size)) // 2,
)


@dataclass(frozen=True)
class ObservationFold:
    """One push section folded into durable device or host facts."""

    section: str
    fold: Callable[[AsyncSession, uuid.UUID, dict[str, Any]], Awaitable[None]]


@dataclass(frozen=True)
class PendingStatusPush:
    """Txn-A result carried across restart ingest and convergence.

    ``publication_id`` identifies the unstamped snapshot written by this
    request. Txn B finalizes only while that snapshot is still latest, so a
    slower older request cannot publish over a newer push.
    """

    publication_id: str
    host_id: uuid.UUID
    boot_id: uuid.UUID | None
    received_at: str
    sections: dict[str, Any]
    prior_sections: dict[str, Any]
    reserved_revisions: dict[str, int]


def push_sections(push: HostStatusPush) -> dict[str, Any]:
    return {
        "appium_processes": push.appium_processes,
        "host_telemetry": push.host_telemetry,
        "node_health": push.node_health,
        "device_health": push.device_health,
        "device_telemetry": push.device_telemetry,
        "device_properties": push.device_properties,
    }


def _persisted_payload(sections: dict[str, Any]) -> dict[str, Any]:
    return {name: sections[name] for name in PERSISTED_SECTIONS if name in sections}


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
        apply_pushed_emulator_state: (
            Callable[[AsyncSession, uuid.UUID, dict[str, Any]], Awaitable[None]] | None
        ) = None,
        publication_concurrency: int | None = None,
    ) -> None:
        self._publisher = publisher
        self._session_factory = session_factory
        self._observation_folds = observation_folds
        self._converge_host = converge_host
        self._ingest_restart_events = ingest_restart_events
        self._apply_pushed_emulator_state = apply_pushed_emulator_state
        if (
            publication_concurrency is None
            and CONFIGURED_DB_POOL_CAPACITY is not None
            and CONFIGURED_DB_POOL_CAPACITY < MIN_STATUS_PUSH_DB_CAPACITY
        ):
            raise RuntimeError("status-push convergence requires at least two database connections per worker")
        concurrency = (
            publication_concurrency if publication_concurrency is not None else STATUS_PUSH_PUBLICATION_CONCURRENCY
        )
        if concurrency < 1:
            raise ValueError("publication_concurrency must be at least 1")
        self._publication_slots = asyncio.Semaphore(concurrency)

    @asynccontextmanager
    async def publication_slot(self) -> AsyncIterator[None]:
        """Reserve pool headroom before the endpoint checks out its Txn-B connection."""
        async with self._publication_slots:
            yield

    async def begin_status_push(self, db: AsyncSession, host: Host, push: HostStatusPush) -> PendingStatusPush:
        """Txn A: validate/fence, persist liveness, and publish unstamped data.

        The caller holds the host row lock and commits this transaction before
        restart ingest and convergence. Guarded sections deliberately carry no
        backend observation revision yet, so ``StatusFoldLoop`` cannot consume
        process-health evidence against pre-convergence node identity.
        """
        self._validate_section_hashes(push)
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

        sections = copy.deepcopy(push_sections(push))
        # observation_revision is backend-owned on both health axes. Strip an
        # agent-supplied value before reserving the backend ordering point.
        for name in HEALTH_SECTIONS:
            section = sections.get(name)
            if isinstance(section, dict):
                section.pop(OBSERVATION_REVISION_KEY, None)
        # Reserve the guard ordering point at ingest, but do not expose it in the
        # snapshot until Txn B. Synchronous restart/convergence writers between
        # the transactions therefore draw higher revisions and still win.
        reserved_revisions = {
            name: await next_observation_revision(db)
            for name in GUARDED_SECTIONS
            if isinstance(sections.get(name), dict)
        }
        prior = await control_plane_state_store.get_value(db, HOST_STATUS_NAMESPACE, str(host.id))
        prior_sections = prior.get("payload") if isinstance(prior, dict) else None
        if not isinstance(prior_sections, dict):
            prior_sections = {}
        publication_id = str(uuid.uuid4())
        received_at = now_utc().isoformat()
        await control_plane_state_store.set_value(
            db,
            HOST_STATUS_NAMESPACE,
            str(host.id),
            {
                "publication_id": publication_id,
                "boot_id": str(push.boot_id) if push.boot_id is not None else None,
                "received_at": received_at,
                "payload": _persisted_payload(sections),
            },
        )
        record_host_status_push(host_id=str(host.id))
        return PendingStatusPush(
            publication_id=publication_id,
            host_id=host.id,
            boot_id=push.boot_id,
            received_at=received_at,
            sections=sections,
            prior_sections=copy.deepcopy(prior_sections),
            reserved_revisions=reserved_revisions,
        )

    async def finalize_status_push(
        self,
        db: AsyncSession,
        host: Host,
        pending: PendingStatusPush,
    ) -> dict[str, Any] | None:
        """Txn B: re-fence and stamp guarded sections after convergence.

        Returns the synchronous fold payload. ``None`` means another request
        replaced the pending snapshot while convergence ran; that newer request
        owns publication and this request performs no folds.
        """
        self._apply_boot_fence(host, pending.boot_id)
        current = await control_plane_state_store.get_value(db, HOST_STATUS_NAMESPACE, str(host.id))
        if not isinstance(current, dict) or current.get("publication_id") != pending.publication_id:
            return None

        sections = copy.deepcopy(pending.sections)
        fold_payload = dict(sections)
        for name in GUARDED_SECTIONS:
            section = sections.get(name)
            if not isinstance(section, dict):
                continue
            revision, advanced, stale, first_received_at = await self._advance_section_cursor(
                db,
                host,
                name,
                section,
                boot_id=pending.boot_id,
                candidate_revision=pending.reserved_revisions.get(name),
                received_at=pending.received_at,
            )
            if advanced:
                section[OBSERVATION_REVISION_KEY] = revision
                section[OBSERVATION_RECEIVED_AT_KEY] = first_received_at
                continue

            fold_payload.pop(name, None)
            prior_section = pending.prior_sections.get(name)
            if stale:
                if isinstance(prior_section, dict):
                    sections[name] = copy.deepcopy(prior_section)
                else:
                    sections.pop(name, None)
            else:
                # Exact re-delivery: convergence still ran for the newest process
                # snapshot, but the health generation retains its existing revision.
                section[OBSERVATION_REVISION_KEY] = revision
                section[OBSERVATION_RECEIVED_AT_KEY] = first_received_at

        finalized = dict(current)
        finalized["payload"] = _persisted_payload(sections)
        await control_plane_state_store.set_value(db, HOST_STATUS_NAMESPACE, str(host.id), finalized)
        return fold_payload

    async def pending_is_current(self, db: AsyncSession, host: Host, pending: PendingStatusPush) -> bool:
        """Re-fence and verify that this request still owns the latest snapshot.

        The endpoint calls this after taking the Txn-B host lock and keeps that
        lock through convergence and finalization. That makes process-identity
        convergence single-file per host while avoiding work for a request that
        a newer Txn A already superseded.
        """
        self._apply_boot_fence(host, pending.boot_id)
        current = await control_plane_state_store.get_value(db, HOST_STATUS_NAMESPACE, str(host.id))
        return isinstance(current, dict) and current.get("publication_id") == pending.publication_id

    @staticmethod
    def _validate_section_hashes(push: HostStatusPush) -> None:
        sections = push_sections(push)
        for name in GUARDED_SECTIONS:
            section = sections.get(name)
            if not isinstance(section, dict):
                continue
            token = extract_token(section, boot_id=push.boot_id)
            if token is None:
                continue
            if canonical_section_hash(section) != token.payload_sha256:
                record_host_push_token_anomaly("hash_mismatch")
                raise SectionHashMismatchError(section=name)

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
        self,
        db: AsyncSession,
        host: Host,
        name: str,
        section: dict[str, Any],
        *,
        boot_id: uuid.UUID | None,
        received_at: str,
        candidate_revision: int | None = None,
    ) -> tuple[int, bool, bool, str]:
        """Compare the section's token against the host's per-section cursor.

        Returns ``(revision, advanced, stale, first_received_at)``. On a genuine advance draw and
        stamp a fresh revision and move the cursor; on a re-delivery/stale token
        reuse the cursor's revision. ``stale`` distinguishes a lower sequence
        (restore the prior snapshot body) from an exact re-delivery.
        """
        token = extract_token(section, boot_id=boot_id)
        if token is None:
            # Tokenless section: fresh revision every push (no dedup, at-least-once).
            revision = candidate_revision or await next_observation_revision(db)
            return revision, True, False, received_at
        computed = canonical_section_hash(section)
        cursors = host.observation_cursors if isinstance(host.observation_cursors, dict) else {}
        cursor = cursors.get(name)
        advanced = True
        stale = False
        if isinstance(cursor, dict):
            cursor_seq = cursor.get("section_sequence")
            if token.boot_id == cursor.get("boot_id") and isinstance(cursor_seq, int):
                if token.section_sequence < cursor_seq:
                    advanced = False  # stale/out-of-order delivery
                    stale = True
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
                prior_received_at = cursor.get("received_at") if isinstance(cursor, dict) else None
                first_received_at = prior_received_at if isinstance(prior_received_at, str) else received_at
                return reused, False, stale, first_received_at
        revision = candidate_revision or await next_observation_revision(db)
        new_cursors = dict(cursors)
        new_cursors[name] = {
            "boot_id": token.boot_id,
            "section_sequence": token.section_sequence,
            "payload_sha256": computed,
            "revision": revision,
            "received_at": received_at,
        }
        host.observation_cursors = new_cursors
        return revision, True, False, received_at

    async def process_prepublication(
        self,
        *,
        host_id: uuid.UUID,
        host_ip: str,
        agent_port: int,
        payload: dict[str, Any],
    ) -> bool:
        """Run restart ingest and convergence before guarded publication.

        Restart-event failure is contained independently. Convergence failure
        returns ``False`` so the request leaves the snapshot unstamped and the
        next push can retry the publication barrier.
        """
        if self._session_factory is None:
            return True
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
        if self._converge_host is None:
            return True
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
            return False
        self._log_stage("convergence", host_id, started)
        return True

    async def process_observation_folds(self, *, host_id: uuid.UUID, payload: dict[str, Any]) -> None:
        """Run the observation folds that remain synchronous after Txn B."""
        if self._session_factory is None:
            return
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
        # device_health folds async on the StatusFoldLoop (Phase 4), but its cheap
        # lifecycle-state (emulator_state) application stays synchronous per spec:
        # read the guarded section's lifecycle items here and apply write-on-diff (M2).
        await self._apply_emulator_state(host_id=host_id, payload=payload)

    async def _apply_emulator_state(self, *, host_id: uuid.UUID, payload: dict[str, Any]) -> None:
        if self._apply_pushed_emulator_state is None or self._session_factory is None:
            return
        section = payload.get("device_health")
        if not isinstance(section, dict):
            return
        started = perf_counter()
        try:
            async with self._session_factory() as em_db:
                await self._apply_pushed_emulator_state(em_db, host_id, section)
                await em_db.commit()
        except Exception:  # noqa: BLE001 - observation stages must never starve liveness
            HOST_PUSH_OBSERVATION_FAILURES.labels(stage="emulator_state").inc()
        self._log_stage("emulator_state", host_id, started)

    async def process_observations(
        self, *, host_id: uuid.UUID, host_ip: str, agent_port: int, payload: dict[str, Any]
    ) -> None:
        """Run restart ingest, convergence, and folds without raising to the endpoint."""
        converged = await self.process_prepublication(
            host_id=host_id,
            host_ip=host_ip,
            agent_port=agent_port,
            payload=payload,
        )
        if converged:
            await self.process_observation_folds(host_id=host_id, payload=payload)

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
