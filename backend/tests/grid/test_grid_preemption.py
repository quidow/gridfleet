"""AllocationService preemption: victim selection, gates, and terminalization."""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import TYPE_CHECKING, Any

import pytest
import pytest_asyncio
from sqlalchemy import delete, select, update

from app.core.timeutil import now_utc
from app.devices import locking as device_locking
from app.devices.models import DeviceGroup, DeviceGroupMembership, DeviceReservation, GroupType
from app.devices.services.intent import IntentService
from app.grid.allocation import PREEMPTED_ERROR_TYPE, AllocationService
from app.grid.models import GridSessionQueueTicket
from app.runs.models import RunState, TestRun
from app.sessions.models import Session, SessionStatus
from app.sessions.probe_constants import PROBE_TEST_NAME
from tests.fakes.settings import FakeSettingsReader
from tests.helpers import seed_host_and_running_node
from tests.helpers import test_event_bus as event_bus
from tests.packs.factories import seed_test_packs

if TYPE_CHECKING:
    from collections.abc import Collection, Sequence
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy.sql.elements import ColumnElement

    from app.devices.models import Device

pytestmark = pytest.mark.usefixtures("seeded_driver_packs")


def _body(**caps: str) -> dict[str, Any]:
    return {"capabilities": {"alwaysMatch": caps, "firstMatch": [{}]}}


async def _stereotype_stub(
    db: AsyncSession, device: Device, *, template_cache: object | None = None, matching_group_keys: Collection[str] = ()
) -> dict[str, Any]:
    """Mirrors device_match_surface: identity keys plus a boolean cap per group
    key the membership index says matches. Advertising the group keys is what
    makes a gridfleet:group:<key> candidate matchable at all."""
    surface: dict[str, Any] = {"platformName": "Android", "gridfleet:deviceId": str(device.id)}
    surface.update({f"gridfleet:group:{key}": True for key in matching_group_keys})
    return surface


def _service() -> AllocationService:
    return AllocationService(
        intent_factory=IntentService,
        publisher=event_bus,
        stereotype_provider=_stereotype_stub,
        settings=FakeSettingsReader({}),
    )


def _running(
    device_id: uuid.UUID, *, last_activity_at: datetime | None = None, test_name: str | None = None
) -> Session:
    return Session(
        session_id=f"appium-{uuid.uuid4().hex}",
        device_id=device_id,
        status=SessionStatus.running,
        last_activity_at=last_activity_at,
        test_name=test_name,
    )


async def _ticket(db: AsyncSession, **caps: str) -> GridSessionQueueTicket:
    ticket = GridSessionQueueTicket(requested_body=_body(**caps))
    db.add(ticket)
    await db.flush()
    return ticket


@pytest_asyncio.fixture
async def packs(db_session: AsyncSession) -> None:
    await seed_test_packs(db_session)


@pytest.mark.db
async def test_prepare_preemption_picks_the_stalest_session(db_session: AsyncSession, packs: None) -> None:
    _, fresh, _ = await seed_host_and_running_node(db_session, identity=f"pre-fresh-{uuid.uuid4().hex[:8]}")
    _, stale, _ = await seed_host_and_running_node(db_session, identity=f"pre-stale-{uuid.uuid4().hex[:8]}")
    now = now_utc()
    db_session.add_all(
        [
            _running(fresh.id, last_activity_at=now - timedelta(minutes=1)),
            _running(stale.id, last_activity_at=now - timedelta(hours=2)),
        ]
    )
    ticket = await _ticket(db_session, platformName="Android")
    await db_session.commit()

    victim = await _service().prepare_preemption(db_session, ticket_id=ticket.id)

    assert victim is not None
    assert victim.device_id == stale.id


@pytest.mark.db
async def test_prepare_preemption_returns_none_when_no_device_matches(db_session: AsyncSession, packs: None) -> None:
    _, device, _ = await seed_host_and_running_node(db_session, identity=f"pre-nomatch-{uuid.uuid4().hex[:8]}")
    db_session.add(_running(device.id))
    ticket = await _ticket(db_session, platformName="iOS")
    await db_session.commit()

    assert await _service().prepare_preemption(db_session, ticket_id=ticket.id) is None


