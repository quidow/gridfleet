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
from dataclasses import dataclass
from time import perf_counter
from typing import TYPE_CHECKING, Any, ClassVar

from sqlalchemy import select

from app.core.background_loop import BackgroundLoop
from app.core.leader import state_store as control_plane_state_store
from app.core.metrics_recorders import (
    record_status_fold_host,
    record_status_fold_lag,
    record_status_fold_oldest_unapplied,
)
from app.core.observability import get_logger
from app.core.timeutil import now_utc, parse_iso
from app.hosts.models import Host
from app.hosts.service_status_push import (
    HOST_STATUS_NAMESPACE,
    OBSERVATION_RECEIVED_AT_KEY,
    OBSERVATION_REVISION_KEY,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.core.type_defs import SessionFactory

logger = get_logger(__name__)

# Plumbing constant (not a registry setting): the reconciler poll interval.
STATUS_FOLD_INTERVAL_SEC = 3.0
# Bound each cycle to one poll interval so this independent lifecycle cannot
# starve the scheduler's other loops. Unfinished hosts remain unapplied and are
# naturally retried from the latest snapshot next cycle.
STATUS_FOLD_CYCLE_BUDGET_SEC = 3.0
FOLD_SECTION = "node_health"
DEVICE_FOLD_SECTION = "device_health"


@dataclass(frozen=True)
class FoldSection:
    """One pushed section folded off the request path. ``fold`` returns True when
    every device/node settled (applied/terminal_noop) so the loop advances this
    section's watermark, False when at least one node was retryable."""

    name: str
    fold: Callable[..., Awaitable[bool]]


class StatusFoldLoop(BackgroundLoop):
    loop_name: ClassVar[str] = "status_fold"
    cycle_failed_message: ClassVar[str] = "Status fold loop error"

    def __init__(self, *, sections: tuple[FoldSection, ...], session_factory: SessionFactory) -> None:
        self._sections = sections
        self._sessions = session_factory
        self._resume_after: str | None = None

    @property
    def _session_factory(self) -> SessionFactory:
        return self._sessions

    def _interval(self) -> float:
        return STATUS_FOLD_INTERVAL_SEC

    async def _run_cycle(self, db: AsyncSession) -> None:
        cycle_started = perf_counter()
        cycle_deadline = cycle_started + STATUS_FOLD_CYCLE_BUDGET_SEC
        snapshots = await control_plane_state_store.get_values(db, HOST_STATUS_NAMESPACE)
        if not snapshots:
            record_status_fold_oldest_unapplied(0.0)
            return
        applied = await self._load_applied(db, list(snapshots))
        self._record_oldest_unapplied(snapshots, applied)
        items = list(snapshots.items())
        if self._resume_after is not None:
            for index, (host_key, _snapshot) in enumerate(items):
                if host_key == self._resume_after:
                    items = items[index + 1 :] + items[: index + 1]
                    break
        for index, (host_key, snapshot) in enumerate(items):
            if index > 0 and perf_counter() - cycle_started >= STATUS_FOLD_CYCLE_BUDGET_SEC:
                record_status_fold_host("budget_deferred")
                break
            try:
                await self._fold_host(host_key, snapshot, applied.get(host_key, {}), deadline=cycle_deadline)
            except Exception:
                record_status_fold_host("contained_error")
                logger.exception("status_fold_host_failed", extra={"host_id": host_key})
            finally:
                # Rotate the next cycle after the last attempted host. A host
                # that is persistently retryable can consume its slice without
                # starving every host that followed it in snapshot order.
                self._resume_after = host_key

    def _record_oldest_unapplied(self, snapshots: dict[str, Any], applied: dict[str, dict[str, Any]]) -> None:
        """Slow-burn stall signal: age of the oldest pushed section whose stamped
        revision still exceeds this host section's applied watermark."""
        now = now_utc()
        oldest = 0.0
        for host_key, snapshot in snapshots.items():
            payload = snapshot.get("payload") if isinstance(snapshot, dict) else None
            if not isinstance(payload, dict):
                continue
            host_applied = applied.get(host_key, {})
            for entry in self._sections:
                section = payload.get(entry.name)
                if not isinstance(section, dict):
                    continue
                revision = section.get(OBSERVATION_REVISION_KEY)
                if not isinstance(revision, int):
                    continue
                prior = host_applied.get(entry.name)
                if isinstance(prior, int) and revision <= prior:
                    continue
                raw_received = section.get(OBSERVATION_RECEIVED_AT_KEY)
                if not isinstance(raw_received, str) and isinstance(snapshot, dict):
                    raw_received = snapshot.get("received_at")
                received = parse_iso(raw_received)
                if received is not None:
                    oldest = max(oldest, (now - received).total_seconds())
        record_status_fold_oldest_unapplied(max(0.0, oldest))

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

    async def _fold_host(
        self,
        host_key: str,
        snapshot: object,
        applied: dict[str, Any],
        *,
        deadline: float | None = None,
    ) -> None:
        payload = snapshot.get("payload") if isinstance(snapshot, dict) else None
        if not isinstance(payload, dict):
            return
        try:
            host_id = uuid.UUID(host_key)
        except ValueError:
            return
        raw_boot_id = snapshot.get("boot_id") if isinstance(snapshot, dict) else None
        try:
            boot_id = uuid.UUID(raw_boot_id) if isinstance(raw_boot_id, str) else None
        except ValueError:
            boot_id = None
        for entry in self._sections:
            section = payload.get(entry.name)
            if not isinstance(section, dict):
                continue
            revision = section.get(OBSERVATION_REVISION_KEY)
            if not isinstance(revision, int):
                continue
            prior = applied.get(entry.name)
            if isinstance(prior, int) and revision <= prior:
                record_status_fold_host("skipped")
                continue
            started = perf_counter()
            async with self._session_factory() as host_db:
                settled = await entry.fold(host_db, host_id, section, boot_id=boot_id, deadline=deadline)
            record_status_fold_host("folded")
            logger.debug(
                "status_fold_host_complete",
                extra={
                    "host_id": host_key,
                    "section": entry.name,
                    "revision": revision,
                    "settled": settled,
                    "ms": round((perf_counter() - started) * 1000, 1),
                },
            )
            if settled and await self._advance_applied(host_id, entry.name, revision):
                raw_received = section.get(OBSERVATION_RECEIVED_AT_KEY)
                if not isinstance(raw_received, str) and isinstance(snapshot, dict):
                    raw_received = snapshot.get("received_at")
                received = parse_iso(raw_received)
                if received is not None:
                    record_status_fold_lag(max(0.0, (now_utc() - received).total_seconds()))

    async def _advance_applied(self, host_id: uuid.UUID, section_name: str, revision: int) -> bool:
        # Serializes on the same host-row lock the push endpoint takes, so the
        # loop's completion watermark cannot race the endpoint's cursor publish.
        async with self._session_factory() as db:
            host = await db.get(Host, host_id, with_for_update=True)
            if host is None:
                return False
            applied = dict(host.observation_applied) if isinstance(host.observation_applied, dict) else {}
            current = applied.get(section_name)
            if not isinstance(current, int) or current < revision:
                applied[section_name] = revision
                host.observation_applied = applied
                await db.commit()
            return True
