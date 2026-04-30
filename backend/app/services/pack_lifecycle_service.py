from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import cast, func, literal, or_, select, union
from sqlalchemy.dialects.postgresql import JSONB as PG_JSONB
from sqlalchemy.orm import selectinload

from app.models.device import Device
from app.models.device_reservation import DeviceReservation
from app.models.driver_pack import DriverPack, DriverPackRelease, PackState
from app.models.session import Session, SessionStatus
from app.models.test_run import TERMINAL_STATES, RunState, TestRun

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


VALID_TRANSITIONS: dict[PackState, set[PackState]] = {
    PackState.draft: {PackState.enabled},
    PackState.enabled: {PackState.draining, PackState.disabled},
    PackState.draining: {PackState.enabled, PackState.disabled},
    PackState.disabled: {PackState.enabled},
}


async def count_active_work_for_pack(session: AsyncSession, pack_id: str) -> dict[str, int]:
    runs_with_reservations = (
        select(TestRun.id)
        .join(DeviceReservation, DeviceReservation.run_id == TestRun.id)
        .join(Device, Device.id == DeviceReservation.device_id)
        .where(
            TestRun.state.notin_(TERMINAL_STATES),
            Device.pack_id == pack_id,
            DeviceReservation.released_at.is_(None),
        )
    )

    runs_with_requirements = select(TestRun.id).where(
        TestRun.state.in_({RunState.pending, RunState.preparing}),
        cast(TestRun.requirements, PG_JSONB).contains(cast(literal(f'[{{"pack_id": "{pack_id}"}}]'), PG_JSONB)),
    )

    combined_runs = union(runs_with_reservations, runs_with_requirements).subquery()
    active_runs = (await session.execute(select(func.count()).select_from(combined_runs))).scalar_one()

    live_sessions = (
        await session.execute(
            select(func.count(func.distinct(Session.id)))
            .select_from(Session)
            .outerjoin(Device, Device.id == Session.device_id)
            .where(
                Session.status == SessionStatus.running,
                Session.ended_at.is_(None),
                or_(
                    Session.requested_pack_id == pack_id,
                    Device.pack_id == pack_id,
                ),
            )
        )
    ).scalar_one()

    return {"active_runs": active_runs, "live_sessions": live_sessions}


async def try_complete_drain(session: AsyncSession, pack_id: str) -> DriverPack:
    pack = await session.get(DriverPack, pack_id)
    if pack is None:
        raise LookupError(f"pack {pack_id!r} not found")
    if pack.state != PackState.draining:
        return pack
    counts = await count_active_work_for_pack(session, pack_id)
    if counts["active_runs"] == 0 and counts["live_sessions"] == 0:
        pack.state = PackState.disabled
    return pack


async def transition_pack_state(
    session: AsyncSession,
    pack_id: str,
    target: PackState,
    *,
    override: bool = False,
) -> DriverPack:
    pack = (
        await session.execute(
            select(DriverPack)
            .where(DriverPack.id == pack_id)
            .options(
                selectinload(DriverPack.releases).selectinload(DriverPackRelease.platforms),
                selectinload(DriverPack.releases).selectinload(DriverPackRelease.features),
            )
        )
    ).scalar_one_or_none()
    if pack is None:
        raise LookupError(f"pack {pack_id!r} not found")

    current = PackState(pack.state)

    if target == PackState.disabled and current == PackState.enabled:
        pack.state = PackState.draining
        await session.flush()
        pack = await try_complete_drain(session, pack_id)
        await session.commit()
        result = (
            await session.execute(
                select(DriverPack)
                .where(DriverPack.id == pack_id)
                .options(
                    selectinload(DriverPack.releases).selectinload(DriverPackRelease.platforms),
                    selectinload(DriverPack.releases).selectinload(DriverPackRelease.features),
                )
            )
        ).scalar_one()
        return result

    if target not in VALID_TRANSITIONS.get(current, set()):
        raise ValueError(f"Cannot transition pack {pack_id!r} from {current.value!r} to {target.value!r}")

    pack.state = target
    await session.commit()
    result = (
        await session.execute(
            select(DriverPack)
            .where(DriverPack.id == pack_id)
            .options(
                selectinload(DriverPack.releases).selectinload(DriverPackRelease.platforms),
                selectinload(DriverPack.releases).selectinload(DriverPackRelease.features),
            )
        )
    ).scalar_one()
    return result
