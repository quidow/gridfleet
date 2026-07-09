from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from datetime import datetime
    from uuid import UUID


def verification_intent_source(device_id: UUID) -> str:
    """Return the ``source`` key used for verification intents on *device_id*."""
    return f"verification:{device_id}"


def failure_stop_sources(device_id: UUID) -> list[str]:
    """Failure-driven node stop sources for *device_id*.

    The ``health_failure:node`` stop command outranks the node-start commands
    (operator start-node, verification) in the decision ladder. Both explicit
    re-qualification paths revoke it before starting a node so a leftover stop
    cannot silently block the start. (``connectivity:`` is not here — it is a fact
    read from ``device_checks_healthy`` and suppressed by an active start command,
    so there is nothing stored to revoke.)
    """
    return [
        f"health_failure:node:{device_id}",
    ]


class CommandKind(StrEnum):
    operator_stop = "operator:stop:node"
    operator_recovery_deny = "operator:stop:recovery"
    forced_release = "forced_release"
    health_failure_stop = "health_failure:node"
    operator_start = "operator:start"
    verification_start = "verification"
    auto_recovery_start = "auto_recovery:node"
    auto_recovery_allow = "auto_recovery:recovery"


@dataclass(frozen=True)
class IntentRegistration:
    source: str
    kind: CommandKind
    payload: dict[str, Any]
    run_id: UUID | None = None
    expires_at: datetime | None = None
