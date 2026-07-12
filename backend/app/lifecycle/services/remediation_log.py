"""Append-only remediation memory: appends, derivation, and the policy-view synthesizer (P12).

Supersession replaces erasure — see WS-15.1.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select

from app.core.timeutil import now_utc
from app.devices.models import DeviceRemediationLogEntry

if TYPE_CHECKING:
    import uuid
    from collections.abc import Sequence

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.core.protocols import SettingsReader

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


async def append_entry(
    db: AsyncSession,
    device_id: uuid.UUID,
    *,
    kind: str,
    source: str,
    action: str,
    reason: str | None = None,
    backoff_until: datetime | None = None,
) -> DeviceRemediationLogEntry:
    entry = DeviceRemediationLogEntry(
        device_id=device_id,
        kind=kind,
        source=source,
        action=action,
        reason=reason,
        backoff_until=backoff_until,
        at=now_utc(),
    )
    db.add(entry)
    await db.flush()
    return entry


async def append_attempt(
    db: AsyncSession,
    device_id: uuid.UUID,
    *,
    source: str,
    reason: str,
    settings: SettingsReader,
) -> tuple[DeviceRemediationLogEntry, LadderState]:
    prior = await load_ladder(db, device_id)
    attempts = prior.attempts + 1
    base = settings.get_int("general.lifecycle_recovery_backoff_base_sec")
    cap = max(base, settings.get_int("general.lifecycle_recovery_backoff_max_sec"))
    seconds = min(cap, base * (2 ** (attempts - 1)))
    entry = await append_entry(
        db,
        device_id,
        kind=KIND_ATTEMPT,
        source=source,
        action="recovery_failed",
        reason=reason,
        backoff_until=now_utc() + timedelta(seconds=seconds),
    )
    assert entry.backoff_until is not None
    ladder = LadderState(
        attempts=attempts,
        backoff_until=entry.backoff_until,
        last_failure_source=source,
        last_failure_reason=reason,
        last_action="recovery_failed",
        last_action_at=entry.at,
    )
    return entry, ladder


async def append_failure(
    db: AsyncSession, device_id: uuid.UUID, *, source: str, reason: str
) -> DeviceRemediationLogEntry:
    return await append_entry(
        db,
        device_id,
        kind=KIND_FAILURE,
        source=source,
        action="failure_observed",
        reason=reason,
    )


async def append_reset(
    db: AsyncSession,
    device_id: uuid.UUID,
    *,
    source: str,
    action: str,
    reason: str | None = None,
) -> DeviceRemediationLogEntry:
    return await append_entry(db, device_id, kind=KIND_RESET, source=source, action=action, reason=reason)


async def append_action(
    db: AsyncSession,
    device_id: uuid.UUID,
    *,
    source: str,
    action: str,
    reason: str | None = None,
) -> DeviceRemediationLogEntry:
    return await append_entry(db, device_id, kind=KIND_ACTION, source=source, action=action, reason=reason)


async def load_ladder(db: AsyncSession, device_id: uuid.UUID) -> LadderState:
    stmt = select(DeviceRemediationLogEntry).where(DeviceRemediationLogEntry.device_id == device_id)
    result = await db.execute(stmt)
    return derive_ladder(result.scalars().all())


async def load_ladders(db: AsyncSession, device_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, LadderState]:
    ids = list(device_ids)
    if not ids:
        return {}
    stmt = select(DeviceRemediationLogEntry).where(DeviceRemediationLogEntry.device_id.in_(ids))
    result = await db.execute(stmt)
    by_device: dict[uuid.UUID, list[DeviceRemediationLogEntry]] = defaultdict(list)
    for entry in result.scalars().all():
        by_device[entry.device_id].append(entry)
    return {device_id: derive_ladder(by_device.get(device_id, [])) for device_id in ids}


async def load_active_backoffs(db: AsyncSession, *, now: datetime) -> dict[uuid.UUID, datetime]:
    entry_model = DeviceRemediationLogEntry
    reset_result = await db.execute(
        select(entry_model.device_id, func.max(entry_model.at))
        .where(entry_model.kind == KIND_RESET)
        .group_by(entry_model.device_id)
    )
    resets: dict[uuid.UUID, datetime] = {device_id: reset_at for device_id, reset_at in reset_result.all()}
    attempt_result = await db.execute(
        select(entry_model.device_id, entry_model.at, entry_model.backoff_until).where(
            entry_model.kind == KIND_ATTEMPT,
            entry_model.backoff_until > now,
        )
    )
    out: dict[uuid.UUID, datetime] = {}
    for device_id, at, backoff_until in attempt_result.all():
        reset_at = resets.get(device_id)
        if reset_at is not None and at <= reset_at:
            continue
        assert backoff_until is not None
        existing = out.get(device_id)
        if existing is None or backoff_until > existing:
            out[device_id] = backoff_until
    return out
