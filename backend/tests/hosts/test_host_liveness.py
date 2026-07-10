from __future__ import annotations

from datetime import timedelta

from app.core.timeutil import now_utc
from app.hosts.liveness import effective_host_status, host_online
from app.hosts.models import Host, HostStatus


def _host(status: HostStatus, *, heartbeat_age_sec: float | None) -> Host:
    now = now_utc()
    return Host(
        hostname="h",
        ip="203.0.113.10",
        os_type="linux",
        agent_port=5100,
        status=status,
        last_heartbeat=(now - timedelta(seconds=heartbeat_age_sec)) if heartbeat_age_sec is not None else None,
        created_at=now - timedelta(days=1),
    )


def test_fresh_heartbeat_reads_online_even_if_ledger_says_offline() -> None:
    host = _host(HostStatus.offline, heartbeat_age_sec=5)
    assert effective_host_status(host, offline_after_sec=45) is HostStatus.online


def test_stale_heartbeat_reads_offline_even_if_ledger_says_online() -> None:
    host = _host(HostStatus.online, heartbeat_age_sec=300)
    assert effective_host_status(host, offline_after_sec=45) is HostStatus.offline
    assert host_online(host, offline_after_sec=45) is False


def test_pending_reads_pending_regardless_of_recency() -> None:
    host = _host(HostStatus.pending, heartbeat_age_sec=1)
    assert effective_host_status(host, offline_after_sec=45) is HostStatus.pending
    assert host_online(host, offline_after_sec=45) is False


def test_never_pushed_online_host_gets_created_at_grace() -> None:
    host = _host(HostStatus.online, heartbeat_age_sec=None)
    host.created_at = now_utc() - timedelta(seconds=10)
    assert effective_host_status(host, offline_after_sec=45) is HostStatus.online


def test_never_pushed_offline_host_reads_offline_despite_fresh_created_at() -> None:
    # The operator-created POST /api/hosts row: no agent, no heartbeat, ledger
    # offline. Grace is gated on ledger online (decision 4) — must read offline.
    host = _host(HostStatus.offline, heartbeat_age_sec=None)
    host.created_at = now_utc() - timedelta(seconds=10)
    assert effective_host_status(host, offline_after_sec=45) is HostStatus.offline
    assert host_online(host, offline_after_sec=45) is False
