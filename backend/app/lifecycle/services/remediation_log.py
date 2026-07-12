"""Append-only remediation memory: appends, derivation, and the policy-view synthesizer (P12).

Supersession replaces erasure — see WS-15.1.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime

    from app.devices.models import DeviceRemediationLogEntry

KIND_ATTEMPT = "attempt"
KIND_FAILURE = "failure"
KIND_RESET = "reset"
KIND_ACTION = "action"


@dataclass(frozen=True)
class LadderState:
    attempts: int
    backoff_until: datetime | None
    last_failure_source: str | None
    last_failure_reason: str | None
    last_action: str | None
    last_action_at: datetime | None

    @property
    def armed(self) -> bool:
        return self.attempts > 0

    def backoff_active(self, *, now: datetime) -> datetime | None:
        if self.backoff_until is not None and self.backoff_until > now:
            return self.backoff_until
        return None


EMPTY_LADDER = LadderState(0, None, None, None, None, None)


def derive_ladder(entries: Sequence[DeviceRemediationLogEntry]) -> LadderState:
    ordered = sorted(entries, key=lambda entry: (entry.at, str(entry.id)))
    if not ordered:
        return EMPTY_LADDER

    window: list[DeviceRemediationLogEntry] = []
    for entry in ordered:
        if entry.kind == KIND_RESET:
            window = []
        else:
            window.append(entry)

    attempts = [entry for entry in window if entry.kind == KIND_ATTEMPT]
    failures = [entry for entry in window if entry.kind in (KIND_ATTEMPT, KIND_FAILURE)]
    last = ordered[-1]
    last_attempt = attempts[-1] if attempts else None
    last_failure = failures[-1] if failures else None
    return LadderState(
        attempts=len(attempts),
        backoff_until=last_attempt.backoff_until if last_attempt is not None else None,
        last_failure_source=last_failure.source if last_failure is not None else None,
        last_failure_reason=last_failure.reason if last_failure is not None else None,
        last_action=last.action,
        last_action_at=last.at,
    )


def build_policy_view(ladder: LadderState, raw: dict[str, Any] | None) -> dict[str, Any]:
    base = raw if isinstance(raw, dict) else {}
    return {
        "maintenance_reason": base.get("maintenance_reason"),
        "deferred_stop": bool(base.get("deferred_stop", False)),
        "deferred_stop_reason": base.get("deferred_stop_reason"),
        "deferred_stop_since": base.get("deferred_stop_since"),
        "backoff_until": ladder.backoff_until.isoformat() if ladder.backoff_until is not None else None,
        "recovery_backoff_attempts": ladder.attempts,
        "last_failure_source": ladder.last_failure_source,
        "last_failure_reason": ladder.last_failure_reason,
        "last_action": ladder.last_action,
        "last_action_at": ladder.last_action_at.isoformat() if ladder.last_action_at is not None else None,
    }
