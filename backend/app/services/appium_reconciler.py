"""Leader-owned reconciler for agent-side Appium processes.

Phase 1 scope: orphan cleanup. Walks `/agent/health.appium_processes.running_nodes`
for each online host and stops any agent process that no DB AppiumNode row
in `state == running` claims. Future phases extend this loop to drive
desired-state convergence.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import uuid
    from collections.abc import Iterable

    from app.services.agent_snapshot import RunningAppiumNode


_ORPHAN_REASON_NO_DB_ROW = "no_db_row"
_ORPHAN_REASON_DB_NOT_RUNNING = "db_state_not_running"
_ORPHAN_REASON_PORT_MISMATCH = "port_mismatch"


@dataclass(frozen=True, slots=True)
class OrphanAppiumNode:
    host_id: uuid.UUID
    port: int
    connection_target: str
    reason: str


def detect_orphans(
    *,
    host_id: uuid.UUID,
    agent_running: Iterable[RunningAppiumNode],
    db_running_rows: Iterable[dict[str, object]],
) -> list[OrphanAppiumNode]:
    """Return entries running on the agent that no DB row claims.

    `db_running_rows` is a list of dicts with the keys
    `host_id`, `device_connection_target`, `node_port`, `node_state`.
    The caller is responsible for the SQL — this function is pure
    so it can be unit-tested without a database fixture.
    """
    rows_by_target: dict[str, dict[str, object]] = {}
    for db_row in db_running_rows:
        if db_row.get("host_id") != host_id:
            continue
        target = db_row.get("device_connection_target")
        if isinstance(target, str):
            rows_by_target[target] = db_row

    orphans: list[OrphanAppiumNode] = []
    for entry in agent_running:
        matched_row = rows_by_target.get(entry.connection_target)
        if matched_row is None:
            orphans.append(
                OrphanAppiumNode(
                    host_id=host_id,
                    port=entry.port,
                    connection_target=entry.connection_target,
                    reason=_ORPHAN_REASON_NO_DB_ROW,
                )
            )
            continue
        if matched_row.get("node_state") != "running":
            orphans.append(
                OrphanAppiumNode(
                    host_id=host_id,
                    port=entry.port,
                    connection_target=entry.connection_target,
                    reason=_ORPHAN_REASON_DB_NOT_RUNNING,
                )
            )
            continue
        if matched_row.get("node_port") != entry.port:
            orphans.append(
                OrphanAppiumNode(
                    host_id=host_id,
                    port=entry.port,
                    connection_target=entry.connection_target,
                    reason=_ORPHAN_REASON_PORT_MISMATCH,
                )
            )
    return orphans
