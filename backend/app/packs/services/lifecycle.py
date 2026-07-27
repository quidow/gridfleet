from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import String, cast, func, literal, literal_column, select, union, union_all
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import JSONB as PG_JSONB
from sqlalchemy.orm import selectinload

from app.core.observability import get_logger
from app.devices.models import Device, DeviceReservation
from app.devices.services.claims import reservation_active
from app.packs.models import DriverPack, DriverPackRelease, PackState
from app.packs.services.service import PackNotFound, PackTransitionError, build_pack_out
from app.runs.models import TERMINAL_STATES, RunState, TestRun
from app.sessions.live_session_predicate import live_session_predicate
from app.sessions.models import Session

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy import Select
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.packs.schemas import PackOut

logger = get_logger(__name__)


def _pack_with_releases_options() -> tuple[Any, ...]:
    return (selectinload(DriverPack.releases).selectinload(DriverPackRelease.platforms),)


VALID_TRANSITIONS: dict[PackState, set[PackState]] = {
    PackState.enabled: {PackState.draining, PackState.disabled},
    PackState.draining: {PackState.enabled, PackState.disabled},
    PackState.disabled: {PackState.enabled},
}


class PackLifecycleService:
    async def summarize_active_work(self, db: AsyncSession, pack_ids: Sequence[str]) -> dict[str, dict[str, int]]:
        """Active runs and live sessions for every pack in *pack_ids*, in one statement.

        The catalog needs this for every draining pack at once, so the shape is
        set-based rather than per-pack: two grouped aggregates over the same id
        list, unioned so a fleet of fifty draining packs still costs one query.
        ``count_active_work_for_pack`` is the single-id caller of the same SQL —
        there is no second definition of "active work" to drift from this one.
        """
        ids = sorted(set(pack_ids))
        summary: dict[str, dict[str, int]] = {pack_id: {"active_runs": 0, "live_sessions": 0} for pack_id in ids}
        if not ids:
            return summary

        runs_with_reservations = (
            select(Device.pack_id.label("pack_id"), TestRun.id.label("run_id"))
            .select_from(TestRun)
            .join(DeviceReservation, DeviceReservation.run_id == TestRun.id)
            .join(Device, Device.id == DeviceReservation.device_id)
            .where(
                TestRun.state.notin_(TERMINAL_STATES),
                Device.pack_id.in_(ids),
                reservation_active(),
            )
        )

        # The requirements gate is a containment test per pack id, so the id list
        # has to be a relation the join can range over. ``@> [{"pack_id": …}]``
        # is kept verbatim: it matches any requirements shape, where expanding
        # the array in SQL would fail on a row that is not a JSON array.
        pack_id_rows = (
            func.unnest(cast(literal(ids), ARRAY(String))).table_valued("pack_id").render_derived(name="drain_pack")
        )
        runs_with_requirements = (
            select(pack_id_rows.c.pack_id.label("pack_id"), TestRun.id.label("run_id"))
            .select_from(pack_id_rows)
            .join(
                TestRun,
                cast(TestRun.requirements, PG_JSONB).contains(
                    func.jsonb_build_array(func.jsonb_build_object("pack_id", pack_id_rows.c.pack_id))
                ),
            )
            .where(TestRun.state.in_({RunState.pending, RunState.preparing}))
        )

        # UNION (not UNION ALL): a run can reach a pack through both gates and
        # must be counted once, exactly as the per-pack query did.
        run_pairs = union(runs_with_reservations, runs_with_requirements).subquery()
        run_totals: Select[Any] = select(
            literal_column("'active_runs'").label("kind"),
            run_pairs.c.pack_id,
            func.count().label("total"),
        ).group_by(run_pairs.c.pack_id)

        session_totals: Select[Any] = (
            select(
                literal_column("'live_sessions'").label("kind"),
                Device.pack_id.label("pack_id"),
                func.count(func.distinct(Session.id)).label("total"),
            )
            .select_from(Session)
            .join(Device, Device.id == Session.device_id)
            .where(
                # running|pending via the shared chokepoint: a grid allocation in
                # the allocate->confirm window mints a pending row with run_id=None
                # and no reservation, so it is invisible to the run gate — counting
                # it here keeps the drain from tearing down the runtime mid-create
                # (wave-5 #9).
                live_session_predicate(),
                Device.pack_id.in_(ids),
            )
            .group_by(Device.pack_id)
        )

        for kind, pack_id, total in (await db.execute(union_all(run_totals, session_totals))).all():
            summary[pack_id][kind] = int(total)
        return summary

    async def count_active_work_for_pack(self, db: AsyncSession, pack_id: str) -> dict[str, int]:
        return (await self.summarize_active_work(db, [pack_id]))[pack_id]

    async def _drain_settled(self, db: AsyncSession, pack_id: str) -> bool:
        """Whether the pack's last active work is gone, read twice to be sure.

        The row lock closes the window against an allocator that takes the pack
        row; the second reading closes it against anything that commits without
        taking it. Both readings must be zero, and only the second one decides.
        """
        counts = await self.count_active_work_for_pack(db, pack_id)
        if counts["active_runs"] or counts["live_sessions"]:
            return False
        recheck = await self.count_active_work_for_pack(db, pack_id)
        return not (recheck["active_runs"] or recheck["live_sessions"])

    async def try_complete_drain(self, db: AsyncSession, pack_id: str) -> PackState:
        """Disable *pack_id* if its drain has finished; returns the resulting state.

        Transaction-local: the caller owns the boundary. A scalar comes back
        rather than the row, so no ORM object outlives the caller's transaction.
        """
        pack = await self._lock_pack(db, pack_id)
        if pack is None:
            raise LookupError(f"pack {pack_id!r} not found")
        if pack.state == PackState.draining and await self._drain_settled(db, pack_id):
            pack.state = PackState.disabled
            await db.flush()
        return PackState(pack.state)

    async def complete_draining_packs_once(self, db: AsyncSession) -> list[str]:
        """Backstop scan (janitor stage): complete any draining pack whose last
        active work released without hitting the inline drain hook.

        Transaction-local; the janitor stage owns the boundary.
        """
        pack_ids = (
            (
                await db.execute(
                    select(DriverPack.id).where(DriverPack.state == PackState.draining).order_by(DriverPack.id)
                )
            )
            .scalars()
            .all()
        )
        completed = [
            pack_id for pack_id in pack_ids if await self.try_complete_drain(db, pack_id) == PackState.disabled
        ]
        if completed:
            logger.info("Completed draining driver packs: %s", ", ".join(completed))
        return completed

    async def transition_pack_state_txn(
        self,
        db: AsyncSession,
        pack_id: str,
        target: PackState,
    ) -> PackOut:
        """Move a pack to *target* inside the caller's transaction.

        The pack row is locked before anything is validated, so an allocator
        holding ``FOR SHARE`` on it has committed or aborted before the active-work
        count runs. ``enabled -> disabled`` settles draining-versus-disabled here
        rather than publishing ``draining`` and correcting it: a peer that reads
        the pack sees the state this command decided on, never the step it took
        to get there.
        """
        pack = await self._lock_pack(db, pack_id, with_releases=True)
        if pack is None:
            raise PackNotFound(f"pack {pack_id!r} not found")

        current = PackState(pack.state)
        if target == PackState.disabled and current == PackState.enabled:
            settled = await self._drain_settled(db, pack_id)
            pack.state = PackState.disabled if settled else PackState.draining
        elif target not in VALID_TRANSITIONS.get(current, set()):
            raise PackTransitionError(f"Cannot transition pack {pack_id!r} from {current.value!r} to {target.value!r}")
        else:
            pack.state = target

        await db.flush()
        return build_pack_out(pack)

    @staticmethod
    async def _lock_pack(db: AsyncSession, pack_id: str, *, with_releases: bool = False) -> DriverPack | None:
        """``SELECT … FOR UPDATE`` the pack row.

        Pairs with the ``FOR SHARE`` the allocator takes on the same row before
        it inserts a ``DeviceReservation``: acquiring this lock blocks until any
        in-flight ``create_run`` that observed ``state=enabled`` has committed its
        reservation or aborted, so the count that follows is authoritative.
        """
        stmt = (
            select(DriverPack)
            .where(DriverPack.id == pack_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if with_releases:
            stmt = stmt.options(*_pack_with_releases_options())
        return (await db.execute(stmt)).scalar_one_or_none()


async def complete_drain_if_draining(db: AsyncSession, pack_id: str | None) -> None:
    """Inline drain completion for session/run release paths.

    Cheap unlocked pre-check so hot close paths never touch the pack row lock;
    only a pack observed ``draining`` proceeds to ``try_complete_drain`` (whose
    FOR UPDATE + recount is the correctness authority). A pack flipping to
    draining just after the pre-check is caught by the janitor's backstop
    stage. Deadlock-safe while draining: ``assert_runnable`` fails at the pack
    gate before taking device row locks, so the allocator's pack→device order
    never interleaves with this hook's device→pack order on a draining pack.
    """
    if pack_id is None:
        return
    state = await db.scalar(select(DriverPack.state).where(DriverPack.id == pack_id))
    if state != PackState.draining:
        return
    await PackLifecycleService().try_complete_drain(db, pack_id)


# ──────────────────────────────────────────────────────────────────────────────
