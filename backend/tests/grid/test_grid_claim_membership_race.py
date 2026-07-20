"""Group membership must be re-checked under the claim's device row lock.

Membership is decided against the pre-lock eligible batch. The ``Device`` row
lock does not serialize ``DeviceGroupMembership`` edits, so a membership DELETE
can commit between that batch read and the ``INSERT INTO sessions`` and the
device would still be routed to a ``gridfleet:group:<key>`` request. Run
allocation already closes this window (``_batch_select_devices`` step 7b); these
tests pin the same guard on the grid claim path.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy import delete, select

from app.devices import locking as device_locking
from app.devices.models import DeviceGroup, DeviceGroupMembership, GroupType
from app.devices.services.intent import IntentService
from app.grid.allocation import AllocationService
from app.grid.models import GridSessionQueueTicket
from app.sessions.models import Session
from tests.helpers import seed_host_and_running_node
from tests.helpers import test_event_bus as event_bus
from tests.packs.factories import seed_test_packs

if TYPE_CHECKING:
    from collections.abc import Collection, Sequence

    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy.sql.elements import ColumnElement

    from app.devices.locking import LockedDevice
    from app.devices.models import Device

pytestmark = pytest.mark.usefixtures("seeded_driver_packs")


def _body(**caps: str | bool) -> dict[str, Any]:
    return {"capabilities": {"alwaysMatch": caps, "firstMatch": [{}]}}


async def _stereotype_stub(
    db: AsyncSession,
    device: Device,
    *,
    template_cache: object | None = None,
    matching_group_keys: Collection[str] = (),
) -> dict[str, Any]:
    surface: dict[str, Any] = {"platformName": "Android"}
    surface.update({f"gridfleet:group:{key}": True for key in matching_group_keys})
    return surface


def _service() -> AllocationService:
    return AllocationService(
        intent_factory=IntentService,
        publisher=event_bus,
        stereotype_provider=_stereotype_stub,
    )


async def _seed_group_member_device(db_session: AsyncSession, group_key: str) -> Device:
    await seed_test_packs(db_session)
    _host, device, _ = await seed_host_and_running_node(db_session, identity=f"claim-race-{uuid.uuid4().hex[:8]}")
    group = DeviceGroup(key=group_key, name=group_key, group_type=GroupType.static)
    db_session.add(group)
    await db_session.flush()
    db_session.add(DeviceGroupMembership(group_id=group.id, device_id=device.id))
    await db_session.flush()
    return device


@pytest.mark.db
async def test_membership_removed_between_poll_and_claim_prevents_session(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A membership DELETE landing after the row lock must abort the claim."""
    device = await _seed_group_member_device(db_session, "claim-race")
    ticket = GridSessionQueueTicket(
        requested_body=_body(platformName="Android", **{"gridfleet:group:claim-race": True})
    )
    db_session.add(ticket)
    await db_session.flush()

    real_lock = device_locking.lock_device_handle

    async def racing_lock(
        db: AsyncSession,
        device_id: uuid.UUID,
        *,
        load_sessions: bool = False,
        predicates: Sequence[ColumnElement[bool]] = (),
    ) -> LockedDevice:
        locked = await real_lock(db, device_id, load_sessions=load_sessions, predicates=predicates)
        # The operator drops the device from the group while the claim holds the
        # device row lock. Under READ COMMITTED a read issued after this point
        # observes the removal; the pre-lock batch snapshot does not.
        await db.execute(delete(DeviceGroupMembership).where(DeviceGroupMembership.device_id == device_id))
        return locked

    monkeypatch.setattr(device_locking, "lock_device_handle", racing_lock)

    result = await _service().try_allocate(db_session, ticket=ticket)

    assert result is None, "claim must decline a device that left the requested group under the lock"
    session_row = (await db_session.execute(select(Session).where(Session.device_id == device.id))).first()
    assert session_row is None, "no session may be created for a device that is no longer a group member"


@pytest.mark.db
async def test_intact_membership_still_claims_under_the_lock(db_session: AsyncSession) -> None:
    """Control: with membership untouched the same setup allocates normally, so
    the test above fails on the recheck rather than on the harness."""
    device = await _seed_group_member_device(db_session, "claim-stable")
    ticket = GridSessionQueueTicket(
        requested_body=_body(platformName="Android", **{"gridfleet:group:claim-stable": True})
    )
    db_session.add(ticket)
    await db_session.flush()

    result = await _service().try_allocate(db_session, ticket=ticket)

    assert result is not None
    assert result.device_id == device.id
