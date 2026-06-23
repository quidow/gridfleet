from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from app.sessions import service_kill
from tests.helpers import create_device_record

if TYPE_CHECKING:
    from httpx2 import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.devices.models import Device


async def _device(db_session: AsyncSession, host_id: str) -> Device:
    return await create_device_record(
        db_session,
        host_id=host_id,
        identity_value="kill-test-device",
        connection_target="kill-test-device",
        name="Kill Test Phone",
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        os_version="14",
    )


@pytest.fixture
def terminate_calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    calls: list[tuple[str, str]] = []

    async def fake_terminate(target: str, session_id: str, *, timeout: float = 10.0) -> bool:
        calls.append((target, session_id))
        return True

    monkeypatch.setattr(service_kill.appium_direct, "terminate_session", fake_terminate)
    return calls


async def test_kill_running_session(
    client: AsyncClient, db_session: AsyncSession, default_host_id: str, terminate_calls: list[tuple[str, str]]
) -> None:
    from app.sessions.models import Session, SessionStatus

    device = await _device(db_session, default_host_id)
    session = Session(
        session_id="kill-me-1",
        device_id=device.id,
        status=SessionStatus.running,
        router_target="http://10.0.0.5:4723",
    )
    db_session.add(session)
    await db_session.commit()
    db_session.expunge(session)

    resp = await client.post("/api/sessions/kill-me-1/kill")
    assert resp.status_code == 200
    data = resp.json()
    assert data["terminated"] is True
    assert data["session"]["status"] == "error"
    assert data["session"]["error_type"] == "operator_kill"
    assert data["session"]["ended_at"] is not None
    # No live appium_node in this test, so the stored router_target is used.
    assert terminate_calls == [("http://10.0.0.5:4723", "kill-me-1")]


async def test_kill_terminalizes_even_when_appium_unreachable(
    client: AsyncClient, db_session: AsyncSession, default_host_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.sessions.models import Session, SessionStatus

    async def failing_terminate(target: str, session_id: str, *, timeout: float = 10.0) -> bool:
        return False

    monkeypatch.setattr(service_kill.appium_direct, "terminate_session", failing_terminate)

    device = await _device(db_session, default_host_id)
    session = Session(
        session_id="kill-me-2",
        device_id=device.id,
        status=SessionStatus.running,
        router_target="http://10.0.0.5:4723",
    )
    db_session.add(session)
    await db_session.commit()
    db_session.expunge(session)

    resp = await client.post("/api/sessions/kill-me-2/kill")
    assert resp.status_code == 200
    data = resp.json()
    assert data["terminated"] is False
    assert data["session"]["status"] == "error"
    assert data["session"]["ended_at"] is not None


async def test_kill_session_without_target_skips_terminate(
    client: AsyncClient, db_session: AsyncSession, default_host_id: str, terminate_calls: list[tuple[str, str]]
) -> None:
    from app.sessions.models import Session, SessionStatus

    device = await _device(db_session, default_host_id)
    session = Session(session_id="kill-me-3", device_id=device.id, status=SessionStatus.running)
    db_session.add(session)
    await db_session.commit()
    db_session.expunge(session)

    resp = await client.post("/api/sessions/kill-me-3/kill")
    assert resp.status_code == 200
    assert resp.json()["terminated"] is False
    assert terminate_calls == []


async def test_kill_unknown_session_is_404(client: AsyncClient, terminate_calls: list[tuple[str, str]]) -> None:
    resp = await client.post("/api/sessions/nope/kill")
    assert resp.status_code == 404
    assert terminate_calls == []


async def test_kill_ended_session_is_409(
    client: AsyncClient, db_session: AsyncSession, default_host_id: str, terminate_calls: list[tuple[str, str]]
) -> None:
    from app.sessions.models import Session, SessionStatus

    device = await _device(db_session, default_host_id)
    session = Session(
        session_id="already-done",
        device_id=device.id,
        status=SessionStatus.passed,
        ended_at=datetime.now(UTC),
    )
    db_session.add(session)
    await db_session.commit()
    db_session.expunge(session)

    resp = await client.post("/api/sessions/already-done/kill")
    assert resp.status_code == 409
    assert terminate_calls == []


async def test_kill_pending_session_is_409(
    client: AsyncClient, db_session: AsyncSession, default_host_id: str, terminate_calls: list[tuple[str, str]]
) -> None:
    """Pending rows (allocate->confirm window) have no Appium session yet; the
    allocation reaper owns them. Kill applies to running sessions only."""
    from app.sessions.models import Session, SessionStatus

    device = await _device(db_session, default_host_id)
    session = Session(session_id="alloc-pending", device_id=device.id, status=SessionStatus.pending)
    db_session.add(session)
    await db_session.commit()
    db_session.expunge(session)

    resp = await client.post("/api/sessions/alloc-pending/kill")
    assert resp.status_code == 409
    assert terminate_calls == []
