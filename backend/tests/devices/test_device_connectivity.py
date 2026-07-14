from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from app.core.leader import state_store as control_plane_state_store
from app.devices.services.connectivity import IP_PING_NAMESPACE, _apply_failure_debounce, _split_ip_ping

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


class _FakeDevice:
    def __init__(self, identity_value: str) -> None:
        self.identity_value = identity_value


def test_split_ip_ping_separates_check() -> None:
    checks = [
        {"check_id": "adb", "ok": True, "message": ""},
        {"check_id": "ip_ping", "ok": False, "message": "ICMP unanswered"},
    ]

    ip_ping, others = _split_ip_ping(checks)

    assert ip_ping == {"check_id": "ip_ping", "ok": False, "message": "ICMP unanswered"}
    assert others == [{"check_id": "adb", "ok": True, "message": ""}]


def test_split_ip_ping_when_absent() -> None:
    checks = [{"check_id": "adb", "ok": True, "message": ""}]

    ip_ping, others = _split_ip_ping(checks)

    assert ip_ping is None
    assert others == checks


@pytest.mark.asyncio
async def test_debounce_suppresses_inside_window(db_session: AsyncSession) -> None:
    fake = _FakeDevice(identity_value="dev-1")
    observed_at = datetime(2026, 7, 11, 12, 0, tzinfo=UTC)

    assert await _apply_failure_debounce(
        db_session, fake, namespace=IP_PING_NAMESPACE, ok=False, window_sec=120, observed_at=observed_at
    )
    assert await _apply_failure_debounce(
        db_session,
        fake,
        namespace=IP_PING_NAMESPACE,
        ok=False,
        window_sec=120,
        observed_at=observed_at + timedelta(seconds=60),
    )
    assert await control_plane_state_store.get_value(db_session, IP_PING_NAMESPACE, fake.identity_value) == (
        observed_at.isoformat()
    )


@pytest.mark.asyncio
async def test_debounce_fires_at_window(db_session: AsyncSession) -> None:
    fake = _FakeDevice(identity_value="dev-2")
    observed_at = datetime(2026, 7, 11, 12, 0, tzinfo=UTC)

    await _apply_failure_debounce(
        db_session, fake, namespace=IP_PING_NAMESPACE, ok=False, window_sec=120, observed_at=observed_at
    )

    assert not await _apply_failure_debounce(
        db_session,
        fake,
        namespace=IP_PING_NAMESPACE,
        ok=False,
        window_sec=120,
        observed_at=observed_at + timedelta(seconds=120),
    )


@pytest.mark.asyncio
async def test_debounce_zero_window_is_strict(db_session: AsyncSession) -> None:
    fake = _FakeDevice(identity_value="dev-3")
    observed_at = datetime(2026, 7, 11, 12, 0, tzinfo=UTC)

    assert not await _apply_failure_debounce(
        db_session, fake, namespace=IP_PING_NAMESPACE, ok=False, window_sec=0, observed_at=observed_at
    )


@pytest.mark.asyncio
async def test_debounce_success_clears(db_session: AsyncSession) -> None:
    fake = _FakeDevice(identity_value="dev-4")
    observed_at = datetime(2026, 7, 11, 12, 0, tzinfo=UTC)

    await _apply_failure_debounce(
        db_session, fake, namespace=IP_PING_NAMESPACE, ok=False, window_sec=120, observed_at=observed_at
    )

    assert await _apply_failure_debounce(
        db_session, fake, namespace=IP_PING_NAMESPACE, ok=True, window_sec=120, observed_at=observed_at
    )
    assert await control_plane_state_store.get_value(db_session, IP_PING_NAMESPACE, fake.identity_value) is None


@pytest.mark.asyncio
async def test_debounce_replay_is_idempotent(db_session: AsyncSession) -> None:
    fake = _FakeDevice(identity_value="dev-5")
    observed_at = datetime(2026, 7, 11, 12, 0, tzinfo=UTC)

    first = await _apply_failure_debounce(
        db_session, fake, namespace=IP_PING_NAMESPACE, ok=False, window_sec=120, observed_at=observed_at
    )
    stored = await control_plane_state_store.get_value(db_session, IP_PING_NAMESPACE, fake.identity_value)
    replay = await _apply_failure_debounce(
        db_session, fake, namespace=IP_PING_NAMESPACE, ok=False, window_sec=120, observed_at=observed_at
    )

    assert (first, stored) == (
        replay,
        await control_plane_state_store.get_value(db_session, IP_PING_NAMESPACE, fake.identity_value),
    )
