from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from app.core.timeutil import now_utc
from app.devices.services.intent_synthesis import synthesize_fact_intents
from tests.helpers import create_device, create_reserved_run

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.hosts.models import Host


@pytest.mark.db
async def test_synthesis_is_empty_for_plain_device(db_session: AsyncSession, db_host: Host) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="synth-empty")
    # ``node`` is unused by every synthesis family; the real caller always passes a
    # live node (the ``node is not None`` branch of ``reconcile_device``).
    intents = await synthesize_fact_intents(db_session, device, None, [], now_utc())
    assert intents == []


@pytest.mark.db
async def test_active_reservation_synthesizes_run_routing(db_session: AsyncSession, db_host: Host) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="synth-run")
    run = await create_reserved_run(db_session, name="synth-run-r", devices=[device])
    intents = await synthesize_fact_intents(db_session, device, None, [], now_utc())
    run_intents = [i for i in intents if i.source == f"run:{run.id}"]
    assert len(run_intents) == 1
    assert run_intents[0].axis == "grid_routing"
    assert run_intents[0].payload == {"accepting_new_sessions": True, "priority": 40}
    assert run_intents[0].run_id == run.id


@pytest.mark.db
async def test_indefinitely_excluded_reservation_synthesizes_nothing(db_session: AsyncSession, db_host: Host) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="synth-run-excl")
    run = await create_reserved_run(db_session, name="synth-run-excl-r", devices=[device])
    entry = run.device_reservations[0]
    entry.excluded = True
    entry.exclusion_reason = "probe failed"
    entry.excluded_until = None
    await db_session.flush()
    intents = await synthesize_fact_intents(db_session, device, None, [], now_utc())
    assert [i for i in intents if i.source == f"run:{run.id}"] == []
