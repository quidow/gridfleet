import importlib
from datetime import UTC, datetime
from types import ModuleType
from typing import TYPE_CHECKING

from sqlalchemy import column, func, select, table

from app.core.metrics import register_gauge_refresher
from app.metrics_recorders import DEVICES_IN_COOLDOWN

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

_SUBMODULES = frozenset({"locking", "models", "routers", "schemas", "services"})

__all__ = ["locking", "models", "routers", "schemas", "services"]

DEVICE_RESERVATIONS = table(
    "device_reservations",
    column("device_id"),
    column("released_at"),
    column("excluded_until"),
)


async def _refresh_devices_gauges(db: "AsyncSession") -> None:
    cooldown_result = await db.execute(
        select(func.count(func.distinct(DEVICE_RESERVATIONS.c.device_id)))
        .select_from(DEVICE_RESERVATIONS)
        .where(DEVICE_RESERVATIONS.c.released_at.is_(None))
        .where(DEVICE_RESERVATIONS.c.excluded_until.is_not(None))
        .where(DEVICE_RESERVATIONS.c.excluded_until > datetime.now(UTC))
    )
    DEVICES_IN_COOLDOWN.set(int(cooldown_result.scalar_one() or 0))


register_gauge_refresher(_refresh_devices_gauges)


def __getattr__(name: str) -> ModuleType:
    if name in _SUBMODULES:
        module = importlib.import_module(f"{__name__}.{name}")
        globals()[name] = module
        return module
    raise AttributeError(name)