@pytest.mark.db
async def test_prepare_preemption_skips_probe_sessions(db_session: AsyncSession, packs: None) -> None:
    _, device, _ = await seed_host_and_running_node(db_session, identity=f"pre-probe-{uuid.uuid4().hex[:8]}")
    db_session.add(_running(device.id, test_name=PROBE_TEST_NAME))
    ticket = await _ticket(db_session, platformName="Android")
    await db_session.commit()

    assert await _service().prepare_preemption(db_session, ticket_id=ticket.id) is None


@pytest.mark.db
async def test_prepare_preemption_skips_pending_claims(db_session: AsyncSession, packs: None) -> None:
    _, device, _ = await seed_host_and_running_node(db_session, identity=f"pre-pending-{uuid.uuid4().hex[:8]}")
    db_session.add(Session(session_id=f"alloc-{uuid.uuid4()}", device_id=device.id, status=SessionStatus.pending))
    ticket = await _ticket(db_session, platformName="Android")
    await db_session.commit()

    assert await _service().prepare_preemption(db_session, ticket_id=ticket.id) is None


@pytest.mark.db
async def test_free_ticket_does_not_preempt_a_reserved_device(db_session: AsyncSession, packs: None) -> None:
    _, device, _ = await seed_host_and_running_node(db_session, identity=f"pre-reserved-{uuid.uuid4().hex[:8]}")
    run = TestRun(
        id=uuid.uuid4(),
        name="preemption-reserved-run",
        state=RunState.active,
        requirements=[],
        ttl_minutes=10,
        heartbeat_timeout_sec=300,
        last_heartbeat=now_utc(),
    )
    db_session.add(run)
    await db_session.flush()
    db_session.add(
        DeviceReservation(
            run_id=run.id,
            device_id=device.id,
            identity_value=device.identity_value,
            connection_target=device.connection_target,
            pack_id=device.pack_id,
            platform_id=device.platform_id,
            os_version=device.os_version,
        )
    )
    db_session.add(_running(device.id))
    ticket = await _ticket(db_session, platformName="Android")
    await db_session.commit()

    assert await _service().prepare_preemption(db_session, ticket_id=ticket.id) is None


@pytest.mark.db
async def test_group_selector_limits_the_victim_pool(db_session: AsyncSession, packs: None) -> None:
    """A gridfleet:group:<key> request may only preempt devices in that group."""
    _, member, _ = await seed_host_and_running_node(db_session, identity=f"pre-in-{uuid.uuid4().hex[:8]}")
    _, outsider, _ = await seed_host_and_running_node(db_session, identity=f"pre-out-{uuid.uuid4().hex[:8]}")
    group = DeviceGroup(key=f"pre-grp-{uuid.uuid4().hex[:8]}", name="preemption pool", group_type=GroupType.static)
    db_session.add(group)
    await db_session.flush()
    db_session.add(DeviceGroupMembership(group_id=group.id, device_id=member.id))
    db_session.add_all([_running(member.id), _running(outsider.id)])
    ticket = GridSessionQueueTicket(
        requested_body={
            "capabilities": {
                "alwaysMatch": {"platformName": "Android", f"gridfleet:group:{group.key}": True},
                "firstMatch": [{}],
            }
        }
    )
    db_session.add(ticket)
    await db_session.commit()

    victim = await _service().prepare_preemption(db_session, ticket_id=ticket.id)

    assert victim is not None
    assert victim.device_id == member.id


@pytest.mark.db
async def test_available_dynamic_group_does_not_admit_a_busy_victim(db_session: AsyncSession, packs: None) -> None:
    _, device, _ = await seed_host_and_running_node(db_session, identity=f"pre-status-{uuid.uuid4().hex[:8]}")
    group = DeviceGroup(
        key=f"pre-available-{uuid.uuid4().hex[:8]}",
        name="available devices",
        group_type=GroupType.dynamic,
        filters={"status": "available"},
    )
    db_session.add_all([group, _running(device.id)])
    await db_session.flush()
    ticket = await _ticket(db_session, platformName="Android", **{f"gridfleet:group:{group.key}": True})
    await db_session.commit()

    assert await _service().prepare_preemption(db_session, ticket_id=ticket.id) is None


