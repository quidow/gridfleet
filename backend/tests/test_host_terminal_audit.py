import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.hosts import service_terminal_audit as host_terminal_audit
from app.hosts.models import Host, HostStatus, HostTerminalSession, OSType


@pytest.mark.asyncio
async def test_open_and_close_session(setup_database: AsyncEngine) -> None:
    sessionmaker = async_sessionmaker(setup_database, expire_on_commit=False)
    async with sessionmaker() as db:
        host = Host(
            hostname=f"term-host-{uuid.uuid4().hex[:8]}",
            ip="10.0.0.5",
            os_type=OSType.linux,
            agent_port=5100,
            status=HostStatus.online,
        )
        db.add(host)
        await db.flush()

        session_id = await host_terminal_audit.open_session(
            db, host_id=host.id, opened_by="alice", client_ip="1.2.3.4", shell="/bin/zsh"
        )
        assert session_id is not None

    async with sessionmaker() as db:
        row = (await db.execute(select(HostTerminalSession))).scalar_one()
        assert row.id == session_id
        assert row.opened_by == "alice"
        assert row.closed_at is None

        await host_terminal_audit.close_session(
            db, session_id=session_id, close_reason="client_disconnect", agent_pid=99
        )

    async with sessionmaker() as db:
        row = (await db.execute(select(HostTerminalSession))).scalar_one()
        assert row.close_reason == "client_disconnect"
        assert row.closed_at is not None
        assert row.agent_pid == 99


@pytest.mark.asyncio
async def test_close_session_noop_on_missing_id(setup_database: AsyncEngine) -> None:
    sessionmaker = async_sessionmaker(setup_database, expire_on_commit=False)
    async with sessionmaker() as db:
        await host_terminal_audit.close_session(db, session_id=uuid.uuid4(), close_reason="client_disconnect")
