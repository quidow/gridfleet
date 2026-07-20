"""Grid allocation must derive readiness, not assert it.

``_ready_sql`` uses ``verified_at`` as a stand-in and cannot express the
pack-manifest setup-fields axis, so a device missing a
``required_for_session`` device field passes the SQL eligibility gate while its
real readiness state is ``setup_required``. The grid allocator built its group
facts with a hardcoded ``readiness_state="verified"``, so such a device evaluated
``needs_attention=False`` there and ``True`` everywhere else — a dynamic group
like ``{"needs_attention": true}`` never matched in grid allocation and the
session hung in the queue until timeout.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy import update

from app.devices.models import Device, DeviceGroup, GroupType
from app.devices.services.group_membership import load_group_membership_index, load_groups_by_keys
from app.devices.services.intent import IntentService
from app.grid.allocation import AllocationService
from app.grid.models import GridSessionQueueTicket
from tests.fakes import FakeSettingsReader
from tests.helpers import seed_host_and_running_node
from tests.helpers import test_event_bus as event_bus
from tests.packs.factories import seed_test_packs

if TYPE_CHECKING:
    from collections.abc import Collection

    from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = [pytest.mark.asyncio, pytest.mark.db]

_settings = FakeSettingsReader({})


async def _stereotype_stub(
    db: AsyncSession,
    device: Device,
    *,
    template_cache: object | None = None,
    matching_group_keys: Collection[str] = (),
) -> dict[str, Any]:
    surface: dict[str, Any] = {"platformName": "Roku"}
    surface.update({f"gridfleet:group:{key}": True for key in matching_group_keys})
    return surface


def _service() -> AllocationService:
    return AllocationService(
        intent_factory=IntentService,
        publisher=event_bus,
        stereotype_provider=_stereotype_stub,
        settings=_settings,
    )


async def _seed_setup_required_device(db_session: AsyncSession) -> Device:
    """A verified, node-running device on a pack platform whose
    ``roku_password`` field is ``required_for_session`` — and unset. It passes
    ``is_available_sql`` (which only checks ``verified_at``) but assesses
    ``setup_required``."""
    await seed_test_packs(db_session)
    _host, device, _node = await seed_host_and_running_node(db_session, identity=f"readiness-{uuid.uuid4().hex[:8]}")
    await db_session.execute(
        update(Device)
        .where(Device.id == device.id)
        .values(pack_id="appium-roku-dlenroc", platform_id="roku_network", device_config={})
    )
    await db_session.flush()
    await db_session.refresh(device)
    return device


async def test_setup_required_device_evaluates_identically_in_both_paths(db_session: AsyncSession) -> None:
    """The grid allocator's facts and ``load_group_membership_index``'s facts must
    report the same readiness state and the same ``needs_attention`` for a device
    whose pack manifest requires a setup field it lacks."""
    device = await _seed_setup_required_device(db_session)
    service = _service()

    rows = await service._eligible_devices_with_facts(db_session, group_keys=())
    assert device.id in {row.device.id for row in rows}, "device must clear the SQL eligibility gate"
    _templates, grid_facts, _catalog = await service._eligible_facts(db_session, rows)

    group = DeviceGroup(
        key=f"attn-{uuid.uuid4().hex[:8]}",
        name="attn",
        group_type=GroupType.dynamic,
        filters={"needs_attention": True},
    )
    db_session.add(group)
    await db_session.flush()
    groups = await load_groups_by_keys(db_session, [group.key])
    index = await load_group_membership_index(db_session, groups=groups, devices=[device], settings=_settings)

    assert grid_facts[device.id].readiness_state == "setup_required"
    assert grid_facts[device.id].needs_attention is True
    # The membership index is the reference projection: the device is a member of
    # a needs_attention group, so grid allocation must agree it needs attention.
    assert device.id in index.device_ids(group.key)


async def test_needs_attention_group_routes_in_grid_allocation(db_session: AsyncSession) -> None:
    """End to end: a ticket pinning a ``{"needs_attention": true}`` dynamic group
    allocates the setup_required device instead of hanging in the queue."""
    device = await _seed_setup_required_device(db_session)
    key = f"attn-route-{uuid.uuid4().hex[:8]}"
    db_session.add(DeviceGroup(key=key, name=key, group_type=GroupType.dynamic, filters={"needs_attention": True}))
    await db_session.flush()
    ticket = GridSessionQueueTicket(
        requested_body={
            "capabilities": {
                "alwaysMatch": {"platformName": "Roku", f"gridfleet:group:{key}": True},
                "firstMatch": [{}],
            }
        }
    )
    db_session.add(ticket)
    await db_session.flush()

    result = await _service().try_allocate(db_session, ticket=ticket)

    assert result is not None
    assert result.device_id == device.id
