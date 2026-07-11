"""Push-time observation processing: containment, ordering, and no-raise guarantee."""

from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock

from app.hosts.service_status_push import HostStatusPushService, ObservationFold

if TYPE_CHECKING:
    import uuid
    from collections.abc import Awaitable, Callable

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.core.type_defs import SessionFactory
    from app.hosts.models import Host


def _service(
    session_factory: SessionFactory,
    *,
    observation_folds: tuple[ObservationFold, ...] = (),
    converge_host: Callable[..., Awaitable[None]] | None = None,
    ingest_restart_events: Callable[[AsyncSession, Host, dict[str, Any]], Awaitable[None]] | None = None,
) -> HostStatusPushService:
    return HostStatusPushService(
        publisher=AsyncMock(),
        session_factory=session_factory,
        observation_folds=observation_folds,
        converge_host=converge_host,
        ingest_restart_events=ingest_restart_events,
    )


async def test_process_observations_dispatches_sections_to_matching_folds(
    db_session_maker: async_sessionmaker[AsyncSession], db_host: Host
) -> None:
    seen: list[tuple[str, str]] = []

    async def fold_a(db: AsyncSession, host_id: uuid.UUID, section: dict[str, Any]) -> None:
        seen.append(("a", section["reported_at"]))

    async def fold_b(db: AsyncSession, host_id: uuid.UUID, section: dict[str, Any]) -> None:
        seen.append(("b", section["reported_at"]))

    service = _service(
        db_session_maker,
        observation_folds=(
            ObservationFold("node_health", fold_a),
            ObservationFold("device_health", fold_b),
        ),
    )
    await service.process_observations(
        host_id=db_host.id,
        host_ip=db_host.ip,
        agent_port=db_host.agent_port,
        payload={"node_health": {"reported_at": "t1"}, "device_health": None},
    )

    assert seen == [("a", "t1")]


async def test_process_observations_isolates_a_raising_fold(
    db_session_maker: async_sessionmaker[AsyncSession], db_host: Host
) -> None:
    ran: list[bool] = []

    async def bad(db: AsyncSession, host_id: uuid.UUID, section: dict[str, Any]) -> None:
        raise RuntimeError("boom")

    async def good(db: AsyncSession, host_id: uuid.UUID, section: dict[str, Any]) -> None:
        ran.append(True)

    service = _service(
        db_session_maker,
        observation_folds=(
            ObservationFold("node_health", bad),
            ObservationFold("device_health", good),
        ),
    )
    await service.process_observations(
        host_id=db_host.id,
        host_ip=db_host.ip,
        agent_port=db_host.agent_port,
        payload={"node_health": {"reported_at": "t"}, "device_health": {"reported_at": "t"}},
    )

    assert ran == [True]


async def test_process_observations_runs_restart_then_convergence_then_folds(
    db_session: AsyncSession, db_session_maker: async_sessionmaker[AsyncSession], db_host: Host
) -> None:
    order: list[str] = []
    await db_session.commit()

    async def ingest(db: AsyncSession, host: Host, payload: dict[str, Any]) -> None:
        order.append("restart")

    async def converge(**kwargs: object) -> None:
        order.append("converge")

    async def fold(db: AsyncSession, host_id: uuid.UUID, section: dict[str, Any]) -> None:
        order.append("fold")

    service = _service(
        db_session_maker,
        ingest_restart_events=ingest,
        converge_host=converge,
        observation_folds=(ObservationFold("node_health", fold),),
    )
    await service.process_observations(
        host_id=db_host.id,
        host_ip=db_host.ip,
        agent_port=db_host.agent_port,
        payload={"appium_processes": {"running_nodes": []}, "node_health": {"reported_at": "t"}},
    )

    assert order == ["restart", "converge", "fold"]


async def test_process_observations_contains_convergence_failure(
    db_session_maker: async_sessionmaker[AsyncSession], db_host: Host
) -> None:
    ran: list[bool] = []

    async def converge(**kwargs: object) -> None:
        raise RuntimeError("boom")

    async def fold(db: AsyncSession, host_id: uuid.UUID, section: dict[str, Any]) -> None:
        ran.append(True)

    service = _service(
        db_session_maker,
        converge_host=converge,
        observation_folds=(ObservationFold("node_health", fold),),
    )
    await service.process_observations(
        host_id=db_host.id,
        host_ip=db_host.ip,
        agent_port=db_host.agent_port,
        payload={"appium_processes": {}, "node_health": {"reported_at": "t"}},
    )

    assert ran == [True]


async def test_process_observations_without_wiring_is_a_noop(db_host: Host) -> None:
    service = HostStatusPushService(publisher=AsyncMock())
    await service.process_observations(
        host_id=db_host.id,
        host_ip=db_host.ip,
        agent_port=db_host.agent_port,
        payload={"node_health": {"reported_at": "t"}},
    )
