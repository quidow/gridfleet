from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
async def test_recovery_probe_stops_on_first_success() -> None:
    from app.devices.services import lifecycle_policy

    probe_mock = AsyncMock(return_value={"status": "passed"})

    with (
        patch.object(lifecycle_policy.session_viability, "run_session_viability_probe", probe_mock),
        patch.object(lifecycle_policy, "_reload_device", AsyncMock(side_effect=lambda db, dev: dev)),
    ):
        result = await lifecycle_policy._run_recovery_probe(SimpleNamespace(), SimpleNamespace(id="dev-1"))

    assert probe_mock.await_count == 1
    assert result == {"status": "passed"}


@pytest.mark.asyncio
async def test_recovery_probe_retries_until_attempts_exhausted(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.devices.services import lifecycle_policy

    monkeypatch.setattr(lifecycle_policy, "RECOVERY_PROBE_RETRY_DELAY_SEC", 0)

    probe_mock = AsyncMock(return_value={"status": "failed", "error": "boom"})

    with (
        patch.object(lifecycle_policy.session_viability, "run_session_viability_probe", probe_mock),
        patch.object(lifecycle_policy, "_reload_device", AsyncMock(side_effect=lambda db, dev: dev)),
    ):
        result = await lifecycle_policy._run_recovery_probe(SimpleNamespace(), SimpleNamespace(id="dev-1"))

    assert probe_mock.await_count == lifecycle_policy.RECOVERY_PROBE_ATTEMPTS
    assert result == {"status": "failed", "error": "boom"}


@pytest.mark.asyncio
async def test_recovery_probe_retries_then_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.devices.services import lifecycle_policy

    monkeypatch.setattr(lifecycle_policy, "RECOVERY_PROBE_RETRY_DELAY_SEC", 0)

    outcomes: list[dict[str, Any]] = [
        {"status": "failed", "error": "x"},
        {"status": "failed", "error": "y"},
        {"status": "passed"},
    ]
    probe_mock = AsyncMock(side_effect=outcomes)

    with (
        patch.object(lifecycle_policy.session_viability, "run_session_viability_probe", probe_mock),
        patch.object(lifecycle_policy, "_reload_device", AsyncMock(side_effect=lambda db, dev: dev)),
    ):
        result = await lifecycle_policy._run_recovery_probe(SimpleNamespace(), SimpleNamespace(id="dev-1"))

    assert probe_mock.await_count == 3
    assert result == {"status": "passed"}
