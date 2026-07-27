"""Phase 9 task 3: host remote effects run with no transaction and no row lock.

Every route in ``app/hosts/router.py`` that dials an agent — pack doctor, tool
status, the three discovery routes, and the ``_auto_discover`` background task —
must copy an immutable :class:`~app.hosts.service.HostTarget` inside a short
transaction, let that transaction end, and only then reach the network. These
tests install a recording session factory into the host and pack containers so
the agent stub can look at *every* session the command opened and assert none of
them is still in a transaction.

The runtime check is the point: a lexical "no ``begin()`` around a remote call"
scan cannot see a session handed down through a service, and a caller that keeps
the request session open across the dial reads identically to one that does not.
"""

from __future__ import annotations

import asyncio
import dataclasses
import uuid
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any
from unittest.mock import ANY, AsyncMock, patch

import pytest
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.exc import DBAPIError, IntegrityError, NoResultFound

from app.core.errors import AgentUnreachableError
from app.devices.models import Device
from app.devices.services.identity_conflicts import DeviceIdentityConflictService
from app.devices.services.presenter import DevicePresenterService
from app.hosts import router as hosts_router
from app.hosts import service as host_service
from app.hosts.dependencies import get_host_services
from app.hosts.models import Host, HostStatus, OSType
from app.hosts.schemas import HostRegister
from app.hosts.service import HostCrudService, HostTarget
from app.main import app
from app.packs.dependencies import get_pack_services
from app.packs.models import HostPackDoctorResult
from app.packs.models.pack import DriverPack
from app.packs.services.discovery import PackDiscoveryService, StaleHostGenerationError
from tests.concurrency.group_lock_helpers import capture_statements, pin_statement_listener
from tests.fakes import FakeSettingsReader
from tests.helpers import create_device_record, dispatch_committed_events, recent_events
from tests.helpers import test_event_bus as event_bus

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Sequence

    from httpx2 import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

CAPS_V7 = {"orchestration_contract_version": 7}

type ExecuteHook = Callable[[AsyncSession, str], Awaitable[None]]


