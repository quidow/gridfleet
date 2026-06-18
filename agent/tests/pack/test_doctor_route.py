"""Tests for POST /agent/pack/{pack_id}/doctor endpoint."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx2 import ASGITransport, AsyncClient

from agent_app.pack.adapter_registry import AdapterRegistry
from agent_app.pack.adapter_types import DoctorCheckResult
from agent_app.pack.host_identity import HostIdentity
from agent_app.pack.router import router


def _build_app(
    *,
    adapter_registry: AdapterRegistry | None = None,
    host_id: str = "00000000-0000-0000-0000-000000000001",
) -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    if adapter_registry is not None:
        app.state.adapter_registry = adapter_registry
    hi = HostIdentity()
    hi.set(host_id)
    app.state.host_identity = hi
    return app


class _OkAdapter:
    pack_id = "appium-uiautomator2"
    pack_release = "2026.05.3"

    async def doctor(self, ctx: object) -> list[DoctorCheckResult]:
        return [
            DoctorCheckResult(check_id="adb", ok=True, message="adb found"),
            DoctorCheckResult(check_id="java", ok=True, message="java 17"),
        ]


class _FailingAdapter:
    pack_id = "appium-uiautomator2"
    pack_release = "2026.05.3"

    async def doctor(self, ctx: object) -> list[DoctorCheckResult]:
        raise RuntimeError("doctor exploded")


@pytest.mark.asyncio
async def test_doctor_returns_checks() -> None:
    registry = AdapterRegistry()
    registry.set("appium-uiautomator2", "2026.05.3", _OkAdapter())  # type: ignore[arg-type]
    app = _build_app(adapter_registry=registry)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/agent/pack/appium-uiautomator2/doctor")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["checks"]) == 2
    assert body["checks"][0] == {"check_id": "adb", "ok": True, "message": "adb found"}


@pytest.mark.asyncio
async def test_doctor_no_adapter_returns_empty() -> None:
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/agent/pack/appium-uiautomator2/doctor")
    assert resp.status_code == 200
    assert resp.json()["checks"] == []


@pytest.mark.asyncio
async def test_doctor_adapter_failure_returns_synthetic_entry() -> None:
    registry = AdapterRegistry()
    registry.set("appium-uiautomator2", "2026.05.3", _FailingAdapter())  # type: ignore[arg-type]
    app = _build_app(adapter_registry=registry)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/agent/pack/appium-uiautomator2/doctor")
    assert resp.status_code == 200
    checks = resp.json()["checks"]
    assert len(checks) == 1
    assert checks[0]["check_id"] == "adapter_doctor"
    assert checks[0]["ok"] is False
    assert checks[0]["message"]
