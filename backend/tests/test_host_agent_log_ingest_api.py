from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest

if TYPE_CHECKING:
    from httpx import AsyncClient

    from app.hosts.models import Host

pytestmark = pytest.mark.db


@pytest.mark.asyncio
async def test_ingest_happy_path(client: AsyncClient, db_host: Host) -> None:
    payload = {
        "boot_id": str(uuid4()),
        "lines": [
            {
                "ts": datetime.now(UTC).isoformat(),
                "level": "INFO",
                "logger_name": "agent.foo",
                "message": "hello",
                "sequence_no": 0,
            }
        ],
    }
    resp = await client.post(f"/agent/{db_host.id}/log-batch", json=payload)
    assert resp.status_code == 202
    body = resp.json()
    assert body["accepted"] == 1
    assert body["deduped"] == 0


@pytest.mark.asyncio
async def test_ingest_dedup_on_retry(client: AsyncClient, db_host: Host) -> None:
    payload = {
        "boot_id": str(uuid4()),
        "lines": [
            {
                "ts": datetime.now(UTC).isoformat(),
                "level": "INFO",
                "logger_name": "agent.foo",
                "message": "hello",
                "sequence_no": n,
            }
            for n in range(3)
        ],
    }
    first = await client.post(f"/agent/{db_host.id}/log-batch", json=payload)
    second = await client.post(f"/agent/{db_host.id}/log-batch", json=payload)
    assert first.status_code == 202
    assert second.status_code == 202
    assert second.json() == {"accepted": 0, "deduped": 3}


@pytest.mark.asyncio
async def test_ingest_rejects_malformed_body(client: AsyncClient, db_host: Host) -> None:
    resp = await client.post(f"/agent/{db_host.id}/log-batch", json={"boot_id": "not-uuid", "lines": []})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_ingest_unknown_host_returns_404(client: AsyncClient) -> None:
    payload = {
        "boot_id": str(uuid4()),
        "lines": [
            {
                "ts": datetime.now(UTC).isoformat(),
                "level": "INFO",
                "logger_name": "agent.foo",
                "message": "hello",
                "sequence_no": 0,
            }
        ],
    }
    resp = await client.post(f"/agent/{uuid4()}/log-batch", json=payload)
    assert resp.status_code == 404
