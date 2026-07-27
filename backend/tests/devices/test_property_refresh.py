from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.core.timeutil import now_utc
from app.devices.models import ConnectionType, Device
from app.devices.services.property_refresh import PropertyRefreshService
from app.hosts.models import Host, HostStatus, OSType
from app.packs.services.discovery import PackDiscoveryService
from tests.helpers import create_device_record

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


class _TrackingSessionFactory:
    """Wraps a real session factory, recording every session it yields.

    Lets a test assert on the shape of sessions opened (one inventory session,
    then one fresh transaction per settled device), not just the persisted data.
    """

    def __init__(self, engine: AsyncEngine) -> None:
        self._factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        self.sessions: list[AsyncSession] = []

    def __call__(self) -> AsyncSession:
        session = self._factory()
        self.sessions.append(session)
        return session

    @asynccontextmanager
    async def begin(self) -> AsyncIterator[AsyncSession]:
        async with self._factory() as session:
            self.sessions.append(session)
            async with session.begin():
                yield session


def _properties_section(*connection_targets: str) -> dict[str, object]:
    stamp = now_utc().isoformat()
    return {
        "reported_at": stamp,
        "devices": {
            target: {"identity_value": target, "detected_properties": {}, "observed_at": stamp}
            for target in connection_targets
        },
    }


async def test_fold_applies_only_to_section_devices_on_the_host(
    db_session: AsyncSession,
    setup_database: AsyncEngine,
) -> None:
    host = Host(hostname="fold-host", ip="10.0.0.10", os_type=OSType.linux, agent_port=5100, status=HostStatus.online)
    other_host = Host(
        hostname="other-host", ip="10.0.0.11", os_type=OSType.linux, agent_port=5100, status=HostStatus.online
    )
    db_session.add_all([host, other_host])
    await db_session.flush()

    in_section = await create_device_record(
        db_session, host_id=host.id, identity_value="refresh-001", connection_target="refresh-001", name="One"
    )
    absent = await create_device_record(
        db_session, host_id=host.id, identity_value="refresh-002", connection_target="refresh-002", name="Two"
    )
    other = await create_device_record(
        db_session, host_id=other_host.id, identity_value="refresh-003", connection_target="refresh-003", name="Three"
    )

    apply = AsyncMock()

    class _DiscoveryDouble:
        apply_pack_device_properties = apply

    svc = PropertyRefreshService(discovery=_DiscoveryDouble())
    session_factory = async_sessionmaker(setup_database, class_=AsyncSession, expire_on_commit=False)
    await svc.fold_host_device_properties(session_factory, host.id, _properties_section("refresh-001"))

    applied = [await_call.args[1].identity_value for await_call in apply.await_args_list]
    assert in_section.identity_value in applied
    assert absent.identity_value not in applied  # not in section
    assert other.identity_value not in applied  # different host


