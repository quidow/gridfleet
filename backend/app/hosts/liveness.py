"""Read-time host liveness: online-ness derives from status-push recency.

The stored Host.status column keeps two other jobs — the enrollment axis
(``pending``) and the event ledger the sweep's edge detector writes so
host.status_changed / host.heartbeat_lost fire exactly once per real
transition. Never read the column to answer "is this host online now";
use these helpers (Python or SQL form) with general.host_offline_after_sec.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import and_, or_

from app.core.timeutil import now_utc
from app.hosts.models import Host, HostStatus

if TYPE_CHECKING:
    from sqlalchemy.sql.elements import ColumnElement


def host_online(host: Host, *, offline_after_sec: float, now: datetime | None = None) -> bool:
    if host.status == HostStatus.pending:
        return False
    moment = now or now_utc()
    if host.last_heartbeat is not None:
        return (moment - host.last_heartbeat).total_seconds() <= offline_after_sec
    # Never pushed: the created_at grace exists only to bridge register->first-push
    # for hosts enrolled online (auto-accept / approval). An operator-created
    # offline row has no agent and must read offline.
    if host.status != HostStatus.online:
        return False
    return (moment - host.created_at).total_seconds() <= offline_after_sec


def effective_host_status(host: Host, *, offline_after_sec: float, now: datetime | None = None) -> HostStatus:
    if host.status == HostStatus.pending:
        return HostStatus.pending
    online = host_online(host, offline_after_sec=offline_after_sec, now=now)
    return HostStatus.online if online else HostStatus.offline


def host_online_clause(*, offline_after_sec: float, now: datetime | None = None) -> ColumnElement[bool]:
    """SQL form of host_online, for filters and aggregate counts."""
    moment = now or now_utc()
    threshold = moment - timedelta(seconds=offline_after_sec)
    return and_(
        Host.status != HostStatus.pending,
        or_(
            and_(Host.last_heartbeat.is_not(None), Host.last_heartbeat >= threshold),
            # Never-pushed grace, gated on ledger online (decision 4).
            and_(Host.last_heartbeat.is_(None), Host.status == HostStatus.online, Host.created_at >= threshold),
        ),
    )
