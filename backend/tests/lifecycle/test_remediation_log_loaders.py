from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from app.lifecycle.services import remediation_log
from tests.fakes import FakeSettingsReader
from tests.helpers import create_device_record

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.hosts.models import Host


pytestmark = pytest.mark.db


def _settings() -> FakeSettingsReader:
    return FakeSettingsReader(
        {
            "general.lifecycle_recovery_backoff_base_sec": 10,
            "general.lifecycle_recovery_backoff_max_sec": 40,
        }
    )


async def test_append_attempt_uses_exponential_backoff_and_loads_ladder(
    db_session: AsyncSession, db_host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    device = await create_device_record(
        db_session,
        host_id=db_host.id,
        identity_value="remediation-log-loaders-attempts",
        name="remediation-log-loaders-attempts",
    )
    fixed_now = datetime(2026, 7, 12, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(remediation_log, "now_utc", lambda: fixed_now)
    settings = _settings()

    deadlines = []
    for attempt_no, expected_seconds in enumerate((10, 20, 40, 40), start=1):
        entry, ladder = await remediation_log.append_attempt(
            db_session,
            device.id,
            source="node_health",
            reason=f"failure {attempt_no}",
            settings=settings,
        )
        deadlines.append(entry.backoff_until)
        assert ladder.attempts == attempt_no
        assert entry.backoff_until == fixed_now + timedelta(seconds=expected_seconds)
    await db_session.commit()

    loaded = await remediation_log.load_ladder(db_session, device.id)
    assert loaded.attempts == 4
    assert loaded.backoff_until == fixed_now + timedelta(seconds=40)
    assert loaded.last_failure_reason == "failure 4"
    assert deadlines == [
        fixed_now + timedelta(seconds=10),
        fixed_now + timedelta(seconds=20),
        fixed_now + timedelta(seconds=40),
        fixed_now + timedelta(seconds=40),
    ]


async def test_append_reset_supersedes_ladder_and_load_ladders_fills_empty_ids(
    db_session: AsyncSession, db_host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    device = await create_device_record(
        db_session,
        host_id=db_host.id,
        identity_value="remediation-log-loaders-reset",
        name="remediation-log-loaders-reset",
    )
    empty_device = await create_device_record(
        db_session,
        host_id=db_host.id,
        identity_value="remediation-log-loaders-empty",
        name="remediation-log-loaders-empty",
    )
    fixed_now = datetime(2026, 7, 12, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(remediation_log, "now_utc", lambda: fixed_now)

    await remediation_log.append_attempt(
        db_session,
        device.id,
        source="node_health",
        reason="failure",
        settings=_settings(),
    )
    await remediation_log.append_reset(db_session, device.id, source="device_checks", action="self_healed")
    await db_session.commit()

    ladder = await remediation_log.load_ladder(db_session, device.id)
    assert ladder.attempts == 0
    assert ladder.backoff_until is None
    assert ladder.last_failure_reason is None
    assert ladder.last_action == "self_healed"
    ladders = await remediation_log.load_ladders(db_session, [device.id, empty_device.id])
    assert ladders[device.id] == ladder
    assert ladders[empty_device.id] == remediation_log.EMPTY_LADDER


async def test_load_active_backoffs_filters_expired_and_reset_entries(
    db_session: AsyncSession, db_host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    active_device = await create_device_record(
        db_session,
        host_id=db_host.id,
        identity_value="remediation-log-loaders-active",
        name="remediation-log-loaders-active",
    )
    reset_device = await create_device_record(
        db_session,
        host_id=db_host.id,
        identity_value="remediation-log-loaders-superseded",
        name="remediation-log-loaders-superseded",
    )
    expired_device = await create_device_record(
        db_session,
        host_id=db_host.id,
        identity_value="remediation-log-loaders-expired",
        name="remediation-log-loaders-expired",
    )
    fixed_now = datetime(2026, 7, 12, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(remediation_log, "now_utc", lambda: fixed_now)
    settings = _settings()

    await remediation_log.append_attempt(
        db_session,
        active_device.id,
        source="node_health",
        reason="active",
        settings=settings,
    )
    await remediation_log.append_attempt(
        db_session,
        reset_device.id,
        source="node_health",
        reason="superseded",
        settings=settings,
    )
    await remediation_log.append_reset(db_session, reset_device.id, source="device_checks", action="self_healed")
    await remediation_log.append_entry(
        db_session,
        expired_device.id,
        kind=remediation_log.KIND_ATTEMPT,
        source="node_health",
        action="recovery_failed",
        reason="expired",
        backoff_until=fixed_now - timedelta(seconds=1),
    )
    await db_session.commit()

    active = await remediation_log.load_active_backoffs(db_session, now=fixed_now)

    assert active == {active_device.id: fixed_now + timedelta(seconds=10)}