async def test_fold_continues_after_device_failure(
    db_session: AsyncSession,
    setup_database: AsyncEngine,
) -> None:
    """A real aborted PostgreSQL transaction on the middle device rolls back only
    that device; first/third persist. Each device settles in its own fresh
    transaction, distinct from the one inventory read and from each other."""
    host = Host(hostname="fold-host", ip="10.0.0.12", os_type=OSType.linux, agent_port=5100, status=HostStatus.online)
    db_session.add(host)
    await db_session.flush()

    first = await create_device_record(
        db_session, host_id=host.id, identity_value="refresh-a", connection_target="refresh-a", name="Refresh A"
    )
    second = await create_device_record(
        db_session, host_id=host.id, identity_value="refresh-b", connection_target="refresh-b", name="Refresh B"
    )
    third = await create_device_record(
        db_session, host_id=host.id, identity_value="refresh-c", connection_target="refresh-c", name="Refresh C"
    )

    discovery = _discovery_service()
    svc = PropertyRefreshService(discovery=discovery)

    stamp = now_utc().isoformat()
    section = {
        "reported_at": stamp,
        "devices": {
            "refresh-a": {
                "identity_value": "refresh-a",
                "detected_properties": {"os_version": "14.9"},
                "observed_at": stamp,
            },
            "refresh-b": {
                "identity_value": "refresh-b",
                # A real PostgreSQL rejection: a non-string value bound against the
                # VARCHAR os_version column fails when the caller's transaction
                # flushes. A mocked side_effect would leave the session clean and
                # miss the aborted-transaction path this refactor exists to isolate.
                "detected_properties": {"os_version": ["invalid"]},
                "observed_at": stamp,
            },
            "refresh-c": {
                "identity_value": "refresh-c",
                "detected_properties": {"os_version": "16.0"},
                "observed_at": stamp,
            },
        },
    }

    tracking_factory = _TrackingSessionFactory(setup_database)
    await svc.fold_host_device_properties(tracking_factory, host.id, section)

    verify_factory = async_sessionmaker(setup_database, class_=AsyncSession, expire_on_commit=False)
    async with verify_factory() as verify:
        refreshed_first = await verify.get(Device, first.id)
        refreshed_second = await verify.get(Device, second.id)
        refreshed_third = await verify.get(Device, third.id)

    assert refreshed_first is not None
    assert refreshed_first.os_version == "14.9"
    assert refreshed_third is not None
    assert refreshed_third.os_version == "16.0"
    assert refreshed_second is not None
    assert refreshed_second.os_version == "14"  # unchanged: its commit aborted

    # Structural claim: one inventory session, then one distinct fresh session
    # per settled device (including the one whose commit aborted).
    assert len(tracking_factory.sessions) == 4
    inventory_session, *device_sessions = tracking_factory.sessions
    assert len({id(session) for session in device_sessions}) == 3
    assert inventory_session not in device_sessions


def _discovery_service() -> PackDiscoveryService:
    return PackDiscoveryService(
        agent_get_pack_devices=AsyncMock(return_value={"candidates": []}),
        circuit_breaker=MagicMock(),
        serializer=MagicMock(),
        identity_guard=MagicMock(),
    )


# ---------------------------------------------------------------------------
# The boundary hand-off apply_pack_device_properties gave up
# ---------------------------------------------------------------------------
#
# ``apply_pack_device_properties`` is flush-only now: it mutates the row and
# returns. ``fold_host_device_properties`` is the only production caller and it
# used to open a plain ``session_factory()`` with no transaction context, so
# deleting the callee commit without giving the caller a ``begin()`` would drop
# every refreshed property while leaving both sides' unit tests green. These two
# tests pin both directions of that hand-off.


class _FailAfterApply:
    """Applies the real property fold, then aborts the transaction for real.

    Not a patched ``side_effect``: the failure is an actual statement Postgres
    rejects, so the transaction is genuinely aborted at the point the fold has
    already mutated the row — the state a partially-applied refresh reaches in
    production.
    """

    def __init__(self, inner: PackDiscoveryService) -> None:
        self._inner = inner
        self.mutated: list[str | None] = []

    async def apply_pack_device_properties(
        self, session: AsyncSession, device: Device, data: dict[str, object]
    ) -> None:
        await self._inner.apply_pack_device_properties(session, device, data)
        self.mutated.append(device.os_version)
        await session.execute(text("SELECT no_such_column_for_property_refresh"))


def _os_version_section(target: str, os_version: str) -> dict[str, Any]:
    stamp = now_utc().isoformat()
    return {
        "reported_at": stamp,
        "devices": {
            target: {
                "identity_value": target,
                "detected_properties": {"os_version": os_version},
                "observed_at": stamp,
            }
        },
    }


async def test_fold_persists_a_clean_apply(db_session: AsyncSession, setup_database: AsyncEngine) -> None:
    """The caller owns the boundary the provider gave up; without it nothing lands."""
    host = Host(hostname="fold-commit", ip="10.0.0.13", os_type=OSType.linux, agent_port=5100, status=HostStatus.online)
    db_session.add(host)
    await db_session.flush()
    device = await create_device_record(
        db_session, host_id=host.id, identity_value="fold-ok", connection_target="fold-ok", name="Fold OK"
    )

    svc = PropertyRefreshService(discovery=_discovery_service())
    session_factory = async_sessionmaker(setup_database, class_=AsyncSession, expire_on_commit=False)
    await svc.fold_host_device_properties(session_factory, host.id, _os_version_section("fold-ok", "15.7"))

    async with session_factory() as verify:
        refreshed = await verify.get(Device, device.id)
    assert refreshed is not None
    assert refreshed.os_version == "15.7", "a clean fold did not persist — the caller's transaction never committed"


