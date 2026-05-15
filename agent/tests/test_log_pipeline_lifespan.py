from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest

from agent_app import observability
from agent_app.lifespan import _start_log_shipper_when_ready
from agent_app.pack.host_identity import HostIdentity

if TYPE_CHECKING:
    from agent_app.logs.schemas import ShippedLogLine


@pytest.mark.asyncio
async def test_start_log_shipper_waits_for_host_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    observed: dict[str, object] = {}
    queue: asyncio.Queue[ShippedLogLine] = asyncio.Queue()
    monkeypatch.setattr(observability, "shipper_queue", queue)

    class _FakeLogShipperTask:
        def __init__(
            self,
            *,
            client: object,
            host_id: object,
            boot_id: object,
            queue: object,
            base_url: str,
            auth: object,
        ) -> None:
            observed.update(
                {
                    "client": client,
                    "host_id": host_id,
                    "boot_id": boot_id,
                    "queue": queue,
                    "base_url": base_url,
                    "auth": auth,
                }
            )

        async def run(self) -> None:
            observed["ran"] = True

    monkeypatch.setattr("agent_app.lifespan.LogShipperTask", _FakeLogShipperTask)
    monkeypatch.setattr("agent_app.lifespan.get_shared_http_client", lambda: "client")
    monkeypatch.setattr("agent_app.lifespan._manager_auth", lambda: "auth")

    identity = HostIdentity()
    host_id = uuid4()
    identity.set(str(host_id))
    boot_id = uuid4()

    await _start_log_shipper_when_ready(identity, "http://manager:8000", boot_id=boot_id)

    assert observed["client"] == "client"
    assert observed["host_id"] == host_id
    assert observed["boot_id"] == boot_id
    assert observed["queue"] is queue
    assert observed["base_url"] == "http://manager:8000"
    assert observed["auth"] == "auth"
    assert observed["ran"] is True
