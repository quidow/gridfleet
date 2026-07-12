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


class CommandKind(StrEnum):
    operator_stop = "operator:stop:node"
    operator_recovery_deny = "operator:stop:recovery"
    forced_release = "forced_release"
    operator_start = "operator:start"
    verification_start = "verification"


@dataclass(frozen=True)
class IntentRegistration:
    source: str
    kind: CommandKind
    payload: dict[str, Any]
    run_id: UUID | None = None
    expires_at: datetime | None = None
