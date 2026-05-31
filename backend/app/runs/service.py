from __future__ import annotations

from typing import TYPE_CHECKING

from app.runs.service_query import (
    build_run_read,
    mark_reserved_device_info_includes_unavailable,
    parse_includes,
)
from app.runs.service_reservation import (
    RunReservationService as _RunReservationService,
)
from app.runs.service_reservation import (
    _reservation_entry_for_device as _reserved_entry_for_device,
)
from app.runs.service_reservation import (
    _reservation_entry_is_excluded as _reserved_entry_is_excluded,
)
from app.runs.service_reservation import (
    _reservation_entry_matches as _reserved_entry_matches,
)
from app.runs.service_reservation import (
    get_device_reservation,
    get_device_reservation_map,
    get_device_reservation_with_entry,
    get_reservation_context_for_device,
    get_reservation_entry_for_device,
    get_run,
    reservation_entry_is_excluded,
)
from app.runs.service_reservation import (
    get_run_for_update as _get_run_for_update,
)

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.runs.models import TestRun

_svc = _RunReservationService()


async def exclude_device_from_run(
    db: AsyncSession,
    device_id: uuid.UUID,
    *,
    reason: str,
    commit: bool = True,
) -> TestRun | None:
    return await _svc.exclude_device_from_run(db, device_id, reason=reason, commit=commit)


async def restore_device_to_run(
    db: AsyncSession,
    device_id: uuid.UUID,
    *,
    commit: bool = True,
) -> TestRun | None:
    return await _svc.restore_device_to_run(db, device_id, commit=commit)


__all__ = [
    "_get_run_for_update",
    "_reserved_entry_for_device",
    "_reserved_entry_is_excluded",
    "_reserved_entry_matches",
    "build_run_read",
    "exclude_device_from_run",
    "get_device_reservation",
    "get_device_reservation_map",
    "get_device_reservation_with_entry",
    "get_reservation_context_for_device",
    "get_reservation_entry_for_device",
    "get_run",
    "mark_reserved_device_info_includes_unavailable",
    "parse_includes",
    "reservation_entry_is_excluded",
    "restore_device_to_run",
]
