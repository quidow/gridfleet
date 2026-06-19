from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import cast, func, literal, select, union
from sqlalchemy.dialects.postgresql import JSONB as PG_JSONB
from sqlalchemy.orm import selectinload

from app.devices.models import Device, DeviceReservation
from app.packs.models import DriverPack, DriverPackRelease, PackState
from app.runs.models import TERMINAL_STATES, RunState, TestRun
from app.sessions.live_session_predicate import live_session_predicate
from app.sessions.models import Session

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


VALID_TRANSITIONS: dict[PackState, set[PackState]] = {
    PackState.draft: {PackState.enabled},
    PackState.enabled: {PackState.draining, PackState.disabled},
    PackState.draining: {PackState.enabled, PackState.disabled},
    PackState.disabled: {PackState.enabled},
}


class PackLifecycleService:
    async def count_active_work_for_pack(self, db: AsyncSession, pack_id: str) -> dict[str, int]:
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
        active_runs = (await db.execute(select(func.count()).select_from(combined_runs))).scalar_one()

        live_sessions = (
            await db.execute(
                select(func.count(func.distinct(Session.id)))
                .select_from(Session)
                .outerjoin(Device, Device.id == Session.device_id)
                .where(
                    # running|pending via the shared chokepoint: a grid allocation in
                    # the allocate->confirm window mints a pending row with run_id=None
                    # and no reservation, so it is invisible to the run gate — counting
                    # it here keeps the drain from tearing down the runtime mid-create
                    # (wave-5 #9).
                    live_session_predicate(),
                    Device.pack_id == pack_id,
                )
            )
        ).scalar_one()

        return {"active_runs": active_runs, "live_sessions": live_sessions}

    async def try_complete_drain(self, db: AsyncSession, pack_id: str) -> DriverPack:
        # ``SELECT … FOR UPDATE`` on the pack row pairs with the ``FOR SHARE``
        # taken by ``assert_runnable(..., pack_lock=True)`` in the allocator: it
        # blocks here until any in-flight ``create_run`` transaction that
        # observed ``state=enabled`` either commits its reservation or aborts.
        # Once we acquire the lock, the recount below sees any reservation
        # those transactions just committed.
        locked_stmt = (
            select(DriverPack)
            .where(DriverPack.id == pack_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        pack = (await db.execute(locked_stmt)).scalar_one_or_none()
        if pack is None:
            raise LookupError(f"pack {pack_id!r} not found")
        if pack.state != PackState.draining:
            return pack
        counts = await self.count_active_work_for_pack(db, pack_id)
        if counts["active_runs"] == 0 and counts["live_sessions"] == 0:
            recheck = await self.count_active_work_for_pack(db, pack_id)
            if recheck["active_runs"] == 0 and recheck["live_sessions"] == 0:
                pack.state = PackState.disabled
        return pack

    async def transition_pack_state(
        self,
        db: AsyncSession,
        pack_id: str,
        target: PackState,
        *,
        override: bool = False,
    ) -> DriverPack:
        pack = (
            await db.execute(
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
            await db.commit()
            await self.try_complete_drain(db, pack_id)
            await db.commit()
            result = (
                await db.execute(
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
        await db.commit()
        result = (
            await db.execute(
                select(DriverPack)
                .where(DriverPack.id == pack_id)
                .options(
                    selectinload(DriverPack.releases).selectinload(DriverPackRelease.platforms),
                    selectinload(DriverPack.releases).selectinload(DriverPackRelease.features),
                )
            )
        ).scalar_one()
        return result


# ──────────────────────────────────────────────────────────────────────────────
