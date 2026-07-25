from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import uuid
    from datetime import datetime

    from app.devices.models import DeviceOperationalState
    from app.devices.services.readiness import DeviceReadiness
    from app.devices.services.recovery_projection import RecoveryAvailability
    from app.lifecycle.services.remediation_log import LadderState


@dataclass(frozen=True)
class ReservationReadFacts:
    """Scalars copied off a device's active reservation (run + entry) while the read
    session is open, so the synchronous DTO/policy builders can project run-tracking
    and reservation reads without touching the ORM rows.

    ``exclusion_kind`` is the plain ``str`` value of ``DeviceReservation.exclusion_kind``
    (a ``String(16)`` column), mirroring ``decision_snapshot.py`` — compare it with
    ``==``/``!=`` against ``ExclusionKind`` members, never ``is``.
    """

    run_id: uuid.UUID
    run_name: str
    run_state: str
    run_terminal: bool
    excluded: bool
    exclusion_kind: str | None
    exclusion_reason: str | None
    excluded_at: datetime | None
    excluded_until: datetime | None
    cooldown_count: int
    blocks_allocation: bool


@dataclass(frozen=True, slots=True)
class DeviceReadProjection:
    """One device's read-time facts, frozen off the ORM row for the list DTO and
    the dynamic-group evaluator.

    Composed in batch by ``load_device_read_projections`` — never store an ORM
    row here; every field is a scalar or another frozen value object so the
    projection outlives the read session it was built under.
    """

    readiness: DeviceReadiness
    blocked_reason: str | None
    operational_state: DeviceOperationalState
    reservation: ReservationReadFacts | None
    ladder: LadderState
    recovery: RecoveryAvailability
    platform_label: str | None
    live_session: bool
    static_group_keys: frozenset[str]
    now: datetime
