"""One doorbell-woken loop for all scheduled direct-to-Appium probe traffic.

The session observation sweep runs on every cycle. The viability scan follows
at most once per minute; per-device probe due-ness remains persistent in the
state store and is still enforced by ``SessionViabilityService``.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from app.core.background_loop import BackgroundLoop
from app.core.observability import get_logger
from app.sessions.service_sync import SESSION_SYNC_WAKE_SOURCE_TOTAL, register_session_sync_wake_hook

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.core.type_defs import SessionFactory
    from app.sessions.services_container import SessionServices

logger = get_logger(__name__)

LOOP_NAME = "appium_sweep"
VIABILITY_PASS_MIN_INTERVAL_SEC = 60.0


class AppiumSweepLoop(BackgroundLoop):
    """Run session observation and scheduled viability passes in order."""

    loop_name = LOOP_NAME
    cycle_failed_message = "appium_sweep_cycle_failed"

    def __init__(self, *, services: SessionServices) -> None:
        self._services = services
        self._last_viability_pass: float | None = None

    @property
    def _session_factory(self) -> SessionFactory:
        return self._services.session_factory

    async def _on_start(self) -> None:
        register_session_sync_wake_hook(self._services.sync.wake)

    def _interval(self) -> float:
        return self._services.settings.get_float("grid.session_poll_interval_sec")

    async def _run_cycle(self, db: AsyncSession) -> None:
        try:
            await self._services.sync.sync(db)
        except Exception:
            logger.exception("appium_sweep_sync_failed")

        now = time.monotonic()
        if self._last_viability_pass is not None:
            elapsed = now - self._last_viability_pass
            if elapsed < VIABILITY_PASS_MIN_INTERVAL_SEC:
                return
        self._last_viability_pass = now

        try:
            await self._services.viability.check_due_devices(db)
        except Exception:
            logger.exception("appium_sweep_viability_failed")

    async def _wait(self, interval: float) -> None:
        woke = await self._services.sync.wait_for_wake(interval)
        SESSION_SYNC_WAKE_SOURCE_TOTAL.labels(source="doorbell" if woke else "tick").inc()
