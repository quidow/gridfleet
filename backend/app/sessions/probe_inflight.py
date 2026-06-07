"""In-flight viability-probe visibility: in-process registry + cross-process lock.

The viability probe creates a real Appium session directly against the
device's Appium node and then deletes it. While that session exists, the
``session_sync`` loop also lists live sessions on each node and would treat
the probe's session as an orphan (no tracking ``Session`` row) and terminate
it: the Appium driver does not echo the client-side ``gridfleet:testName`` /
``gridfleet:probeSession`` markers back in matched capabilities, so the orphan
sweep cannot identify the session as a probe from caps alone.

Both the probe runner and the session_sync loop are leader-owned and share
one process, so an in-memory set keyed by device id is sufficient. The set
is populated before the probe issues its ``POST /session`` and cleared after
the cleanup ``DELETE`` returns. Entries leak only if the leader process dies
mid-probe; in that case the next session_sync cycle will pick up the orphan
session correctly because the probe is no longer happening.

Thread-safety: callers run on the leader's asyncio loop in a single CPython
process, so ``set.add`` / ``set.discard`` are atomic under the GIL and no
explicit locking is required. If this registry is ever promoted to a
cross-process or cross-host cache, it must be replaced with a locked or
externally-coordinated store; the bare ``set`` will not be safe under
multi-process writers.

The grid allocator runs on EVERY API worker, not just the leader, so it cannot
use the in-memory set. It consults the probe's DB-backed control-plane lock
instead (``viability_probe_lock_active``): the probe claims that lock before
its ``POST /session`` and releases it after cleanup, so the lock is the only
allocation-visible footprint of a mid-flight probe — no ``Session`` row exists
until the probe completes. The lock surface lives here (not in
``service_viability``) because ``service_viability`` imports from
``app.grid.allocation`` and the allocator must not import it back.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.core.leader import state_store as control_plane_state_store
from app.core.timeutil import now_utc
from app.core.timeutil import parse_iso as _parse_timestamp

if TYPE_CHECKING:
    import uuid
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncSession

SESSION_VIABILITY_RUNNING_NAMESPACE = "session_viability.running"

_VIABILITY_LOCK_STALE_TIMEOUT_FACTOR = 2
_VIABILITY_LOCK_STALE_MARGIN_SEC = 60


def viability_lock_is_stale(value: object, *, now: datetime, timeout_sec: int) -> bool:
    # Reclaim a lock ONLY when we can prove it is old. A missing/unparseable
    # ``started_at`` is treated as live (do not reclaim) — the probe always writes
    # a valid ISO timestamp, so an unreasonable one is anomalous and reclaiming it
    # could stomp a real in-progress probe.
    if not isinstance(value, dict):
        return False
    started_at = _parse_timestamp(value.get("started_at"))
    if started_at is None:
        return False
    threshold_sec = _VIABILITY_LOCK_STALE_TIMEOUT_FACTOR * timeout_sec + _VIABILITY_LOCK_STALE_MARGIN_SEC
    return (now - started_at).total_seconds() > threshold_sec


async def viability_probe_lock_active(db: AsyncSession, device_id: uuid.UUID, *, timeout_sec: int) -> bool:
    """True while a session-viability probe holds *device_id*'s control-plane lock.

    Applies the same staleness rule the probe's own reclaim path uses, so a lock
    leaked by a dead probe process does not park the device out of allocation
    forever — it stops blocking once it is provably old.
    """
    value = await control_plane_state_store.get_value(db, SESSION_VIABILITY_RUNNING_NAMESPACE, str(device_id))
    if value is None:
        return False
    return not viability_lock_is_stale(value, now=now_utc(), timeout_sec=timeout_sec)


_INFLIGHT_PROBE_DEVICE_IDS: set[str] = set()


def mark_probe_started(device_id: str) -> None:
    _INFLIGHT_PROBE_DEVICE_IDS.add(device_id)


def mark_probe_finished(device_id: str) -> None:
    _INFLIGHT_PROBE_DEVICE_IDS.discard(device_id)


def is_probe_inflight(device_id: str) -> bool:
    return device_id in _INFLIGHT_PROBE_DEVICE_IDS
