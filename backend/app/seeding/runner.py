"""Runner for demo-data seeding: guardrail, wipe, scenario dispatch, summary."""

from __future__ import annotations

import importlib
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from sqlalchemy import func, select, text

from app.database import Base
from app.seeding.context import SeedContext

if TYPE_CHECKING:
    from collections.abc import Iterable

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = logging.getLogger(__name__)


class DatabaseGuardError(RuntimeError):
    """Raised when the target database cannot safely be wiped by the seed runner."""


def ensure_demo_database_url(url: str, *, allow_any_db: bool) -> None:
    """Raise DatabaseGuardError unless URL points at a `*_demo` database.

    Set `allow_any_db=True` to bypass (controlled by env `GRIDFLEET_SEED_ALLOW_ANY_DB`).
    """
    parsed = urlparse(url)
    db_name = parsed.path.lstrip("/") if parsed.path else ""
    if not db_name:
        raise DatabaseGuardError(f"no database name in URL: {url!r}")
    if allow_any_db:
        return
    if not db_name.endswith("_demo"):
        raise DatabaseGuardError(
            f"refusing to seed {db_name!r}: database name must end with '_demo' "
            f"(or set GRIDFLEET_SEED_ALLOW_ANY_DB=1 to override)"
        )


async def wipe_all_tables(session: AsyncSession, *, table_names: Iterable[str]) -> None:
    """Truncate every provided table with CASCADE and identity restart.

    Alembic's `alembic_version` table is excluded so migrations stay intact.
    """
    safe = [name for name in table_names if name != "alembic_version"]
    if not safe:
        return
    quoted = ", ".join(f'"{name}"' for name in safe)
    await session.execute(text(f"TRUNCATE TABLE {quoted} RESTART IDENTITY CASCADE"))


@dataclass
class SeedResult:
    scenario: str
    row_counts: dict[str, int]
    elapsed_seconds: float

    @property
    def rows_written(self) -> int:
        return sum(self.row_counts.values())


_SCENARIO_REGISTRY: dict[str, tuple[str, str]] = {
    "minimal": ("app.seeding.scenarios.minimal", "apply_minimal"),
    "full_demo": ("app.seeding.scenarios.full_demo", "apply_full_demo"),
    "chaos": ("app.seeding.scenarios.chaos", "apply_chaos"),
}


async def run_scenario(
    *,
    session_factory: async_sessionmaker,  # type: ignore[type-arg]
    scenario: str,
    seed: int,
    wipe: bool,
    skip_telemetry: bool = False,
) -> SeedResult:
    """Apply a named scenario into the database managed by *session_factory*.

    If *wipe* is ``True``, all tables are truncated before seeding.
    ``skip_telemetry`` is forwarded only to scenarios that accept it
    (currently ``full_demo``).
    """
    if scenario not in _SCENARIO_REGISTRY:
        raise ValueError(f"unknown scenario {scenario!r}; available: {sorted(_SCENARIO_REGISTRY)}")
    module_path, func_name = _SCENARIO_REGISTRY[scenario]
    scenario_module = importlib.import_module(module_path)
    apply = getattr(scenario_module, func_name)

    started = time.perf_counter()
    async with session_factory() as session:
        if wipe:
            await wipe_all_tables(session, table_names=sorted(Base.metadata.tables.keys()))
            await session.flush()
        ctx = SeedContext.build(session=session, seed=seed)
        if scenario == "full_demo":
            await apply(ctx, skip_telemetry=skip_telemetry)
        else:
            await apply(ctx)
        await session.commit()
        row_counts = await _collect_row_counts(session)
    elapsed = time.perf_counter() - started
    return SeedResult(scenario=scenario, row_counts=row_counts, elapsed_seconds=elapsed)


async def _collect_row_counts(session: AsyncSession) -> dict[str, int]:
    counts: dict[str, int] = {}
    for table_name, table in sorted(Base.metadata.tables.items()):
        if table_name == "alembic_version":
            continue
        count = await session.scalar(select(func.count()).select_from(table))
        counts[table_name] = int(count or 0)
    return counts