class RecordingSessionFactory:
    """An ``async_sessionmaker`` stand-in that keeps every session it hands out.

    Supports both shapes a Phase 9 command uses — ``factory()`` for a short read
    and ``factory.begin()`` for the single write boundary — and optionally runs
    *hook* after each statement so a test can commit a racing peer at an exact
    point in the command's statement sequence.
    """

    def __init__(self, inner: async_sessionmaker[AsyncSession]) -> None:
        self._inner = inner
        self._detach: list[Callable[[], None]] = []
        self.sessions: list[AsyncSession] = []
        self.statements: list[list[str]] = []
        self.hook: ExecuteHook | None = None

    def _track(self, session: AsyncSession) -> AsyncSession:
        self.sessions.append(session)
        # Real SQL, through the same pinned listener ``capture_statements`` uses:
        # an ORM flush issues its UPDATE on the connection, never through
        # ``session.execute``, so a wrapper alone cannot see the writes.
        sink: list[str] = []
        self.statements.append(sink)
        self._detach.append(pin_statement_listener(session, sink))
        original = session.execute

        async def spy(statement: Any, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
            result = await original(statement, *args, **kwargs)
            if self.hook is not None:
                await self.hook(session, str(statement).lower())
            return result

        session.execute = spy  # type: ignore[method-assign]
        return session

    def __call__(self) -> AsyncSession:
        return self._track(self._inner())

    @asynccontextmanager
    async def begin(self) -> AsyncIterator[AsyncSession]:
        async with self._inner() as session:
            self._track(session)
            async with session.begin():
                yield session

    def close(self) -> None:
        for detach in self._detach:
            detach()
        self._detach.clear()

    def open_transactions(self) -> list[int]:
        """Indexes of recorded sessions still inside a transaction, right now."""
        return [index for index, session in enumerate(self.sessions) if session.in_transaction()]

    def statements_for(self, index: int) -> list[str]:
        return [" ".join(statement.lower().split()) for statement in self.statements[index]]


@pytest.fixture
def recorder(client: AsyncClient) -> Iterator[RecordingSessionFactory]:
    """Swap the host and pack containers onto one shared recording factory.

    ``client`` has already installed the container overrides, so this reuses the
    factory those overrides built instead of minting a second sessionmaker. The
    replacement stays a *factory*, not a fixed container: the pack container
    binds ``agent_operations.get_pack_devices`` when it is built, so a container
    built once at fixture time would capture the real dialler and ignore a
    per-test patch.
    """
    _ = client
    build_hosts = app.dependency_overrides[get_host_services]
    build_packs = app.dependency_overrides[get_pack_services]
    recording = RecordingSessionFactory(build_hosts().session_factory)
    app.dependency_overrides[get_host_services] = lambda: dataclasses.replace(
        build_hosts(),
        session_factory=recording,  # type: ignore[arg-type]
    )
    app.dependency_overrides[get_pack_services] = lambda: dataclasses.replace(
        build_packs(),
        session_factory=recording,  # type: ignore[arg-type]
    )
    yield recording
    recording.close()


DOCTOR_PACK_ID = "appium-uiautomator2"


async def _seed_online_host(db_session: AsyncSession, *, hostname: str, ip: str, pack: bool = False) -> Host:
    host = Host(hostname=hostname, ip=ip, os_type=OSType.linux, agent_port=5100, status=HostStatus.online)
    db_session.add(host)
    if pack:
        # host_pack_doctor_results.pack_id is a real FK.
        db_session.add(DriverPack(id=DOCTOR_PACK_ID, display_name="UiAutomator2", maintainer=""))
    await db_session.commit()
    await db_session.refresh(host)
    return host


def _pack_candidate(identity_value: str, *, os_version: str = "17.4") -> dict[str, Any]:
    return {
        "pack_id": "appium-xcuitest",
        "platform_id": "ios",
        "identity_scheme": "apple_udid",
        "identity_scope": "global",
        "identity_value": identity_value,
        "suggested_name": f"Phone {identity_value}",
        "detected_properties": {"connection_target": identity_value, "os_version": os_version},
        "runnable": True,
    }


# ---------------------------------------------------------------------------
# Remote calls happen outside every transaction the command opened
# ---------------------------------------------------------------------------


@pytest.mark.db
async def test_pack_doctor_dials_the_agent_with_no_open_transaction(
    client: AsyncClient, db_session: AsyncSession, recorder: RecordingSessionFactory
) -> None:
    host = await _seed_online_host(db_session, hostname="doctor-boundary", ip="10.9.0.1", pack=True)
    observed: list[list[int]] = []

    async def _doctor(ip: str, port: int, pack_id: str, **kwargs: object) -> list[dict[str, Any]]:
        _ = (ip, port, pack_id, kwargs)
        observed.append(recorder.open_transactions())
        return [{"check_id": "adb", "ok": True, "message": "adb found"}]

    with patch("app.hosts.router.agent_operations.pack_doctor", new=_doctor):
        resp = await client.post(f"/api/hosts/{host.id}/driver-packs/{DOCTOR_PACK_ID}/doctor")

    assert resp.status_code == 200
    assert observed == [[]], (
        f"pack_doctor was dialled while sessions {observed} still held a transaction; "
        "the target read must close before the agent call"
    )
    # The persist step is a *different*, later session: the read that produced the
    # HostTarget is gone by the time the checks come back.
    assert len(recorder.sessions) >= 2
    assert recorder.sessions[0] is not recorder.sessions[-1]


@pytest.mark.db
async def test_pack_doctor_persists_results_in_a_fresh_transaction(
    client: AsyncClient, db_session: AsyncSession, db_session_maker: async_sessionmaker[AsyncSession]
) -> None:
    host = await _seed_online_host(db_session, hostname="doctor-persist", ip="10.9.0.2", pack=True)
    checks = [
        {"check_id": "adb", "ok": True, "message": "adb found"},
        {"check_id": "java", "ok": False, "message": "java not found"},
    ]
    with patch("app.hosts.router.agent_operations.pack_doctor", new=AsyncMock(return_value=checks)) as dial:
        resp = await client.post(f"/api/hosts/{host.id}/driver-packs/{DOCTOR_PACK_ID}/doctor")

    assert resp.status_code == 200
    dial.assert_awaited_once_with(host.ip, host.agent_port, DOCTOR_PACK_ID, circuit_breaker=ANY, pool=ANY)
    async with db_session_maker() as verify:
        stored = (
            (
                await verify.execute(
                    select(HostPackDoctorResult).where(
                        HostPackDoctorResult.host_id == host.id,
                        HostPackDoctorResult.pack_id == DOCTOR_PACK_ID,
                    )
                )
            )
            .scalars()
            .all()
        )
    assert {row.check_id: row.ok for row in stored} == {"adb": True, "java": False}


@pytest.mark.db
async def test_pack_doctor_remote_failure_persists_no_rows(
    client: AsyncClient, db_session: AsyncSession, db_session_maker: async_sessionmaker[AsyncSession]
) -> None:
    host = await _seed_online_host(db_session, hostname="doctor-unreachable", ip="10.9.0.3", pack=True)
    unreachable = AsyncMock(side_effect=AgentUnreachableError(host.ip, "Connection refused"))
    with patch("app.hosts.router.agent_operations.pack_doctor", new=unreachable):
        resp = await client.post(f"/api/hosts/{host.id}/driver-packs/{DOCTOR_PACK_ID}/doctor")

    assert resp.status_code == 502
    async with db_session_maker() as verify:
        total = await verify.scalar(
            select(func.count()).select_from(HostPackDoctorResult).where(HostPackDoctorResult.host_id == host.id)
        )
    assert total == 0


@pytest.mark.db
async def test_tool_status_dials_the_agent_with_no_open_transaction(
    client: AsyncClient, db_session: AsyncSession, recorder: RecordingSessionFactory
) -> None:
    host = await _seed_online_host(db_session, hostname="tools-boundary", ip="10.9.0.4")
    observed: list[list[int]] = []

    async def _tool_status(ip: str, port: int, **kwargs: object) -> dict[str, Any]:
        _ = (ip, port, kwargs)
        observed.append(recorder.open_transactions())
        return {"host": {}, "packs": {}}

    with patch("app.hosts.router.get_agent_tool_status", new=_tool_status):
        resp = await client.get(f"/api/hosts/{host.id}/tools/status")

    assert resp.status_code == 200
    assert observed == [[]], f"tool status was dialled while sessions {observed} still held a transaction"


@pytest.mark.db
@pytest.mark.parametrize(
    ("method", "suffix", "body"),
    [
        ("post", "/discover", None),
        ("get", "/intake-candidates", None),
        ("post", "/discover/confirm", {"add_identity_values": [], "remove_identity_values": []}),
    ],
)
async def test_discovery_routes_dial_the_agent_with_no_open_transaction(
    client: AsyncClient,
    db_session: AsyncSession,
    recorder: RecordingSessionFactory,
    method: str,
    suffix: str,
    body: dict[str, Any] | None,
) -> None:
    host = await _seed_online_host(db_session, hostname=f"discovery{suffix.replace('/', '-')}", ip="10.9.0.5")
    observed: list[list[int]] = []

    async def _pack_devices(ip: str, port: int, **kwargs: object) -> dict[str, Any]:
        _ = (ip, port, kwargs)
        observed.append(recorder.open_transactions())
        return {"candidates": [_pack_candidate("BOUNDARY-1")]}

    with patch("app.agent_comm.operations.get_pack_devices", new=_pack_devices):
        resp = await getattr(client, method)(f"/api/hosts/{host.id}{suffix}", **({"json": body} if body else {}))

    assert resp.status_code == 200
    assert observed, "the discovery route never dialled the agent"
    assert all(open_sessions == [] for open_sessions in observed), (
        f"{method.upper()} {suffix} dialled the agent while sessions {observed} still held a transaction"
    )


@pytest.mark.db
async def test_auto_discover_dials_the_agent_with_no_open_transaction(
    client: AsyncClient, db_session: AsyncSession, recorder: RecordingSessionFactory
) -> None:
    """The one discovery caller that runs outside a request is observable too.

    ``_auto_discover`` used to open the module-global ``async_session``, which no
    recording factory can see — the assertion above would have passed over it
    without ever running.
    """
    host = await _seed_online_host(db_session, hostname="auto-discover-boundary", ip="10.9.0.6")
    observed: list[list[int]] = []

    async def _pack_devices(ip: str, port: int, **kwargs: object) -> dict[str, Any]:
        _ = (ip, port, kwargs)
        observed.append(recorder.open_transactions())
        return {"candidates": [_pack_candidate("AUTO-1")]}

    with patch("app.agent_comm.operations.get_pack_devices", new=_pack_devices):
        host_services = app.dependency_overrides[get_host_services]()
        pack_services = app.dependency_overrides[get_pack_services]()
        assert host_services.session_factory is recorder
        await hosts_router._auto_discover(
            host.id,
            event_bus,
            pack_services.discovery,
            host_services.crud,
            host_services.session_factory,
        )

    assert observed == [[]], f"_auto_discover dialled the agent while sessions {observed} still held a transaction"


# ---------------------------------------------------------------------------
# Registration conflict: a failed context is exited, never reused
# ---------------------------------------------------------------------------


@pytest.mark.db
async def test_hostname_conflict_constant_matches_the_live_database(
    db_session: AsyncSession, db_session_maker: async_sessionmaker[AsyncSession]
) -> None:
    """Read the constraint name off a real violation instead of trusting the naming rule."""
    hostname = f"observed-{uuid.uuid4().hex[:8]}"
    async with db_session_maker() as seed:
        seed.add(Host(hostname=hostname, ip="10.9.1.1", os_type=OSType.linux, agent_port=5100))
        await seed.commit()

    observed: str | None = None
    async with db_session_maker() as duplicate:
        duplicate.add(Host(hostname=hostname, ip="10.9.1.2", os_type=OSType.linux, agent_port=5100))
        try:
            await duplicate.flush()
        except IntegrityError as exc:
            observed = host_service.integrity_constraint_name(exc)
        await duplicate.rollback()

    assert observed is not None, "a duplicate hostname insert did not raise IntegrityError"
    assert observed == host_service.HOSTNAME_UNIQUE_INDEX, (
        f"the live database reports {observed!r} for the hostname unique index; "
        f"app.hosts.service pins {host_service.HOSTNAME_UNIQUE_INDEX!r}"
    )


@pytest.mark.db
async def test_register_conflict_fallback_uses_a_distinct_session_and_locks_host(
    client: AsyncClient,
    recorder: RecordingSessionFactory,
    db_session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """The losing registrant exits its failed context and re-locks from a new session.

    The peer runs the real ``register_host`` command, so the winner is the only
    side that stages ``host.registered``; the loser degrades to a re-register and
    must add no second event.
    """
    hostname = f"conflict-{uuid.uuid4().hex[:8]}"
    boot_id = uuid.uuid4()
    peer_done = asyncio.Event()

    async def _commit_peer_between_select_and_insert(session: AsyncSession, statement: str) -> None:
        if peer_done.is_set() or "from hosts" not in statement or "select" not in statement:
            return
        peer_done.set()
        crud = HostCrudService(publisher=event_bus, settings=FakeSettingsReader({}))
        async with db_session_maker() as peer, peer.begin():
            await crud.register_host(
                peer,
                HostRegister(hostname=hostname, ip="10.9.1.3", os_type=OSType.linux, capabilities=CAPS_V7),
            )

    recorder.hook = _commit_peer_between_select_and_insert
    resp = await client.post(
        "/api/hosts/register",
        json={
            "hostname": hostname,
            "ip": "10.9.1.4",
            "os_type": "linux",
            "agent_port": 5100,
            "capabilities": CAPS_V7,
            "boot_id": str(boot_id),
        },
    )
    recorder.hook = None

    assert resp.status_code == 200, resp.text
    assert peer_done.is_set(), "the racing peer never committed; the conflict was not exercised"
    assert len(recorder.sessions) == 2, (
        f"expected one failed attempt plus one fallback session, saw {len(recorder.sessions)}"
    )
    first, second = recorder.sessions
    assert first is not second, "the fallback reused the session whose transaction had already failed"
    assert not first.in_transaction(), "the failed begin() context was still open when the fallback started"
    fallback_statements = recorder.statements_for(1)
    lock_positions = [i for i, s in enumerate(fallback_statements) if "from hosts" in s and "for update" in s]
    assert lock_positions, f"the fallback never locked the Host row: {fallback_statements}"
    boot_writes = [i for i, s in enumerate(fallback_statements) if s.startswith("update hosts")]
    assert boot_writes, f"the fallback never wrote the boot fence: {fallback_statements}"
    assert min(lock_positions) < min(boot_writes), "the boot fence was written before the Host row was locked"

    await dispatch_committed_events()
    registered = [
        e for e in recent_events(event_bus, event_types=["host.registered"]) if e["data"]["hostname"] == hostname
    ]
    assert len(registered) == 1, f"expected exactly one host.registered for the winner, saw {registered}"

    async with db_session_maker() as verify:
        stored = (await verify.execute(select(Host).where(Host.hostname == hostname))).scalar_one()
    assert stored.current_boot_id == boot_id
    assert stored.ip == "10.9.1.4"


@pytest.mark.db
async def test_register_conflict_fallback_holds_the_boot_fence_lock(
    client: AsyncClient,
    recorder: RecordingSessionFactory,
    db_session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """A competing status-style Host lock cannot land inside the fallback's window.

    Gated on a real Postgres ``lock_timeout`` rejection rather than a sleep: the
    peer either blocks until registration commits (and is refused) or it does
    not, in which case the fence write is unserialised.
    """
    hostname = f"fence-{uuid.uuid4().hex[:8]}"
    boot_id = uuid.uuid4()
    peer_committed = asyncio.Event()
    fallback_locked = asyncio.Event()
    release = asyncio.Event()
    competing_refused: list[bool] = []

    async def _hook(session: AsyncSession, statement: str) -> None:
        if "from hosts" not in statement or "select" not in statement:
            return
        if not peer_committed.is_set():
            peer_committed.set()
            async with db_session_maker() as peer:
                peer.add(
                    Host(hostname=hostname, ip="10.9.1.5", os_type=OSType.linux, agent_port=5100, capabilities=CAPS_V7)
                )
                await peer.commit()
            return
        # Gate on the fallback's Host read, not on the text "for update": keying
        # the pause on the lock itself would make a lock-free fallback stall this
        # test forever instead of failing it.
        if not fallback_locked.is_set():
            fallback_locked.set()
            await release.wait()

    async def _compete() -> None:
        await fallback_locked.wait()
        async with db_session_maker() as side:
            try:
                await side.execute(text("SET LOCAL lock_timeout = '400ms'"))
                await side.execute(select(Host).where(Host.hostname == hostname).with_for_update())
                competing_refused.append(False)
            except DBAPIError:
                competing_refused.append(True)
            finally:
                await side.rollback()
        release.set()

    recorder.hook = _hook
    competing = asyncio.create_task(_compete())
    resp = await client.post(
        "/api/hosts/register",
        json={
            "hostname": hostname,
            "ip": "10.9.1.6",
            "os_type": "linux",
            "agent_port": 5100,
            "capabilities": CAPS_V7,
            "boot_id": str(boot_id),
        },
    )
    release.set()
    await competing
    recorder.hook = None

    assert resp.status_code == 200, resp.text
    assert competing_refused == [True], (
        "a competing Host FOR UPDATE acquired the row while the fallback held it; "
        "the boot fence write is not serialised against a concurrent status push"
    )
    async with db_session_maker() as verify:
        stored = (await verify.execute(select(Host).where(Host.hostname == hostname))).scalar_one()
    assert stored.current_boot_id == boot_id


# ---------------------------------------------------------------------------
# Confirmation: stale boot fence, and the Host -> sorted Device lock order
# ---------------------------------------------------------------------------


def _discovery_service() -> PackDiscoveryService:
    return PackDiscoveryService(
        agent_get_pack_devices=AsyncMock(return_value={"candidates": []}),
        circuit_breaker=AsyncMock(),
        serializer=DevicePresenterService(),
        identity_guard=DeviceIdentityConflictService(),
    )


async def _seed_host_and_device(
    db_session: AsyncSession, *, hostname: str, boot_id: uuid.UUID, identity_value: str
) -> tuple[Host, Device]:
    host = Host(
        hostname=hostname,
        ip="10.9.2.1",
        os_type=OSType.linux,
        agent_port=5100,
        status=HostStatus.online,
        current_boot_id=boot_id,
    )
    db_session.add(host)
    await db_session.commit()
    await db_session.refresh(host)
    device = await create_device_record(
        db_session,
        host_id=host.id,
        identity_value=identity_value,
        connection_target=identity_value,
        name=f"Device {identity_value}",
        pack_id="appium-xcuitest",
        platform_id="ios",
        identity_scheme="apple_udid",
        identity_scope="global",
        os_version="17.0",
    )
    return host, device


def _target(host: Host, boot_id: uuid.UUID | None) -> HostTarget:
    return HostTarget(
        host_id=host.id,
        hostname=host.hostname,
        ip=host.ip,
        agent_port=host.agent_port,
        current_boot_id=boot_id,
    )


@pytest.mark.db
async def test_confirm_declines_a_rotated_boot_without_touching_devices(
    db_session: AsyncSession, db_session_maker: async_sessionmaker[AsyncSession]
) -> None:
    """A target prepared under boot A cannot apply once boot B has registered.

    The rotation is a real committed write from a peer session, not a patched
    attribute, so the fence recheck runs against exactly what a re-registering
    agent leaves behind.
    """
    boot_a, boot_b = uuid.uuid4(), uuid.uuid4()
    host, device = await _seed_host_and_device(
        db_session, hostname=f"stale-{uuid.uuid4().hex[:6]}", boot_id=boot_a, identity_value="STALE-1"
    )
    target = _target(host, boot_a)
    candidates: Sequence[dict[str, Any]] = ()

    async with db_session_maker() as peer:
        await peer.execute(update(Host).where(Host.id == host.id).values(current_boot_id=boot_b))
        await peer.commit()

    svc = _discovery_service()
    async with db_session_maker() as db, db.begin():
        # Not NoResultFound: the Host row is still there. Conflating the two would
        # tell the operator the host was deleted.
        with pytest.raises(StaleHostGenerationError) as caught:
            await svc.confirm_discovery(db, target, candidates, [], ["STALE-1"])
    assert host.hostname in str(caught.value)
    assert "re-run intake" in str(caught.value)

    async with db_session_maker() as verify:
        survivor = await verify.get(Device, device.id)
    assert survivor is not None, "a stale-boot confirmation deleted a Device row"
    assert survivor.os_version == "17.0"


@pytest.mark.db
async def test_confirm_declines_a_vanished_host_as_not_found(
    db_session: AsyncSession, db_session_maker: async_sessionmaker[AsyncSession]
) -> None:
    """The sibling outcome, kept distinguishable: a deleted host is still 404 fodder."""
    boot_id = uuid.uuid4()
    host, device = await _seed_host_and_device(
        db_session, hostname=f"gone-{uuid.uuid4().hex[:6]}", boot_id=boot_id, identity_value="GONE-1"
    )
    target = _target(host, boot_id)

    async with db_session_maker() as peer:
        await peer.execute(delete(Device).where(Device.id == device.id))
        await peer.execute(delete(Host).where(Host.id == host.id))
        await peer.commit()

    svc = _discovery_service()
    async with db_session_maker() as db, db.begin():
        with pytest.raises(NoResultFound):
            await svc.confirm_discovery(db, target, (), [], [])


@pytest.mark.db
@pytest.mark.parametrize("condition", ["rotated_boot", "deleted_host"])
async def test_confirm_route_separates_a_rotated_boot_from_a_deleted_host(
    client: AsyncClient,
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    recorder: RecordingSessionFactory,
    condition: str,
) -> None:
    """A client must be able to tell "re-run intake" from "the host is gone".

    Both conditions are provoked *inside* the window the ``HostTarget`` hand-off
    opens — after the route's prepare read, before its write transaction — by
    committing the peer change from the recording factory's statement hook. That
    is the only window where the write phase's own recheck can be the thing that
    answers, rather than the prepare read short-circuiting to 404.
    """
    boot_a, boot_b = uuid.uuid4(), uuid.uuid4()
    host, device = await _seed_host_and_device(
        db_session, hostname=f"route-{uuid.uuid4().hex[:6]}", boot_id=boot_a, identity_value="ROUTE-1"
    )
    fired = asyncio.Event()

    async def _change_host_after_prepare(session: AsyncSession, statement: str) -> None:
        if fired.is_set() or "from hosts" not in statement or "select" not in statement:
            return
        fired.set()
        async with db_session_maker() as peer:
            if condition == "rotated_boot":
                await peer.execute(update(Host).where(Host.id == host.id).values(current_boot_id=boot_b))
            else:
                await peer.execute(delete(Device).where(Device.id == device.id))
                await peer.execute(delete(Host).where(Host.id == host.id))
            await peer.commit()

    recorder.hook = _change_host_after_prepare
    with patch("app.agent_comm.operations.get_pack_devices", new=AsyncMock(return_value={"candidates": []})):
        resp = await client.post(
            f"/api/hosts/{host.id}/discover/confirm",
            json={"add_identity_values": [], "remove_identity_values": []},
        )
    recorder.hook = None

    assert fired.is_set(), "the peer change never landed; the hand-off window was not exercised"
    detail = resp.json()["error"]["message"]
    if condition == "rotated_boot":
        assert resp.status_code == 409, resp.text
        assert host.hostname in detail
        assert "re-run intake" in detail
    else:
        assert resp.status_code == 404, resp.text
        assert detail == "Host not found"


@pytest.mark.db
async def test_confirm_locks_host_then_devices_in_sorted_order(
    db_session: AsyncSession, db_session_maker: async_sessionmaker[AsyncSession]
) -> None:
    boot_id = uuid.uuid4()
    # ORDER-1 is the row the confirmation updates, ORDER-2 the one it removes;
    # both must be inside a single ascending Device lock.
    host, _updated = await _seed_host_and_device(
        db_session, hostname=f"order-{uuid.uuid4().hex[:6]}", boot_id=boot_id, identity_value="ORDER-1"
    )
    await create_device_record(
        db_session,
        host_id=host.id,
        identity_value="ORDER-2",
        connection_target="ORDER-2",
        name="Device ORDER-2",
        pack_id="appium-xcuitest",
        platform_id="ios",
        identity_scheme="apple_udid",
        identity_scope="global",
        os_version="17.0",
    )
    target = _target(host, boot_id)
    candidates = [_pack_candidate("ORDER-1", os_version="17.9")]

    svc = _discovery_service()
    async with db_session_maker() as db, capture_statements(db) as statements, db.begin():
        await svc.confirm_discovery(db, target, candidates, [], ["ORDER-2"])

    locks = [_locked_table(stmt) for stmt in statements if "for update" in stmt.lower()]
    assert locks, "confirm_discovery took no row locks at all"
    assert locks[0] == "hosts", f"the first row lock was on {locks[0]!r}, not the Host aggregate root: {locks}"
    assert "hosts" not in locks[1:], f"a Host lock was taken after a Device lock (inversion): {locks}"
    assert "devices" in locks, f"confirm_discovery never locked the Device rows it mutates: {locks}"
    device_locks = [stmt for stmt in statements if "for update" in stmt.lower() and _locked_table(stmt) == "devices"]
    assert all("order by devices.id" in stmt.lower() for stmt in device_locks), (
        f"Device rows were locked without an ascending id order: {device_locks}"
    )


def _locked_table(statement: str) -> str:
    lowered = " ".join(statement.lower().split())
    if " from hosts" in lowered:
        return "hosts"
    if " from devices" in lowered:
        return "devices"
    return lowered.partition(" from ")[2].partition(" ")[0] or "?"
