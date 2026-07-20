"""Query-budget regressions for group-routed grid allocation (Task 4).

Asserts the database read count of a free group-routed poll is constant (four
SELECT/CTE reads) regardless of fleet size, plus companion assertions for the
no-group free poll (three reads), the run-scoped no-match poll (five reads),
and the successful claim (five reads before the first ``INSERT INTO
sessions``: the three free-poll reads, the joined lock read, and the
live-session recheck that closes the concurrent-claim race under READ
COMMITTED).
"""

from __future__ import annotations

import contextlib
import uuid
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy import event, select, update

if TYPE_CHECKING:
    from collections.abc import Collection, Iterator

    from sqlalchemy.ext.asyncio import AsyncSession

from app.devices.models import Device, DeviceGroup, DeviceGroupMembership, GroupType
from app.devices.services.intent import IntentService
from app.grid.allocation import AllocationService
from app.grid.models import GridSessionQueueTicket
from app.runs.models import RunState, TestRun
from tests.helpers import seed_host_and_running_node
from tests.helpers import test_event_bus as event_bus
from tests.packs.factories import seed_test_packs

pytestmark = pytest.mark.usefixtures("seeded_driver_packs")


@contextlib.contextmanager
def _capture_statements(session: AsyncSession) -> Iterator[list[str]]:
    statements: list[str] = []

    def listener(
        conn: object,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        statements.append(statement)

    bind = session.bind
    assert bind is not None
    sync_engine = bind.sync_engine if hasattr(bind, "sync_engine") else bind
    event.listen(sync_engine, "before_cursor_execute", listener)
    try:
        yield statements
    finally:
        event.remove(sync_engine, "before_cursor_execute", listener)


def _reads(statements: list[str]) -> list[str]:
    return [stmt for stmt in statements if stmt.lstrip().upper().startswith(("SELECT", "WITH"))]


def _reads_before_first_session_insert(statements: list[str]) -> list[str]:
    reads: list[str] = []
    for stmt in statements:
        lowered = stmt.lstrip().lower()
        if lowered.startswith("insert") and "into sessions" in lowered:
            break
        if lowered.startswith(("select", "with")):
            reads.append(stmt)
    return reads


def _body(**caps: str) -> dict[str, Any]:
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


_PACK_PLATFORMS = [
    ("appium-uiautomator2", "android_mobile"),
    ("appium-uiautomator2", "android_tv"),
    ("appium-uiautomator2", "firetv_real"),
    ("appium-xcuitest", "ios"),
    ("appium-xcuitest", "tvos"),
    ("appium-roku-dlenroc", "roku_network"),
]


async def seed_no_match_poll(
    db_session: AsyncSession,
    *,
    devices: int,
    groups: int,
    platforms: int,
) -> GridSessionQueueTicket:
    """Seed a fleet and a ticket that requests a valid group matching no device.

    The ticket's requested group is a freshly-created static group with no
    members, so the poll cannot match any device and ``try_allocate`` returns
    None without cancelling the ticket (the group exists, it just has no
    eligible members).
    """
    await seed_test_packs(db_session)
    host_id: uuid.UUID | None = None
    for i in range(devices):
        host, _, _ = await seed_host_and_running_node(db_session, identity=f"budget-{uuid.uuid4().hex[:8]}")
        host_id = host.id
        # Cycle through the requested platform count so the fleet exercises the
        # batch pack-template load across multiple (pack_id, platform_id) pairs.
        pack_id, platform_id = _PACK_PLATFORMS[i % len(_PACK_PLATFORMS)]
        # Adjust the last device's pack/platform by updating the row (the helper
        # seeds appium-uiautomator2/android_mobile by default).
        if (pack_id, platform_id) != ("appium-uiautomator2", "android_mobile"):
            await db_session.execute(
                update(Device).where(Device.host_id == host_id).values(pack_id=pack_id, platform_id=platform_id)
            )
            await db_session.flush()
    assert host_id is not None

    # Seed ``groups`` groups, half static (some with members on odd-indexed
    # devices) and half dynamic with a member_of reference to a static group.
    static_keys: list[str] = []
    for i in range(groups):
        key = f"g-{uuid.uuid4().hex[:8]}"
        if i % 2 == 0:
            db_session.add(DeviceGroup(key=key, name=key, group_type=GroupType.static))
            static_keys.append(key)
        else:
            member_of = static_keys[-1] if static_keys else None
            filters: dict[str, Any] = {"platform_id": _PACK_PLATFORMS[i % len(_PACK_PLATFORMS)][1]}
            if member_of is not None:
                filters["member_of"] = [member_of]
            db_session.add(DeviceGroup(key=key, name=key, group_type=GroupType.dynamic, filters=filters or None))
    await db_session.flush()

    # Add a few static memberships to exercise the static-key aggregation path.
    if static_keys and devices:
        first_device = (await db_session.execute(select(Device).limit(1).order_by(Device.created_at))).scalar_one()
        group_row = (
            await db_session.execute(select(DeviceGroup).where(DeviceGroup.key == static_keys[0]))
        ).scalar_one()
        db_session.add(DeviceGroupMembership(group_id=group_row.id, device_id=first_device.id))
        await db_session.flush()

    # The ticket requests a freshly-created empty static group so it cannot match.
    miss_key = f"miss-{uuid.uuid4().hex[:8]}"
    db_session.add(DeviceGroup(key=miss_key, name=miss_key, group_type=GroupType.static))
    await db_session.flush()
    ticket = GridSessionQueueTicket(
        requested_body=_body(platformName="Android", **{f"gridfleet:group:{miss_key}": True})
    )
    db_session.add(ticket)
    await db_session.flush()
    return ticket


async def seed_member_of_poll(
    db_session: AsyncSession,
    *,
    devices: int,
    groups: int,
    platforms: int,
) -> GridSessionQueueTicket:
    """Seed a fleet and a ticket that requests a dynamic group whose
    ``filters.member_of`` references one or more static groups.

    The requested dynamic group's filters combine ``member_of`` (pointing at static
    groups with no members) with a ``platform_id`` constraint that no seeded device
    satisfies, so the poll is a free no-match (the group is valid but matches no
    eligible device). This exercises the ``member_of`` closure path in the
    group-definition loader — the recursive CTE must fold the requested dynamic
    group and its static targets into a single read.
    """
    await seed_test_packs(db_session)
    host_id: uuid.UUID | None = None
    for i in range(devices):
        host, _, _ = await seed_host_and_running_node(db_session, identity=f"budget-mof-{uuid.uuid4().hex[:8]}")
        host_id = host.id
        pack_id, platform_id = _PACK_PLATFORMS[i % len(_PACK_PLATFORMS)]
        if (pack_id, platform_id) != ("appium-uiautomator2", "android_mobile"):
            await db_session.execute(
                update(Device).where(Device.host_id == host_id).values(pack_id=pack_id, platform_id=platform_id)
            )
            await db_session.flush()
    assert host_id is not None

    # Seed a chain of static groups (the ``member_of`` targets) and a single
    # dynamic group that references all of them. ``groups`` controls closure
    # width so the test scales the member_of fan-out, not just fleet size.
    static_keys: list[str] = []
    for _i in range(max(1, groups)):
        key = f"pool-{uuid.uuid4().hex[:8]}"
        db_session.add(DeviceGroup(key=key, name=key, group_type=GroupType.static))
        static_keys.append(key)
    await db_session.flush()

    # A platform_id no seeded device can have — guarantees a no-match free poll
    # regardless of fleet composition, so the read count is observable.
    miss_platform = "no-such-platform"
    dyn_key = f"dyn-{uuid.uuid4().hex[:8]}"
    db_session.add(
        DeviceGroup(
            key=dyn_key,
            name=dyn_key,
            group_type=GroupType.dynamic,
            filters={"member_of": static_keys, "platform_id": miss_platform},
        )
    )
    await db_session.flush()

    ticket = GridSessionQueueTicket(
        requested_body=_body(platformName="Android", **{f"gridfleet:group:{dyn_key}": True})
    )
    db_session.add(ticket)
    await db_session.flush()
    return ticket


@pytest.mark.parametrize(
    ("devices", "groups", "platforms"),
    [(1, 1, 1), (25, 12, 8)],
)
@pytest.mark.db
async def test_group_routed_free_poll_has_constant_read_budget(
    db_session: AsyncSession,
    devices: int,
    groups: int,
    platforms: int,
) -> None:
    ticket = await seed_no_match_poll(db_session, devices=devices, groups=groups, platforms=platforms)
    service = _service()
    with _capture_statements(db_session) as statements:
        result = await service.try_allocate(db_session, ticket=ticket)
    assert result is None
    assert len(_reads(statements)) == 4, (
        f"group free poll reads grew with scale: {_reads(statements)} (devices={devices}, groups={groups})"
    )


@pytest.mark.parametrize(
    ("devices", "groups", "platforms"),
    [(1, 1, 1), (25, 12, 8)],
)
@pytest.mark.db
async def test_member_of_dynamic_group_free_poll_has_constant_read_budget(
    db_session: AsyncSession,
    devices: int,
    groups: int,
    platforms: int,
) -> None:
    """A free poll that *requests* a dynamic group whose ``filters.member_of``
    references static groups must still cost 4 reads: the recursive CTE folds the
    requested dynamic group and its static ``member_of`` targets into a single
    group-definition read. The closure is over ``member_of`` edges from any loaded
    group back to its static targets, so a multi-hop closure (dynamic -> static)
    resolves in the same one statement."""
    ticket = await seed_member_of_poll(db_session, devices=devices, groups=groups, platforms=platforms)
    service = _service()
    with _capture_statements(db_session) as statements:
        result = await service.try_allocate(db_session, ticket=ticket)
    assert result is None
    reads = _reads(statements)
    assert len(reads) == 4, (
        f"member_of dynamic-group free poll reads grew with scale: {reads} (devices={devices}, groups={groups})"
    )


@pytest.mark.db
async def test_no_group_free_poll_has_three_reads(db_session: AsyncSession) -> None:
    """A free poll with no group selectors skips the group-definition read (3 reads)."""
    await seed_test_packs(db_session)
    _, _, _ = await seed_host_and_running_node(db_session, identity=f"budget-nogrp-{uuid.uuid4().hex[:8]}")
    ticket = GridSessionQueueTicket(requested_body=_body(platformName="iOS"))
    db_session.add(ticket)
    await db_session.flush()
    service = _service()
    with _capture_statements(db_session) as statements:
        result = await service.try_allocate(db_session, ticket=ticket)
    assert result is None
    assert len(_reads(statements)) == 3, _reads(statements)


@pytest.mark.db
async def test_run_scoped_no_match_poll_has_five_reads(db_session: AsyncSession) -> None:
    """A run-bound group-routed no-match poll issues five reads: scalar Run.state,
    older-waiter candidate sets, referenced groups, eligible devices + facts, and
    the pack-template batch."""
    await seed_test_packs(db_session)
    _, _, _ = await seed_host_and_running_node(db_session, identity=f"budget-run-{uuid.uuid4().hex[:8]}")
    # An empty static group so the ticket is valid but matches no device.
    miss_key = f"miss-run-{uuid.uuid4().hex[:8]}"
    db_session.add(DeviceGroup(key=miss_key, name=miss_key, group_type=GroupType.static))
    run = TestRun(
        name="budget-run",
        state=RunState.active,
        requirements=[],
        ttl_minutes=10,
        heartbeat_timeout_sec=300,
    )
    db_session.add(run)
    await db_session.flush()
    ticket = GridSessionQueueTicket(
        requested_body=_body(platformName="Android", **{f"gridfleet:group:{miss_key}": True}),
        run_id=run.id,
    )
    db_session.add(ticket)
    await db_session.flush()
    service = _service()
    with _capture_statements(db_session) as statements:
        result = await service.try_allocate(db_session, ticket=ticket)
    assert result is None
    assert len(_reads(statements)) == 5, _reads(statements)


@pytest.mark.db
async def test_successful_claim_adds_one_joined_lock_read_before_session_insert(
    db_session: AsyncSession,
) -> None:
    """A successful claim issues exactly one additional joined ``FOR UPDATE`` read
    and one fresh-snapshot live-session recheck before the first
    ``INSERT INTO sessions``. The lock-time rechecks (availability, node
    viability/acceptance, reservation owner) are folded into the lock query;
    the live-session recheck is a separate read because ``FOR UPDATE OF devices``
    does not re-evaluate a correlated sessions subquery after a concurrent claim
    commits (the row is not updated, so the waiting transaction's WHERE
    snapshot stands). Post-claim reconcile reads occur after the insert and are
    outside the budget."""
    await seed_test_packs(db_session)
    _, _, _ = await seed_host_and_running_node(db_session, identity=f"budget-claim-{uuid.uuid4().hex[:8]}")
    ticket = GridSessionQueueTicket(requested_body=_body(platformName="Android"))
    db_session.add(ticket)
    await db_session.flush()
    service = _service()
    with _capture_statements(db_session) as statements:
        result = await service.try_allocate(db_session, ticket=ticket)
    assert result is not None
    pre_insert_reads = _reads_before_first_session_insert(statements)
    # 3 (no-group free poll reads) + 1 (joined lock read) + 1 (live-session recheck) = 5
    assert len(pre_insert_reads) == 5, pre_insert_reads


@pytest.mark.db
async def test_unknown_group_key_cancels_ticket_with_400(db_session: AsyncSession) -> None:
    """A ticket requesting a group key that does not exist is cancelled and raises
    CapabilityMergeError (HTTP 400) — the missing-key rejection Task 3 deferred."""
    from app.grid.matching import CapabilityMergeError

    await seed_test_packs(db_session)
    _, _, _ = await seed_host_and_running_node(db_session, identity=f"budget-unknown-{uuid.uuid4().hex[:8]}")
    ticket = GridSessionQueueTicket(
        requested_body=_body(platformName="Android", **{"gridfleet:group:does-not-exist": True})
    )
    db_session.add(ticket)
    await db_session.flush()
    service = _service()
    with pytest.raises(CapabilityMergeError, match="unknown device group"):
        await service.try_allocate(db_session, ticket=ticket)
    from app.grid.models import GridQueueStatus

    assert ticket.status == GridQueueStatus.cancelled


@pytest.mark.db
async def test_static_group_routes_to_member_device(db_session: AsyncSession) -> None:
    """A ticket requesting a static group matches a device that is a member of it."""
    await seed_test_packs(db_session)
    _host, device, _ = await seed_host_and_running_node(db_session, identity=f"budget-static-{uuid.uuid4().hex[:8]}")
    group = DeviceGroup(key="east-lab", name="east-lab", group_type=GroupType.static)
    db_session.add(group)
    await db_session.flush()
    db_session.add(DeviceGroupMembership(group_id=group.id, device_id=device.id))
    await db_session.flush()
    ticket = GridSessionQueueTicket(requested_body=_body(platformName="Android", **{"gridfleet:group:east-lab": True}))
    db_session.add(ticket)
    await db_session.flush()
    result = await _service().try_allocate(db_session, ticket=ticket)
    assert result is not None
    assert result.device_id == device.id


@pytest.mark.db
async def test_dynamic_group_member_of_routes_correctly(db_session: AsyncSession) -> None:
    """A dynamic group with a ``member_of`` reference to a static group resolves
    membership correctly: only devices in the static group AND matching the
    dynamic filters belong to the dynamic group."""
    await seed_test_packs(db_session)
    _host, device_in, _ = await seed_host_and_running_node(db_session, identity=f"budget-dyn-in-{uuid.uuid4().hex[:8]}")
    _, _device_out, _ = await seed_host_and_running_node(db_session, identity=f"budget-dyn-out-{uuid.uuid4().hex[:8]}")
    static = DeviceGroup(key="pool", name="pool", group_type=GroupType.static)
    db_session.add(static)
    await db_session.flush()
    # Only device_in is a member of the static group.
    db_session.add(DeviceGroupMembership(group_id=static.id, device_id=device_in.id))
    await db_session.flush()
    dyn = DeviceGroup(
        key="android-pool",
        name="android-pool",
        group_type=GroupType.dynamic,
        filters={"member_of": ["pool"], "platform_id": "android_mobile"},
    )
    db_session.add(dyn)
    await db_session.flush()
    ticket = GridSessionQueueTicket(
        requested_body=_body(platformName="Android", **{"gridfleet:group:android-pool": True})
    )
    db_session.add(ticket)
    await db_session.flush()
    result = await _service().try_allocate(db_session, ticket=ticket)
    assert result is not None
    assert result.device_id == device_in.id


@pytest.mark.db
async def test_membership_change_between_polls_observed_on_next_poll(db_session: AsyncSession) -> None:
    """A group edit committed between two polls is observed on the next poll —
    the membership snapshot is per-poll, matching the live-projection contract."""
    await seed_test_packs(db_session)
    _host, device, _ = await seed_host_and_running_node(db_session, identity=f"budget-mem-{uuid.uuid4().hex[:8]}")
    group = DeviceGroup(key="flip", name="flip", group_type=GroupType.static)
    db_session.add(group)
    await db_session.flush()
    membership = DeviceGroupMembership(group_id=group.id, device_id=device.id)
    db_session.add(membership)
    await db_session.flush()
    ticket = GridSessionQueueTicket(requested_body=_body(platformName="Android", **{"gridfleet:group:flip": True}))
    db_session.add(ticket)
    await db_session.flush()
    service = _service()
    first = await service.try_allocate(db_session, ticket=ticket)
    assert first is not None
    # Remove membership: a fresh ticket requesting the same group no longer matches.
    await db_session.delete(membership)
    await db_session.flush()
    second_ticket = GridSessionQueueTicket(
        requested_body=_body(platformName="Android", **{"gridfleet:group:flip": True})
    )
    db_session.add(second_ticket)
    await db_session.flush()
    second = await service.try_allocate(db_session, ticket=second_ticket)
    assert second is None


@pytest.mark.db
async def test_fifo_veto_differs_by_group_selector(db_session: AsyncSession) -> None:
    """Two older waiters with disjoint group selectors do not veto each other's
    devices — the FIFO veto is reservation-aware AND group-aware through the
    candidate matcher (the stereotype advertises only the device's matching
    group keys)."""
    from datetime import UTC, datetime, timedelta

    await seed_test_packs(db_session)
    _, device_a, _ = await seed_host_and_running_node(db_session, identity=f"budget-fifo-a-{uuid.uuid4().hex[:8]}")
    _, device_b, _ = await seed_host_and_running_node(db_session, identity=f"budget-fifo-b-{uuid.uuid4().hex[:8]}")
    group_a = DeviceGroup(key="lab-a", name="lab-a", group_type=GroupType.static)
    group_b = DeviceGroup(key="lab-b", name="lab-b", group_type=GroupType.static)
    db_session.add_all([group_a, group_b])
    await db_session.flush()
    db_session.add_all(
        [
            DeviceGroupMembership(group_id=group_a.id, device_id=device_a.id),
            DeviceGroupMembership(group_id=group_b.id, device_id=device_b.id),
        ]
    )
    await db_session.flush()
    now = datetime.now(UTC)
    older_a = GridSessionQueueTicket(
        requested_body=_body(platformName="Android", **{"gridfleet:group:lab-a": True}),
        created_at=now - timedelta(seconds=10),
    )
    younger_b = GridSessionQueueTicket(
        requested_body=_body(platformName="Android", **{"gridfleet:group:lab-b": True}),
        created_at=now,
    )
    db_session.add_all([older_a, younger_b])
    await db_session.flush()
    older_a.last_polled_at = datetime.now(UTC)
    # The younger lab-b ticket is not blocked by the older lab-a waiter (their
    # selectors target disjoint devices).
    result = await _service().try_allocate(db_session, ticket=younger_b)
    assert result is not None
    assert result.device_id == device_b.id


@pytest.mark.db
async def test_reservation_symmetry_under_group_routing(db_session: AsyncSession) -> None:
    """A run-bound group-routed ticket allocates a reserved group-member device,
    and a free group-routed ticket does not steal it (reservation symmetry)."""
    await seed_test_packs(db_session)
    _, device, _ = await seed_host_and_running_node(db_session, identity=f"budget-res-{uuid.uuid4().hex[:8]}")
    group = DeviceGroup(key="res-pool", name="res-pool", group_type=GroupType.static)
    db_session.add(group)
    await db_session.flush()
    db_session.add(DeviceGroupMembership(group_id=group.id, device_id=device.id))
    await db_session.flush()
    run = TestRun(
        name="budget-res-run",
        state=RunState.active,
        requirements=[],
        ttl_minutes=10,
        heartbeat_timeout_sec=300,
    )
    db_session.add(run)
    await db_session.flush()
    from app.devices.models import DeviceReservation

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
    await db_session.flush()

    free_ticket = GridSessionQueueTicket(
        requested_body=_body(platformName="Android", **{"gridfleet:group:res-pool": True})
    )
    db_session.add(free_ticket)
    await db_session.flush()
    assert await _service().try_allocate(db_session, ticket=free_ticket) is None

    bound_ticket = GridSessionQueueTicket(
        requested_body=_body(platformName="Android", **{"gridfleet:group:res-pool": True}),
        run_id=run.id,
    )
    db_session.add(bound_ticket)
    await db_session.flush()
    result = await _service().try_allocate(db_session, ticket=bound_ticket)
    assert result is not None
    assert result.device_id == device.id
