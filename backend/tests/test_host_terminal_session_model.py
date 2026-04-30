import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.models.host import Host, HostStatus, OSType
from app.models.host_terminal_session import HostTerminalSession


@pytest.mark.asyncio
async def test_host_terminal_session_persists(setup_database: AsyncEngine) -> None:
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

        row = HostTerminalSession(
            host_id=host.id,
            opened_by="alice",
            client_ip="10.0.0.5",
            shell="/bin/zsh",
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)
        assert row.id is not None
        assert row.opened_at is not None
        assert row.closed_at is None

        row.closed_at = datetime.now(UTC)
        row.close_reason = "client_disconnect"
        row.agent_pid = 4242
        await db.commit()
        await db.refresh(row)
        assert row.close_reason == "client_disconnect"
        assert row.agent_pid == 4242
