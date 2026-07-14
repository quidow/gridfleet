"""Level-triggered reconciler that folds pushed node_health snapshots.

Moves the ``node_health`` observation-application off the status-push request
path. It polls (no cross-process doorbell), reads every host's latest snapshot in
one query, and folds the ``node_health`` section of each host whose stamped
observation revision exceeds the host's ``observation_applied`` watermark for that
section. Because the fold debounces on duration windows (not sample counts),
reading only the latest snapshot is safe: a dropped intermediate generation still
carries the current verdict with an advancing ``observed_at``.

Per-host containment: one host raising does not abort the others in the cycle.
Per-node containment lives in ``fold_host_nodes``; a retryable node holds the
host's section-skip watermark below the snapshot revision so only that node is
retried next cycle (its committed peers are skipped by the revision guard).
"""

from __future__ import annotations

import uuid
from time import perf_counter
from typing import TYPE_CHECKING, Any, ClassVar

from sqlalchemy import select

from app.core.background_loop import BackgroundLoop
from app.core.leader import state_store as control_plane_state_store
from app.core.metrics_recorders import record_status_fold_host, record_status_fold_lag
from app.core.observability import get_logger
from app.core.timeutil import now_utc, parse_iso
from app.hosts.models import Host
from app.hosts.service_status_push import HOST_STATUS_NAMESPACE, OBSERVATION_REVISION_KEY

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.appium_nodes.services.node_health import NodeHealthService
    from app.core.type_defs import SessionFactory

logger = get_logger(__name__)

# Plumbing constant (not a registry setting): the reconciler poll interval.
STATUS_FOLD_INTERVAL_SEC = 3.0
FOLD_SECTION = "node_health"


class StatusFoldLoop(BackgroundLoop):
    loop_name: ClassVar[str] = "status_fold"
    cycle_failed_message: ClassVar[str] = "Status fold loop error"

    def __init__(self, *, node_health: NodeHealthService, session_factory: SessionFactory) -> None:
        self._node_health = node_health
        self._sessions = session_factory

    @property
    def _session_factory(self) -> SessionFactory:
        return self._sessions

    def _interval(self) -> float:
        return STATUS_FOLD_INTERVAL_SEC

    async def _run_cycle(self, db: AsyncSession) -> None:
        snapshots = await control_plane_state_store.get_values(db, HOST_STATUS_NAMESPACE)
        if not snapshots:
            return
        applied = await self._load_applied(db, list(snapshots))
        for host_key, snapshot in snapshots.items():
            try:
                await self._fold_host(host_key, snapshot, applied.get(host_key, {}))
            except Exception:
                record_status_fold_host("contained_error")
                logger.exception("status_fold_host_failed", extra={"host_id": host_key})

    async def _load_applied(self, db: AsyncSession, host_keys: list[str]) -> dict[str, dict[str, Any]]:
        host_ids: list[uuid.UUID] = []
        for key in host_keys:
            try:
                host_ids.append(uuid.UUID(key))
            except ValueError:
                continue
        if not host_ids:
            return {}
        rows = await db.execute(select(Host.id, Host.observation_applied).where(Host.id.in_(host_ids)))
        return {str(hid): (value if isinstance(value, dict) else {}) for hid, value in rows.all()}

    async def _fold_host(self, host_key: str, snapshot: object, applied: dict[str, Any]) -> None:
        payload = snapshot.get("payload") if isinstance(snapshot, dict) else None
        section = payload.get(FOLD_SECTION) if isinstance(payload, dict) else None
        if not isinstance(section, dict):
            return
        revision = section.get(OBSERVATION_REVISION_KEY)
        if not isinstance(revision, int):
            return
        prior = applied.get(FOLD_SECTION)
        if isinstance(prior, int) and revision <= prior:
            record_status_fold_host("skipped")
            return
        try:
            host_id = uuid.UUID(host_key)
        except ValueError:
            return

        started = perf_counter()
        async with self._session_factory() as host_db:
            settled = await self._node_health.fold_host_nodes(host_db, host_id, section)
        record_status_fold_host("folded")
        received = parse_iso(snapshot.get("received_at")) if isinstance(snapshot, dict) else None
        if received is not None:
            record_status_fold_lag(max(0.0, (now_utc() - received).total_seconds()))
        logger.debug(
            "status_fold_host_complete",
            extra={
                "host_id": host_key,
                "revision": revision,
                "settled": settled,
                "ms": round((perf_counter() - started) * 1000, 1),
            },
        )
        if settled:
            await self._advance_applied(host_id, revision)

    async def _advance_applied(self, host_id: uuid.UUID, revision: int) -> None:
        # Serializes on the same host-row lock the push endpoint takes, so the
        # loop's completion watermark cannot race the endpoint's cursor publish.
        async with self._session_factory() as db:
            host = await db.get(Host, host_id, with_for_update=True)
            if host is None:
                return
            applied = dict(host.observation_applied) if isinstance(host.observation_applied, dict) else {}
            current = applied.get(FOLD_SECTION)
            if not isinstance(current, int) or current < revision:
                applied[FOLD_SECTION] = revision
                host.observation_applied = applied
                await db.commit()
