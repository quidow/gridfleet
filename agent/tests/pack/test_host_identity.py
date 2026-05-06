import asyncio

import pytest

from agent_app.pack.host_identity import HostIdentity


@pytest.mark.asyncio
async def test_wait_returns_host_id_once_set() -> None:
    identity = HostIdentity()

    async def _set_later() -> None:
        await asyncio.sleep(0.01)
        identity.set("00000000-0000-0000-0000-000000000001")

    task = asyncio.create_task(_set_later())
    host_id = await identity.wait()
    _ = await task  # ensure clean completion
    assert host_id == "00000000-0000-0000-0000-000000000001"


@pytest.mark.asyncio
async def test_wait_returns_existing_value_without_blocking() -> None:
    identity = HostIdentity()
    identity.set("00000000-0000-0000-0000-000000000002")
    assert await asyncio.wait_for(identity.wait(), timeout=0.1) == "00000000-0000-0000-0000-000000000002"
