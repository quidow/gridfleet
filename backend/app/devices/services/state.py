"""Writers + helpers for the orthogonal device state model.

operational_state -- what the device is doing (available/busy/offline).
hold              -- what is blocking use (maintenance/reserved/null).

Events are queued through the SQLAlchemy session so they fire on commit, not
before. Bypassing the queue causes ghost transitions when the surrounding
transaction rolls back.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import inspect as sa_inspect

from app.core.observability import get_logger
from app.devices.models import Device, DeviceHold, DeviceOperationalState
from app.devices.services.health_view import device_allows_allocation
from app.devices.services.readiness import is_ready_for_use_async
from app.events import queue_event_for_session

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy.orm import Session

    from app.events.catalog import EventSeverity

logger = get_logger(__name__)


def _persistent_session(device: Device) -> Session:
    state = sa_inspect(device, raiseerr=False)
    assert state is not None and state.persistent, (
        "Device must be persistent in a session; callers that write state "
        "must load it through lock_device in the same transaction"
    )
    session = state.session
    assert session is not None, "device has no session despite persistent==True"
    return session


async def set_operational_state(
    device: Device,
    new_state: DeviceOperationalState,
    *,
    reason: str | None = None,
    publish_event: bool = True,
    severity: EventSeverity | None = None,
) -> bool:
    session = _persistent_session(device)
    old = device.operational_state
    if old == new_state:
        return False
    device.operational_state = new_state
    if publish_event:
        payload = {
            "device_id": str(device.id),
            "device_name": device.name,
            "old_operational_state": old.value,
            "new_operational_state": new_state.value,
        }
        if reason is not None:
            payload["reason"] = reason
        queue_event_for_session(session, "device.operational_state_changed", payload, severity=severity)
    return True


async def set_hold(
    device: Device,
    new_hold: DeviceHold | None,
    *,
    reason: str | None = None,
    publish_event: bool = True,
    severity: EventSeverity | None = None,
) -> bool:
    session = _persistent_session(device)
    old = device.hold
    if old == new_hold:
        return False
    device.hold = new_hold
    if publish_event:
        payload = {
            "device_id": str(device.id),
            "device_name": device.name,
            "old_hold": old.value if old is not None else None,
            "new_hold": new_hold.value if new_hold is not None else None,
        }
        if reason is not None:
            payload["reason"] = reason
        queue_event_for_session(session, "device.hold_changed", payload, severity=severity)
    return True


async def ready_operational_state(db: AsyncSession, device: Device) -> DeviceOperationalState:
    """Project readiness into the operational axis."""
    if device.operational_state is DeviceOperationalState.verifying:
        return DeviceOperationalState.verifying
    if await is_ready_for_use_async(db, device) and device_allows_allocation(device):
        return DeviceOperationalState.available
    return DeviceOperationalState.offline


def legacy_label_for_audit(device: Device) -> str:
    """Return the legacy chip label for audit/log output only."""
    if device.hold is not None:
        return device.hold.value
    return device.operational_state.value
