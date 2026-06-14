"""Bug 8: ``register_host`` raises IntegrityError on concurrent same-hostname registration.

See ``docs/superpowers/specs/2026-05-20-backend-bug-audit.md#bug-8``.

``register_host`` at ``backend/app/hosts/service.py:130-184`` does an
unlocked SELECT against ``Host.hostname`` and falls through to a plain
``Host(...)`` + ``db.add`` + ``await db.commit()`` when the SELECT
returns nothing. Two concurrent agent registrations with the same
hostname (e.g. an agent restart that overlaps with an in-flight
heartbeat-driven re-register) both fail the SELECT, both attempt
INSERT, and the unique constraint on ``Host.hostname`` makes one of
them surface a 500.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy.exc import IntegrityError

from app.hosts.models import Host, HostStatus, OSType
from app.hosts.schemas import HostRegister
from app.hosts.service import HostCrudService
from tests.fakes import FakeSettingsReader
from tests.helpers import test_event_bus as event_bus

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@pytest.mark.db
@pytest.mark.asyncio
async def test_register_host_races_concurrent_same_hostname(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
) -> None:
    hostname = f"race-{uuid.uuid4().hex[:8]}"

    original_execute = db_session.execute
    triggered = False

    async def _race_after_select(stmt: Any, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        nonlocal triggered
        result = await original_execute(stmt, *args, **kwargs)
        stmt_text = str(stmt).lower()
        # First SELECT against the hosts table inside ``register_host`` — the
        # one that decides "is this a new registration or a re-registration."
        # Simulate a concurrent peer registering the same hostname between
        # our snapshot and our subsequent INSERT.
        if not triggered and "from hosts" in stmt_text and "select" in stmt_text and "hostname" in stmt_text:
            triggered = True
            async with db_session_maker() as side:
                peer = Host(
                    hostname=hostname,
                    ip="10.0.99.2",
                    os_type=OSType.linux,
                    agent_port=5100,
                    agent_version="0.3.0",
                    status=HostStatus.online,
                    capabilities={
                        "orchestration_contract_version": 2,
                    },
                )
                side.add(peer)
                await side.commit()
        return result

    crud = HostCrudService(publisher=event_bus, settings=FakeSettingsReader({}))
    db_session.execute = _race_after_select  # type: ignore[assignment, method-assign]
    try:
        # Fixed behavior: register_host would catch IntegrityError (or use
        # INSERT ... ON CONFLICT) and degrade to the re-register branch.
        # Current behavior (bug): the second registrant raises
        # IntegrityError on the hostname unique constraint.
        try:
            await crud.register_host(
                db_session,
                HostRegister(
                    hostname=hostname,
                    ip="10.0.99.3",
                    os_type=OSType.linux,
                    agent_port=5100,
                    agent_version="0.3.0",
                    capabilities={"orchestration_contract_version": 2},
                ),
            )
        except IntegrityError as exc:
            pytest.fail(f"register_host raised IntegrityError on concurrent same-hostname insert: {exc}")
    finally:
        db_session.execute = original_execute  # type: ignore[method-assign]
