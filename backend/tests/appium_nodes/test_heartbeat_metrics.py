from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from app.appium_nodes.services.heartbeat import APPIUM_RESTART_SEQUENCE_NAMESPACE, _ingest_appium_restart_events
from app.core.leader import state_store as control_plane_state_store
from app.core.metrics_recorders import (
    APPIUM_RESTART_EVENTS_SUPPRESSED_TOTAL,
    HEARTBEAT_PING_TOTAL,
    record_heartbeat_ping,
)
from app.hosts.models import Host, HostStatus, OSType
from tests.helpers import test_event_bus

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


def test_heartbeat_ping_metric_increments_with_labels() -> None:
    sample_before = HEARTBEAT_PING_TOTAL.labels(host_id="hid", outcome="success", client_mode="pooled")._value.get()  # type: ignore[attr-defined]
    record_heartbeat_ping(
        host_id="hid",
        outcome="success",
        client_mode="pooled",
        duration_seconds=0.012,
    )
    sample_after = HEARTBEAT_PING_TOTAL.labels(host_id="hid", outcome="success", client_mode="pooled")._value.get()  # type: ignore[attr-defined]
    assert sample_after == sample_before + 1


def test_heartbeat_ping_helper_exported_via_app_metrics() -> None:
    from app.core.metrics_recorders import record_heartbeat_ping as exported

    assert exported is record_heartbeat_ping


async def test_appium_restart_events_suppressed_total_counts_same_boot_replay_only(db_session: AsyncSession) -> None:
    """Suppression counter: a genuine same-boot replay increments the unlabeled
    counter by exactly one. Invalid shapes, unknown kinds, and invalid ports
    are filtered before the sequence comparison and must not increment it."""
    boot_id = uuid.uuid4()
    host = Host(
        hostname="metrics-restart-host",
        ip="10.0.0.30",
        os_type=OSType.linux,
        agent_port=5100,
        status=HostStatus.online,
        current_boot_id=boot_id,
    )
    db_session.add(host)
    await db_session.commit()

    await control_plane_state_store.set_value(
        db_session,
        APPIUM_RESTART_SEQUENCE_NAMESPACE,
        str(host.id),
        {"boot_id": str(boot_id), "sequence": 5},
    )
    await db_session.commit()

    before = APPIUM_RESTART_EVENTS_SUPPRESSED_TOTAL._value.get()  # type: ignore[attr-defined]

    await _ingest_appium_restart_events(
        db_session,
        host,
        {
            "appium_processes": {
                "recent_restart_events": [
                    {"sequence": 5, "port": 4723, "kind": "crash_detected"},  # same-boot replay: suppressed
                    {"sequence": 6, "port": "bad", "kind": "crash_detected"},  # invalid port: not counted
                    {"sequence": 7, "port": 4723, "kind": "unknown"},  # unknown kind: not counted
                ]
            }
        },
        publisher=test_event_bus,
    )

    after = APPIUM_RESTART_EVENTS_SUPPRESSED_TOTAL._value.get()  # type: ignore[attr-defined]
    assert after == before + 1
