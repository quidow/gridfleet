"""create-session end-to-end: the flag gates preemption, and off means unchanged."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.appium_nodes.models import AppiumNode
from app.devices.services.health import DeviceHealthService
from app.devices.services.intent import IntentService
from app.grid import appium_direct, router_internal, session_create
from app.grid.allocation import PREEMPTED_ERROR_TYPE, AllocationResult, AllocationService
from app.grid.schemas_internal import CreateSessionRequest
from app.grid.services_container import GridServices
from app.hosts.models import Host
from app.sessions.models import Session, SessionStatus
from tests.conftest import settings_service
from tests.helpers import seed_host_and_running_node
from tests.helpers import test_event_bus as event_bus
from tests.packs.factories import seed_test_packs

if TYPE_CHECKING:
    from collections.abc import Collection

    from app.core.type_defs import SessionFactory
    from app.devices.models import Device

pytestmark = pytest.mark.usefixtures("seeded_driver_packs")

PREEMPT_KEY = "grid.preempt_running_sessions"


def _body(**caps: str) -> dict[str, Any]:
    return {"capabilities": {"alwaysMatch": caps, "firstMatch": [{}]}}


async def _stereotype_stub(
    db: AsyncSession, device: Device, *, template_cache: object | None = None, matching_group_keys: Collection[str] = ()
) -> dict[str, Any]:
    return {"platformName": "Android"}


@pytest.fixture(autouse=True)
def fast_long_poll(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(router_internal, "LONG_POLL_SEC", 0.2)
    monkeypatch.setattr(router_internal, "RETRY_INTERVAL_SEC", 0.02)


@pytest.fixture
def services(db_session: AsyncSession) -> GridServices:
    assert db_session.bind is not None
    allocation = AllocationService(
        intent_factory=IntentService,
        publisher=event_bus,
        stereotype_provider=_stereotype_stub,
        settings=settings_service,
    )
    return GridServices(
        settings=settings_service,
        session_factory=async_sessionmaker(db_session.bind, class_=AsyncSession, expire_on_commit=False),
        allocation=allocation,
        health=DeviceHealthService(publisher=event_bus),
    )


@pytest.fixture
def stub_create(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stand in for the real Appium create so the test exercises allocation only.

    Unlike the plan's verbatim stub, this still runs ``promote_to_running`` (the
    DB half of ``create_and_promote``) so the claimed ``pending`` row becomes
    ``running`` exactly as production does — only the remote Appium HTTP call is
    skipped. Without it the new session row never reaches ``running`` and the
    end-to-end assertions in ``test_flag_on_preempts_then_allocates`` cannot hold.
    """

    async def fake_create(
        db_factory: SessionFactory,
        allocation_service: AllocationService,
        *,
        allocation: AllocationResult,
        raw_body: bytes,
        claim_window_sec: int,
        max_create_timeout_sec: float | None = None,
    ) -> session_create.CreateOutcome:
        session_id = f"appium-{uuid.uuid4().hex}"
        async with db_factory.begin() as db:
            await allocation_service.promote_to_running(
                db,
                allocation_id=allocation.allocation_id,
                appium_session_id=session_id,
            )
        return session_create.CreateOutcome(
            kind="created", session_id=session_id, appium_status=200, appium_body={}, message=""
        )

    monkeypatch.setattr(session_create, "create_and_promote", fake_create)