@pytest.mark.db
async def test_prepare_preemption_rechecks_static_membership_under_lock(db_session: AsyncSession, packs: None) -> None:
    _, device, _ = await seed_host_and_running_node(db_session, identity=f"pre-group-race-{uuid.uuid4().hex[:8]}")
    group = DeviceGroup(
        key=f"pre-race-{uuid.uuid4().hex[:8]}",
        name="race group",
        group_type=GroupType.static,
    )
    db_session.add(group)
    await db_session.flush()
    membership = DeviceGroupMembership(group_id=group.id, device_id=device.id)
    db_session.add_all([membership, _running(device.id)])
    ticket = await _ticket(db_session, platformName="Android", **{f"gridfleet:group:{group.key}": True})
    await db_session.commit()

    async def remove_membership_after_match(
        db: AsyncSession,
        matched_device: Device,
        *,
        template_cache: object | None = None,
        matching_group_keys: Collection[str] = (),
    ) -> dict[str, Any]:
        del template_cache
        await db.execute(delete(DeviceGroupMembership).where(DeviceGroupMembership.id == membership.id))
        surface: dict[str, Any] = {"platformName": "Android", "gridfleet:deviceId": str(matched_device.id)}
        surface.update({f"gridfleet:group:{key}": True for key in matching_group_keys})
        return surface

    service = AllocationService(
        intent_factory=IntentService,
        publisher=event_bus,
        stereotype_provider=remove_membership_after_match,
        settings=FakeSettingsReader({}),
    )

    assert await service.prepare_preemption(db_session, ticket_id=ticket.id) is None


@pytest.mark.db
async def test_prepare_preemption_does_not_switch_to_a_replacement_session(
    db_session: AsyncSession,
    packs: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, device, _ = await seed_host_and_running_node(db_session, identity=f"pre-replace-{uuid.uuid4().hex[:8]}")
    original = _running(device.id, last_activity_at=now_utc() - timedelta(hours=2))
    db_session.add(original)
    ticket = await _ticket(db_session, platformName="Android")
    await db_session.commit()
    real_lock = device_locking.lock_device_handle

    async def replace_session_before_lock_read(
        db: AsyncSession,
        device_id: uuid.UUID,
        *,
        load_sessions: bool = False,
        predicates: Sequence[ColumnElement[bool]] = (),
    ) -> device_locking.LockedDevice:
        locked = await real_lock(db, device_id, load_sessions=load_sessions, predicates=predicates)
        await db.execute(
            update(Session)
            .where(Session.id == original.id, Session.device_id == device_id)
            .values(status=SessionStatus.passed, ended_at=now_utc())
        )
        db.add(_running(device_id, last_activity_at=now_utc()))
        await db.flush()
        return locked

    monkeypatch.setattr(device_locking, "lock_device_handle", replace_session_before_lock_read)

    assert await _service().prepare_preemption(db_session, ticket_id=ticket.id) is None


@pytest.mark.db
async def test_finalize_preemption_terminalizes_and_frees_the_device(db_session: AsyncSession, packs: None) -> None:
    _, device, _ = await seed_host_and_running_node(db_session, identity=f"pre-final-{uuid.uuid4().hex[:8]}")
    row = _running(device.id)
    db_session.add(row)
    ticket = await _ticket(db_session, platformName="Android")
    await db_session.commit()
    service = _service()

    victim = await service.prepare_preemption(db_session, ticket_id=ticket.id)
    assert victim is not None
    assert await service.finalize_preemption(db_session, session_pk=victim.session_pk, device_id=victim.device_id)
    await db_session.commit()

    closed = (await db_session.execute(select(Session).where(Session.id == victim.session_pk))).scalar_one()
    assert closed.status == SessionStatus.error
    assert closed.error_type == PREEMPTED_ERROR_TYPE
    assert closed.ended_at is not None
