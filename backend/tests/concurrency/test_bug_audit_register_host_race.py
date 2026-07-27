"""Bug 8: concurrent same-hostname registration must not surface as a 500.

See ``docs/superpowers/specs/2026-05-20-backend-bug-audit.md#bug-8``.

Two concurrent agent registrations with the same hostname (e.g. an agent restart
that overlaps an in-flight heartbeat-driven re-register) both miss the row on
their ``SELECT``, both attempt the ``INSERT``, and the unique index on
``Host.hostname`` rejects one of them.

Phase 9 moved the recovery out of ``HostCrudService``. The service performs one
attempt inside the caller's transaction and lets the conflict propagate; the
router exits that failed context and retries on a *fresh* transaction, because
a rolled-back session cannot serve the re-fetch the old code ran on it. These
tests exercise that two-transaction shape at the service level;
``tests/hosts/test_phase9_host_remote_boundaries.py`` covers the same race
end-to-end through ``POST /api/hosts/register``.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.hosts import service as host_service
from app.hosts.models import Host, HostStatus, OSType
from app.hosts.schemas import HostRegister
from app.hosts.service import HostCrudService
from tests.fakes import FakeSettingsReader
from tests.helpers import test_event_bus as event_bus

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

CAPS_V7 = {"orchestration_contract_version": 7}


def _register(hostname: str, ip: str, boot_id: uuid.UUID | None = None) -> HostRegister:
    return HostRegister(
        hostname=hostname,
        ip=ip,
        os_type=OSType.linux,
        agent_port=5100,
        capabilities=CAPS_V7,
        boot_id=boot_id,
    )


def _seed_peer(session: AsyncSession, hostname: str) -> None:
    session.add(
        Host(
            hostname=hostname,
            ip="10.0.99.2",
            os_type=OSType.linux,
            agent_port=5100,
            status=HostStatus.online,
            capabilities=CAPS_V7,
        )
    )


@pytest.mark.db
@pytest.mark.asyncio
async def test_register_host_races_concurrent_same_hostname(
    db_session_maker: async_sessionmaker[AsyncSession],
) -> None:
    hostname = f"race-{uuid.uuid4().hex[:8]}"
    triggered = False

    async with db_session_maker() as attempt:
        original_execute = attempt.execute

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
                    _seed_peer(side, hostname)
                    await side.commit()
            return result

        attempt.execute = _race_after_select  # type: ignore[assignment, method-assign]
        crud = HostCrudService(publisher=event_bus, settings=FakeSettingsReader({}))
        await attempt.begin()
        with pytest.raises(IntegrityError) as caught:
            await crud.register_host(attempt, _register(hostname, "10.0.99.3"))
        await attempt.rollback()

    assert triggered, "the racing peer never committed; the conflict was not exercised"
    # The conflict must be *identifiable* — an unrecognised integrity failure is
    # a real error, not a lost race, and must not degrade to a re-register.
    assert host_service.is_hostname_conflict(caught.value), (
        f"the hostname race raised {host_service.integrity_constraint_name(caught.value)!r}, "
        f"not {host_service.HOSTNAME_UNIQUE_INDEX!r}"
    )

    # The recovery the router performs: a brand-new transaction, re-locking the
    # winner's row. Nothing is carried over from the failed attempt.
    async with db_session_maker() as fallback, fallback.begin():
        snapshot = await crud.reregister_host(fallback, _register(hostname, "10.0.99.3"))
    assert snapshot is not None
    assert snapshot.ip == "10.0.99.3"

    async with db_session_maker() as verify:
        stored = (await verify.execute(select(Host).where(Host.hostname == hostname))).scalar_one()
    assert stored.ip == "10.0.99.3"


@pytest.mark.db
@pytest.mark.asyncio
async def test_register_host_unique_conflict_fallback_holds_boot_fence_lock(
    db_session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """The fallback's re-fetch is part of the boot-fence write window.

    Pause immediately after that SELECT. A competing status-style host lock must
    be refused until registration commits; otherwise status can validate the old
    boot between the re-fetch and the ``current_boot_id`` update.
    """
    hostname = f"race-lock-{uuid.uuid4().hex[:8]}"
    boot_id = uuid.uuid4()
    fallback_selected = asyncio.Event()
    release_registration = asyncio.Event()
    competing_refused: list[bool] = []

    async with db_session_maker() as seed:
        _seed_peer(seed, hostname)
        await seed.commit()

    crud = HostCrudService(publisher=event_bus, settings=FakeSettingsReader({}))

    async def _fallback() -> None:
        async with db_session_maker() as fallback:
            original_execute = fallback.execute

            async def _pause_after_select(stmt: Any, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
                result = await original_execute(stmt, *args, **kwargs)
                stmt_text = str(stmt).lower()
                # Gate on the Host read, not on the text "for update": keying the
                # pause on the lock itself would make a lock-free fallback stall
                # this test forever instead of failing it.
                if "from hosts" in stmt_text and "select" in stmt_text and not fallback_selected.is_set():
                    fallback_selected.set()
                    await release_registration.wait()
                return result

            fallback.execute = _pause_after_select  # type: ignore[assignment, method-assign]
            async with fallback.begin():
                await crud.reregister_host(fallback, _register(hostname, "10.0.99.3", boot_id))

    async def _lock_like_status_push() -> None:
        await fallback_selected.wait()
        async with db_session_maker() as side:
            try:
                # A real Postgres refusal, not a sleep: either the fallback holds
                # the row and this times out, or it does not and this succeeds.
                await side.execute(text("SET LOCAL lock_timeout = '400ms'"))
                await side.execute(select(Host).where(Host.hostname == hostname).with_for_update())
                competing_refused.append(False)
            except DBAPIError:
                competing_refused.append(True)
            finally:
                await side.rollback()
        release_registration.set()

    competing = asyncio.create_task(_lock_like_status_push())
    try:
        await _fallback()
    finally:
        release_registration.set()
        await competing

    assert competing_refused == [True], (
        "a competing Host FOR UPDATE acquired the row while the fallback held it; "
        "the boot fence write is not serialised against a concurrent status push"
    )
    async with db_session_maker() as verify:
        stored = (await verify.execute(select(Host).where(Host.hostname == hostname))).scalar_one()
    assert stored.current_boot_id == boot_id