@pytest.fixture
def no_appium_delete(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Record terminate calls instead of issuing them."""
    calls: list[tuple[str, str]] = []

    async def fake_terminate(target: str, session_id: str, *, timeout: float = 10.0) -> bool:
        calls.append((target, session_id))
        return True

    monkeypatch.setattr(appium_direct, "terminate_session", fake_terminate)
    return calls


@pytest_asyncio.fixture
async def busy_device(db_session: AsyncSession) -> tuple[Device, uuid.UUID]:
    await seed_test_packs(db_session)
    _, device, _ = await seed_host_and_running_node(db_session, identity=f"pre-e2e-{uuid.uuid4().hex[:8]}")
    row = Session(session_id=f"appium-{uuid.uuid4().hex}", device_id=device.id, status=SessionStatus.running)
    db_session.add(row)
    await db_session.commit()
    return device, row.id


@pytest.mark.db
async def test_flag_off_queues_and_leaves_the_session_alone(
    services: GridServices, busy_device: tuple[Device, uuid.UUID], db_session: AsyncSession
) -> None:
    settings_service._cache[PREEMPT_KEY] = False
    _, session_pk = busy_device

    response = await router_internal.create_session(CreateSessionRequest(body=_body(platformName="Android")), services)

    assert getattr(response, "status", None) == "queued"
    survivor = (await db_session.execute(select(Session).where(Session.id == session_pk))).scalar_one()
    assert survivor.status == SessionStatus.running
    assert survivor.ended_at is None


@pytest.mark.db
async def test_flag_on_preempts_then_allocates(
    services: GridServices,
    busy_device: tuple[Device, uuid.UUID],
    db_session: AsyncSession,
    stub_create: None,
    no_appium_delete: list[tuple[str, str]],
) -> None:
    settings_service._cache[PREEMPT_KEY] = True
    device, session_pk = busy_device

    response = await router_internal.create_session(CreateSessionRequest(body=_body(platformName="Android")), services)

    assert getattr(response, "status", None) == "created"
    assert len(no_appium_delete) == 1
    evicted = (await db_session.execute(select(Session).where(Session.id == session_pk))).scalar_one()
    assert evicted.status == SessionStatus.error
    assert evicted.error_type == PREEMPTED_ERROR_TYPE
    live = (
        (
            await db_session.execute(
                select(Session).where(
                    Session.device_id == device.id,
                    Session.status == SessionStatus.running,
                    Session.ended_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(live) == 1
    assert live[0].id != session_pk


@pytest.mark.db
async def test_flag_on_with_no_match_queues_without_killing(
    services: GridServices,
    busy_device: tuple[Device, uuid.UUID],
    db_session: AsyncSession,
    no_appium_delete: list[tuple[str, str]],
) -> None:
    settings_service._cache[PREEMPT_KEY] = True
    _, session_pk = busy_device

    response = await router_internal.create_session(CreateSessionRequest(body=_body(platformName="iOS")), services)

    assert getattr(response, "status", None) == "queued"
    assert no_appium_delete == []
    survivor = (await db_session.execute(select(Session).where(Session.id == session_pk))).scalar_one()
    assert survivor.status == SessionStatus.running


@pytest.mark.db
async def test_flag_on_kills_at_most_one_session_per_request(
    services: GridServices, db_session: AsyncSession, no_appium_delete: list[tuple[str, str]]
) -> None:
    """Two busy matching devices: exactly one terminate_session call.

    Nothing is stubbed — after the preemption frees a device, the real
    create_and_promote runs against the seeded node and fails on the closed
    loopback port, which is what keeps allocation failing for the request.
    """
    settings_service._cache[PREEMPT_KEY] = True
    await seed_test_packs(db_session)
    for suffix in ("a", "b"):
        _, device, _ = await seed_host_and_running_node(db_session, identity=f"pre-one-{suffix}-{uuid.uuid4().hex[:8]}")
        db_session.add(
            Session(session_id=f"appium-{uuid.uuid4().hex}", device_id=device.id, status=SessionStatus.running)
        )
        # Point the real create at a closed loopback port so it fails instantly
        # (connection-refused) instead of timing out on an RFC1918 address.
        host = (await db_session.execute(select(Host).where(Host.id == device.host_id))).scalar_one()
        host.ip = "127.0.0.1"
        node = (await db_session.execute(select(AppiumNode).where(AppiumNode.device_id == device.id))).scalar_one()
        node.port = 1
    await db_session.commit()

    await router_internal.create_session(CreateSessionRequest(body=_body(platformName="Android")), services)

    assert len(no_appium_delete) == 1


@pytest.mark.db
async def test_free_device_wins_and_no_session_is_killed(
    services: GridServices,
    busy_device: tuple[Device, uuid.UUID],
    db_session: AsyncSession,
    stub_create: None,
    no_appium_delete: list[tuple[str, str]],
) -> None:
    """Preemption is a miss-path step: with spare capacity it never runs."""
    settings_service._cache[PREEMPT_KEY] = True
    _, session_pk = busy_device
    _, free, _ = await seed_host_and_running_node(db_session, identity=f"pre-free-{uuid.uuid4().hex[:8]}")
    await db_session.commit()

    response = await router_internal.create_session(CreateSessionRequest(body=_body(platformName="Android")), services)

    assert getattr(response, "status", None) == "created"
    assert getattr(response, "device_id", None) == free.id
    assert no_appium_delete == []
    survivor = (await db_session.execute(select(Session).where(Session.id == session_pk))).scalar_one()
    assert survivor.status == SessionStatus.running


@pytest.mark.db
async def test_unreachable_appium_still_frees_the_device(
    services: GridServices, busy_device: tuple[Device, uuid.UUID], db_session: AsyncSession, stub_create: None
) -> None:
    """A real DELETE against a dead target — not a patched side_effect, which would
    leave the transaction clean and exercise a different path than production.

    Note there is no ``no_appium_delete`` fixture here on purpose: this test wants
    the real ``terminate_session`` to run and fail.
    """
    settings_service._cache[PREEMPT_KEY] = True
    device, session_pk = busy_device
    # resolve_router_target prefers the live node target over the stored
    # router_target, so point the node itself at a closed local port: loopback
    # gives an instant connection-refused instead of a 10s timeout, and
    # terminate_session takes its httpx.HTTPError branch for real.
    host = (await db_session.execute(select(Host).where(Host.id == device.host_id))).scalar_one()
    host.ip = "127.0.0.1"
    node = (await db_session.execute(select(AppiumNode).where(AppiumNode.device_id == device.id))).scalar_one()
    node.port = 1
    await db_session.commit()

    response = await router_internal.create_session(CreateSessionRequest(body=_body(platformName="Android")), services)

    assert getattr(response, "status", None) == "created"
    evicted = (await db_session.execute(select(Session).where(Session.id == session_pk))).scalar_one()
    assert evicted.status == SessionStatus.error
    assert evicted.error_type == PREEMPTED_ERROR_TYPE
