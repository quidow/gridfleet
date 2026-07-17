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


def release_rollout_intent_source(device_id: UUID) -> str:
    """Return the ``source`` key used for release-rollout intents on *device_id*."""
    return f"release_rollout:{device_id}"


# Terminal-outcome stamp on the verification lease payload (WS-15.3). A stamped
# lease is a tombstone awaiting deletion: no longer an active claim
# (``claims.verification_lease_*``) and no longer a command
# (``decision.parse_command``). Verification finalization is the only stamper;
# the finalizer's revoke or the intent TTL GC deletes the row.
VERIFICATION_OUTCOME_KEY = "outcome"
VERIFICATION_OUTCOME_PASSED = "passed"
VERIFICATION_OUTCOME_FAILED = "failed"


class CommandKind(StrEnum):
    operator_stop = "operator:stop:node"
    operator_recovery_deny = "operator:stop:recovery"
    forced_release = "forced_release"
    operator_start = "operator:start"
    verification_start = "verification"
    release_rollout = "release_rollout"


@dataclass(frozen=True)
class IntentRegistration:
    source: str
    kind: CommandKind
    payload: dict[str, Any]
    run_id: UUID | None = None
    expires_at: datetime | None = None
