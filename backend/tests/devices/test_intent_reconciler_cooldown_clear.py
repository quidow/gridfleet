"""A failing cooldown clear is contained: the tick still runs the full scan."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

from app.devices.services import intent_reconciler

if TYPE_CHECKING:
    import pytest


async def test_cooldown_clear_failure_does_not_block_the_scan(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(intent_reconciler, "_gc_expired_intents", AsyncMock())
    monkeypatch.setattr(intent_reconciler, "_clear_elapsed_cooldowns", AsyncMock(side_effect=RuntimeError("boom")))
    scan = AsyncMock()
    monkeypatch.setattr(intent_reconciler, "_reconcile_all_devices", scan)
    db = AsyncMock()

    await intent_reconciler.run_device_intent_reconciler_once(
        db, settings=AsyncMock(), circuit_breaker=AsyncMock(), publisher=AsyncMock()
    )

    db.rollback.assert_awaited_once()
    scan.assert_awaited_once()
