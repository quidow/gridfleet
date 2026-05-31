from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import or_, select
from sqlalchemy.orm import selectinload

from app.devices.models import Device, DeviceHold, DeviceOperationalState
from app.devices.models.intent import DeviceIntent
from app.devices.models.reservation import DeviceReservation
from app.devices.services.health_view import device_allows_allocation
from app.devices.services.intent_types import NODE_PROCESS
from app.devices.services.lifecycle_policy_state import state as policy_state
from app.devices.services.readiness import is_ready_for_use_async
from app.devices.services.state import appium_node_stop_in_flight
from app.devices.services.verification_execution import verification_intent_source
from app.sessions.models import Session, SessionStatus

if TYPE_CHECKING:
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True)
class DeviceStateFacts:
    """All inputs the device-state derivation needs, pre-gathered (no IO here)."""

    has_running_session: bool  # a Session row status=running, ended_at IS NULL
    has_verification_lease: bool  # an active verification intent (§16 task 4)
    in_maintenance: bool  # lifecycle_policy_state["maintenance_reason"] set (§16.1)
    stop_in_flight: bool  # appium_node_stop_in_flight(device)
    ready: bool  # is_ready_for_use ∧ device_allows_allocation ∧ ¬review_required
    is_reserved: bool  # an active device_reservations row


def evaluate_operational_state(facts: DeviceStateFacts) -> DeviceOperationalState:
    """Derive the 4-value operational axis. Stage 1: no `maintenance` value (that lives in `hold`)."""
    if facts.has_running_session:
        return DeviceOperationalState.busy
    if facts.has_verification_lease:
        return DeviceOperationalState.verifying
    if facts.stop_in_flight or not facts.ready:
        return DeviceOperationalState.offline
    return DeviceOperationalState.available


def evaluate_hold(facts: DeviceStateFacts) -> DeviceHold | None:
    """Derive the back-compat `hold` mirror. `maintenance` > `reserved` (§4)."""
    if facts.in_maintenance:
        return DeviceHold.maintenance
    if facts.is_reserved:
        return DeviceHold.reserved
    return None


async def gather_device_state_facts(db: AsyncSession, device: Device, *, now: datetime) -> DeviceStateFacts:
    """Gather all inputs needed for state derivation via async DB queries.

    ``device`` must be persistent (committed or flushed) in *db*.  The
    function refreshes ``device.appium_node`` eagerly so that
    ``device_allows_allocation`` and ``appium_node_stop_in_flight`` can
    inspect the node without triggering synchronous lazy loading.
    """
    # Reload the device with appium_node eager-loaded so health-view helpers
    # can access it synchronously without triggering MissingGreenlet.
    device = (
        await db.execute(select(Device).where(Device.id == device.id).options(selectinload(Device.appium_node)))
    ).scalar_one()

    has_running_session = (
        await db.execute(
            select(Session.id)
            .where(
                Session.device_id == device.id,
                Session.status == SessionStatus.running,
                Session.ended_at.is_(None),
            )
            .limit(1)
        )
    ).first() is not None

    has_verification_lease = (
        await db.execute(
            select(DeviceIntent.id)
            .where(
                DeviceIntent.device_id == device.id,
                DeviceIntent.axis == NODE_PROCESS,
                DeviceIntent.source == verification_intent_source(device.id),
                or_(DeviceIntent.expires_at.is_(None), DeviceIntent.expires_at > now),
            )
            .limit(1)
        )
    ).first() is not None

    is_reserved = (
        await db.execute(
            select(DeviceReservation.id)
            .where(
                DeviceReservation.device_id == device.id,
                DeviceReservation.released_at.is_(None),
            )
            .limit(1)
        )
    ).first() is not None

    in_maintenance = policy_state(device).get("maintenance_reason") is not None
    ready = await is_ready_for_use_async(db, device) and device_allows_allocation(device) and not device.review_required

    return DeviceStateFacts(
        has_running_session=has_running_session,
        has_verification_lease=has_verification_lease,
        in_maintenance=in_maintenance,
        stop_in_flight=appium_node_stop_in_flight(device),
        ready=ready,
        is_reserved=is_reserved,
    )
