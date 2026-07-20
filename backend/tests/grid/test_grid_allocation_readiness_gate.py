"""The grid claim path must gate on the real readiness verdict, not on SQL.

``is_available_sql`` is only an approximation of ``DeviceStateFacts.ready``: its
own comment notes that the pack-manifest setup-fields axis of
``is_ready_for_use`` is not SQL-expressible and that ``verified_at`` stands in.
A device with ``verified_at`` set but missing a ``required_for_session`` device
field therefore clears the SQL gate on both sides of the claim lock while the
Python evaluator derives ``setup_required`` -> ``offline`` and the Devices page
shows it offline.

Both the candidate-selection pass and the post-lock recheck must consult the
readiness verdict that the poll already computed (no extra read), mirroring the
run allocator's steps 5 and 7b in ``_batch_select_devices``.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy import update

from app.devices.models import Device
from app.devices.services.intent import IntentService
from app.grid.allocation import AllocationService
from app.grid.models import GridQueueStatus, GridSessionQueueTicket
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


def _body(**caps: str) -> dict[str, Any]:
    return {"capabilities": {"alwaysMatch": caps, "firstMatch": [{}]}}


async def _seed_roku_device(db_session: AsyncSession, *, device_config: dict[str, Any]) -> Device:
    """A verified, node-running device on a pack platform whose ``roku_password``
    field is ``required_for_session``. With the field set the device is genuinely
    ready; with it unset the device assesses ``setup_required`` while still
    clearing ``is_available_sql`` (which only checks ``verified_at``)."""
    await seed_test_packs(db_session)
    _host, device, _node = await seed_host_and_running_node(db_session, identity=f"ready-gate-{uuid.uuid4().hex[:8]}")
    await db_session.execute(
        update(Device)
        .where(Device.id == device.id)
        .values(pack_id="appium-roku-dlenroc", platform_id="roku_network", device_config=device_config)
    )
    await db_session.flush()
    await db_session.refresh(device)
    return device


async def test_setup_required_device_is_not_claimed(db_session: AsyncSession) -> None:
    """A device missing a pack-manifest setup field cannot run a session, so the
    poll must not select it — even though it clears ``is_available_sql``."""
    device = await _seed_roku_device(db_session, device_config={})
    ticket = GridSessionQueueTicket(requested_body=_body(platformName="Roku"))
    db_session.add(ticket)
    await db_session.flush()

    result = await _service().try_allocate(db_session, ticket=ticket)

    assert result is None, f"claimed a setup_required device {device.id}"
    assert ticket.status == GridQueueStatus.waiting


async def test_claim_declines_device_that_lost_readiness_after_poll(db_session: AsyncSession) -> None:
    """A device that was ready at poll time but lost a required setup field before
    the claim must be declined under the lock, using the pack catalog the poll
    already loaded."""
    device = await _seed_roku_device(db_session, device_config={"roku_password": "set"})
    service = _service()

    rows = await service._eligible_devices_with_facts(db_session, group_keys=())
    row = next(row for row in rows if row.device.id == device.id)
    _templates, facts, pack_catalog = await service._eligible_facts(db_session, rows)
    assert facts[device.id].readiness_state == "verified", "device must be ready at poll time"

    # Readiness is lost between the poll and the claim.
    await db_session.execute(update(Device).where(Device.id == device.id).values(device_config={}))
    await db_session.flush()

    ticket = GridSessionQueueTicket(requested_body=_body(platformName="Roku"))
    db_session.add(ticket)
    await db_session.flush()

    result = await service._claim(
        db_session,
        ticket=ticket,
        row=row,
        candidate={},
        run_id=None,
        pack_catalog=pack_catalog,
    )

    assert result is None
    assert ticket.status == GridQueueStatus.waiting
