"""Claim and release routes are removed from the API surface."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import pytest

from app.main import app

if TYPE_CHECKING:
    from httpx import AsyncClient


def _route_paths() -> set[str]:
    return {getattr(route, "path", "") for route in app.routes}


def test_claim_route_not_registered() -> None:
    assert "/api/runs/{run_id}/claim" not in _route_paths()


def test_release_route_not_registered() -> None:
    assert "/api/runs/{run_id}/release" not in _route_paths()


def test_release_with_cooldown_route_not_registered() -> None:
    assert "/api/runs/{run_id}/devices/{device_id}/release-with-cooldown" not in _route_paths()


@pytest.mark.asyncio
async def test_claim_endpoint_returns_404(client: AsyncClient) -> None:
    resp = await client.post(f"/api/runs/{uuid.uuid4()}/claim", json={"worker_id": "gw0"})
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_release_endpoint_returns_404(client: AsyncClient) -> None:
    resp = await client.post(
        f"/api/runs/{uuid.uuid4()}/release",
        json={"device_id": str(uuid.uuid4()), "worker_id": "gw0"},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_release_with_cooldown_endpoint_returns_404(client: AsyncClient) -> None:
    resp = await client.post(
        f"/api/runs/{uuid.uuid4()}/devices/{uuid.uuid4()}/release-with-cooldown",
        json={"worker_id": "gw0", "reason": "fail", "ttl_seconds": 60},
    )
    assert resp.status_code == 404
