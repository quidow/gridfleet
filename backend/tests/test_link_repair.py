from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

from app.core.leader import state_store
from app.devices.services import link_repair
from app.devices.services.link_repair import (
    REPAIR_ATTEMPTS_NAMESPACE,
    REPAIR_MAX_ATTEMPTS,
    dispatch_recommended_action,
    next_repair_attempt,
    reset_repair_attempts,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


@pytest.mark.asyncio
async def test_dispatch_passes_only_driver_agnostic_args(monkeypatch: pytest.MonkeyPatch) -> None:
    # Driver-agnostic core must not inject adb-specific args (e.g. port 5555);
    # the adapter owns the platform default. Capture the args forwarded to the
    # agent dispatch and assert no adb port leaks through.
    captured: dict[str, object] = {}

    async def fake_dispatch(*_args: object, **kwargs: object) -> dict[str, object]:
        captured.update(kwargs)
        return {"success": True}

    monkeypatch.setattr(link_repair, "pack_device_lifecycle_action", fake_dispatch)
    device = SimpleNamespace(
        host=SimpleNamespace(ip="10.0.0.5", agent_port=5100),
        connection_target="10.0.0.20:5555",
        ip_address="10.0.0.20",
        pack_id="appium-uiautomator2",
        platform_id="firetv_real",
    )

    await dispatch_recommended_action(device, "reconnect", settings=AsyncMock(), circuit_breaker=AsyncMock(), pool=None)

    assert captured["args"] == {"ip_address": "10.0.0.20"}
    assert "port" not in captured["args"]  # type: ignore[operator]


@pytest.mark.db
@pytest.mark.asyncio
async def test_attempt_budget_increments_then_exhausts(db_session: AsyncSession) -> None:
    identity = "192.168.1.254:5555"
    seen = []
    for _ in range(REPAIR_MAX_ATTEMPTS + 1):
        seen.append(await next_repair_attempt(db_session, identity))
    # First REPAIR_MAX_ATTEMPTS return an attempt number; the last returns None (exhausted).
    assert seen[:REPAIR_MAX_ATTEMPTS] == list(range(1, REPAIR_MAX_ATTEMPTS + 1))
    assert seen[REPAIR_MAX_ATTEMPTS] is None


@pytest.mark.db
@pytest.mark.asyncio
async def test_reset_clears_counter(db_session: AsyncSession) -> None:
    identity = "192.168.1.254:5555"
    await next_repair_attempt(db_session, identity)
    await reset_repair_attempts(db_session, identity)
    assert await state_store.get_value(db_session, REPAIR_ATTEMPTS_NAMESPACE, identity) is None
    assert await next_repair_attempt(db_session, identity) == 1
