"""Tests for the host-driver-pack feature-action route.

Route under test:
    POST /api/hosts/{host_id}/driver-packs/{pack_id}/features/{feature_id}/actions/{action_id}

The route delegates to ``pack_feature_dispatch_service.dispatch_feature_action``
which in turn POSTs to the agent's ``/agent/pack/features/...`` endpoint and
records ``ok`` via ``pack_feature_status_service.record_feature_status``. We
override the service in the FastAPI app to avoid network I/O.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

import pytest

from app.config import settings as process_settings
from app.main import app
from app.pack.adapter import FeatureActionResult
from app.routers import host_driver_pack_features as feature_routes

if TYPE_CHECKING:
    from collections.abc import Iterator

    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.asyncio


@pytest.fixture
def auth_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, str]]:
    values = {
        "auth_username": "operator",
        "auth_password": "operator-secret",
        "auth_session_secret": "session-secret-for-tests",
        "machine_auth_username": "machine",
        "machine_auth_password": "machine-secret",
    }
    monkeypatch.setattr(process_settings, "auth_enabled", True)
    monkeypatch.setattr(process_settings, "auth_username", values["auth_username"])
    monkeypatch.setattr(process_settings, "auth_password", values["auth_password"])
    monkeypatch.setattr(process_settings, "auth_session_secret", values["auth_session_secret"])
    monkeypatch.setattr(process_settings, "auth_session_ttl_sec", 28_800)
    monkeypatch.setattr(process_settings, "auth_cookie_secure", False)
    monkeypatch.setattr(process_settings, "machine_auth_username", values["machine_auth_username"])
    monkeypatch.setattr(process_settings, "machine_auth_password", values["machine_auth_password"])
    yield values


async def test_action_route_dispatches_to_service(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The route forwards parsed args to the dispatch service and serialises the result."""
    captured: dict[str, Any] = {}

    async def fake_dispatch(
        session: AsyncSession,
        *,
        host_id: uuid.UUID,
        pack_id: str,
        feature_id: str,
        action_id: str,
        args: dict[str, Any],
    ) -> FeatureActionResult:
        del session  # unused — exercise routes only
        captured["host_id"] = host_id
        captured["pack_id"] = pack_id
        captured["feature_id"] = feature_id
        captured["action_id"] = action_id
        captured["args"] = args
        return FeatureActionResult(ok=True, detail="dispatched", data={"k": "v"})

    monkeypatch.setattr(feature_routes, "dispatch_feature_action", fake_dispatch)

    host_id = uuid.uuid4()
    res = await client.post(
        f"/api/hosts/{host_id}/driver-packs/vendor%2Fpack-x/features/feat.x/actions/act.y",
        json={"args": {"foo": "bar"}},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body == {"ok": True, "detail": "dispatched", "data": {"k": "v"}}

    assert captured["host_id"] == host_id
    assert captured["pack_id"] == "vendor/pack-x"
    assert captured["feature_id"] == "feat.x"
    assert captured["action_id"] == "act.y"
    assert captured["args"] == {"foo": "bar"}


async def test_action_route_404_on_unknown_combination(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Service raising HTTPException(404) bubbles out of the route as 404."""
    from fastapi import HTTPException

    async def fake_dispatch(*_args: object, **_kwargs: object) -> FeatureActionResult:
        raise HTTPException(status_code=404, detail="pack not found")

    monkeypatch.setattr(feature_routes, "dispatch_feature_action", fake_dispatch)

    host_id = uuid.uuid4()
    res = await client.post(
        f"/api/hosts/{host_id}/driver-packs/missing-pack/features/x/actions/y",
        json={"args": {}},
    )
    assert res.status_code == 404


async def test_action_route_anonymous_rejected_when_auth_enabled(
    client: AsyncClient,
    auth_settings: dict[str, str],
) -> None:
    """When auth is on the route returns 401/403 for anonymous callers."""
    host_id = uuid.uuid4()
    res = await client.post(
        f"/api/hosts/{host_id}/driver-packs/some-pack/features/feat/actions/act",
        json={"args": {}},
    )
    assert res.status_code in (401, 403)


async def test_action_route_rejects_bad_body(client: AsyncClient) -> None:
    """A body without ``args`` falls back to an empty dict (allowed) but a non-dict ``args`` 422s."""
    host_id = uuid.uuid4()
    res = await client.post(
        f"/api/hosts/{host_id}/driver-packs/p/features/f/actions/a",
        json={"args": "not-an-object"},
    )
    assert res.status_code == 422


async def test_router_registered_on_app() -> None:
    """The feature-action router must be wired into ``app.main.app``."""
    paths = {route.path for route in app.routes if hasattr(route, "path")}
    assert any("/api/hosts/" in p and "/features/" in p and "/actions/" in p for p in paths), (
        "host_driver_pack_features router not registered on app"
    )