async def test_fold_persists_nothing_when_the_transaction_aborts_after_the_mutation(
    db_session: AsyncSession, setup_database: AsyncEngine
) -> None:
    host = Host(hostname="fold-abort", ip="10.0.0.14", os_type=OSType.linux, agent_port=5100, status=HostStatus.online)
    db_session.add(host)
    await db_session.flush()
    device = await create_device_record(
        db_session, host_id=host.id, identity_value="fold-abort", connection_target="fold-abort", name="Fold Abort"
    )
    # Whatever the row started at, not a literal the helper's default owns.
    baseline_os_version = device.os_version
    refreshed_os_version = "15.7"
    assert baseline_os_version != refreshed_os_version, "the fold would be a no-op; the test could not fail"

    failing = _FailAfterApply(_discovery_service())
    svc = PropertyRefreshService(discovery=failing)  # type: ignore[arg-type]
    session_factory = async_sessionmaker(setup_database, class_=AsyncSession, expire_on_commit=False)
    await svc.fold_host_device_properties(
        session_factory, host.id, _os_version_section("fold-abort", refreshed_os_version)
    )

    assert failing.mutated == [refreshed_os_version], "the fold never reached the mutation this test is pinning"
    async with session_factory() as verify:
        refreshed = await verify.get(Device, device.id)
    assert refreshed is not None
    assert refreshed.os_version == baseline_os_version, "a partially applied refresh was committed anyway"


def _roku_device(**overrides: object) -> SimpleNamespace:
    defaults: dict[str, object] = {
        "identity_value": "SER123",
        "connection_target": "10.0.0.5",
        "pack_id": "roku",
        "connection_type": ConnectionType.network,
        "os_version": None,
        "os_version_display": None,
        "software_versions": None,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


@pytest.mark.asyncio
async def test_apply_updates_connection_target_for_verified_identity() -> None:
    svc = _discovery_service()
    device = _roku_device()
    session = AsyncMock()
    await svc.apply_pack_device_properties(
        session,
        device,  # type: ignore[arg-type]
        {
            "identity_value": "SER123",
            "detected_properties": {"connection_target": "10.0.0.9", "os_version": "14.5"},
        },
    )
    assert device.connection_target == "10.0.0.9"
    assert device.os_version == "14.5"
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_apply_ignores_connection_target_on_identity_mismatch() -> None:
    svc = _discovery_service()
    device = _roku_device()
    session = AsyncMock()
    await svc.apply_pack_device_properties(
        session,
        device,  # type: ignore[arg-type]
        {
            "identity_value": "OTHER-SERIAL",
            "detected_properties": {"connection_target": "10.0.0.9"},
        },
    )
    assert device.connection_target == "10.0.0.5"
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_apply_leaves_the_connection_target_alone_when_unchanged() -> None:
    svc = _discovery_service()
    device = _roku_device()
    session = AsyncMock()
    await svc.apply_pack_device_properties(
        session,
        device,  # type: ignore[arg-type]
        {
            "identity_value": "SER123",
            "detected_properties": {"connection_target": "10.0.0.5"},
        },
    )
    assert device.connection_target == "10.0.0.5"
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_apply_skips_connection_target_for_non_network_device() -> None:
    """Emulator/USB connection targets are owned by intake/verification, not refresh.

    The android pack's discover reports the live adb serial while normalize
    reports the stable AVD name — letting refresh write both forms would make
    the row oscillate every cycle. Only network devices (the DHCP-move case)
    get the connection_target heal.
    """
    svc = _discovery_service()
    device = _roku_device(
        identity_value="avd:Television_1080p",
        connection_target="emulator-5554",
        pack_id="appium-uiautomator2",
        connection_type=ConnectionType.virtual,
    )
    session = AsyncMock()
    await svc.apply_pack_device_properties(
        session,
        device,  # type: ignore[arg-type]
        {
            "identity_value": "avd:Television_1080p",
            "detected_properties": {"connection_target": "Television_1080p"},
        },
    )
    assert device.connection_target == "emulator-5554"
    session.commit.assert_not_awaited()
