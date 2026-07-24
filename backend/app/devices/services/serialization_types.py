from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import uuid
    from datetime import datetime

    from app.devices.models import DeviceOperationalState
    from app.devices.services.readiness import DeviceReadiness


@dataclass(frozen=True)
class DeviceSerializationContext:
    """Per-device values precomputed in batch by
    ``DevicePresenterService.build_serialization_contexts`` so ``serialize_device``
    can skip its per-device pack-catalog queries.

    Lives in this leaf module (rather than ``presenter``) so ``protocols`` can
    reference the type without importing ``presenter`` and forming an import cycle.
    """

    readiness: DeviceReadiness
    blocked_reason: str | None
    operational_state: DeviceOperationalState


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
