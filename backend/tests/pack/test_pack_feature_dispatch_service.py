"""Unit tests for ``pack_feature_dispatch_service.dispatch_feature_action``.

The service forwards a feature-action invocation from a host's driver pack to
the host agent over HTTP, parses the agent's reply into a ``FeatureActionResult``,
records the resulting ``ok`` via ``pack_feature_status_service.record_feature_status``,
and returns the parsed result.

Validation cases:
- 404 when the host is missing.
- 404 when the pack is missing.
- 404 when the requested feature_id is not present in the pack release.
- 502 when the agent surface returns 5xx / is unreachable; status row is still
  recorded as ``ok=False`` so the existing ``pack_feature.degraded`` webhook fires.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.packs.models import DriverPack, DriverPackFeature, DriverPackRelease, HostPackFeatureStatus
from app.packs.services import feature_dispatch as pack_feature_dispatch_service

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.agent_comm.client import QueryParams, RequestHeaders
    from app.hosts.models import Host


PACK_ID = "local/feature-dispatch-test"
FEATURE_ID = "android.diagnostics"
ACTION_ID = "collect-bugreport"


def _response(method: str, url: str, *, status_code: int = 200, payload: object) -> httpx.Response:
    return httpx.Response(status_code, request=httpx.Request(method, url), json=payload)


class StrictAgentClient:
    """Minimal ``AgentHttpClient`` stand-in that records every call."""

    def __init__(
        self,
        *,
        post_response: httpx.Response | None = None,
        post_exception: Exception | None = None,
    ) -> None:
        self.post_response = post_response or _response("POST", "http://example.test", payload={})
        self.post_exception = post_exception
        self.post_calls: list[tuple[str, dict[str, object]]] = []

    async def __aenter__(self) -> StrictAgentClient:
        return self

    async def __aexit__(self, _exc_type: object, _exc: object, _tb: object) -> bool:
        return False

    async def get(  # pragma: no cover — dispatch only POSTs.
        self,
        url: str,
        *,
        params: QueryParams = None,
        headers: RequestHeaders = None,
        timeout: float | int | None = None,
        auth: httpx.Auth | None = None,
    ) -> httpx.Response:
        del auth
        raise AssertionError("dispatch_feature_action must not GET the agent")

    async def post(
        self,
        url: str,
        *,
        params: QueryParams = None,
        headers: RequestHeaders = None,
        json: object | None = None,
        timeout: float | int | None = None,
        auth: httpx.Auth | None = None,
    ) -> httpx.Response:
        self.post_calls.append(
            (
                url,
                {"params": params, "headers": headers, "json": json, "timeout": timeout, "auth": auth},
            )
        )
        if self.post_exception is not None:
            raise self.post_exception
        return self.post_response


def _factory(client: StrictAgentClient) -> object:
    def make(*, timeout: float | int) -> StrictAgentClient:
        del timeout
        return client

    return make


async def _seed_pack_with_feature(session: AsyncSession, *, pack_id: str, feature_id: str) -> DriverPackRelease:
    pack = DriverPack(
        id=pack_id,
        origin="uploaded",
        display_name=pack_id,
        maintainer="",
        license="",
    )
    session.add(pack)
    await session.flush()
    release = DriverPackRelease(
        id=uuid.uuid4(),
        pack_id=pack.id,
        release="0.1.0",
        manifest_json={},
    )
    session.add(release)
    await session.flush()
    feature = DriverPackFeature(
        pack_release_id=release.id,
        manifest_feature_id=feature_id,
        data={"label": feature_id, "actions": [{"id": ACTION_ID, "label": "Collect"}]},
    )
    session.add(feature)
    await session.flush()
    return release


@pytest.mark.asyncio
async def test_dispatch_calls_agent_and_records_ok_status(
    db_session: AsyncSession,
    sample_host: Host,
) -> None:
    """Happy path: agent returns ok=True, status recorded, result returned."""
    await _seed_pack_with_feature(db_session, pack_id=PACK_ID, feature_id=FEATURE_ID)
    await db_session.commit()

    expected_url = (
        f"http://{sample_host.ip}:{sample_host.agent_port}/agent/pack/features/{FEATURE_ID}/actions/{ACTION_ID}"
    )
    client = StrictAgentClient(
        post_response=_response(
            "POST",
            expected_url,
            payload={"ok": True, "detail": "captured 1 file", "data": {"path": "/tmp/bugreport.zip"}},
        )
    )

    result = await pack_feature_dispatch_service.dispatch_feature_action(
        db_session,
        host_id=sample_host.id,
        pack_id=PACK_ID,
        feature_id=FEATURE_ID,
        action_id=ACTION_ID,
        args={"with_logs": True},
        http_client_factory=_factory(client),
    )
    await db_session.commit()

    assert result.ok is True
    assert result.detail == "captured 1 file"
    assert result.data == {"path": "/tmp/bugreport.zip"}

    assert len(client.post_calls) == 1
    posted_url, kwargs = client.post_calls[0]
    assert posted_url == expected_url
    assert kwargs["json"] == {"pack_id": PACK_ID, "args": {"with_logs": True}}

    row = (
        await db_session.execute(
            select(HostPackFeatureStatus).where(
                HostPackFeatureStatus.host_id == sample_host.id,
                HostPackFeatureStatus.pack_id == PACK_ID,
                HostPackFeatureStatus.feature_id == FEATURE_ID,
            )
        )
    ).scalar_one()
    assert row.ok is True
    assert row.detail == "captured 1 file"


@pytest.mark.asyncio
async def test_dispatch_uses_configured_agent_auth(
    monkeypatch: pytest.MonkeyPatch,
    db_session: AsyncSession,
    sample_host: Host,
) -> None:
    from app.agent_comm import client as agent_client

    await _seed_pack_with_feature(db_session, pack_id=PACK_ID, feature_id=FEATURE_ID)
    await db_session.commit()
    monkeypatch.setattr(agent_client._settings, "agent_auth_username", "ops")
    monkeypatch.setattr(agent_client._settings, "agent_auth_password", "secret")

    client = StrictAgentClient(
        post_response=_response(
            "POST",
            "http://x/agent/pack/features/x/actions/y",
            payload={"ok": True, "detail": "done", "data": {}},
        )
    )

    await pack_feature_dispatch_service.dispatch_feature_action(
        db_session,
        host_id=sample_host.id,
        pack_id=PACK_ID,
        feature_id=FEATURE_ID,
        action_id=ACTION_ID,
        args={},
        http_client_factory=_factory(client),
    )

    assert client.post_calls
    _, kwargs = client.post_calls[0]
    assert isinstance(kwargs["auth"], httpx.BasicAuth)


@pytest.mark.asyncio
async def test_dispatch_records_failure_when_agent_returns_not_ok(
    db_session: AsyncSession,
    sample_host: Host,
) -> None:
    """Agent returns ok=False — service still records degraded status and returns the failed result."""
    await _seed_pack_with_feature(db_session, pack_id=PACK_ID, feature_id=FEATURE_ID)
    await db_session.commit()

    client = StrictAgentClient(
        post_response=_response(
            "POST",
            "http://x/agent/pack/features/x/actions/y",
            payload={"ok": False, "detail": "device offline", "data": {}},
        )
    )

    result = await pack_feature_dispatch_service.dispatch_feature_action(
        db_session,
        host_id=sample_host.id,
        pack_id=PACK_ID,
        feature_id=FEATURE_ID,
        action_id=ACTION_ID,
        args={},
        http_client_factory=_factory(client),
    )
    await db_session.commit()

    assert result.ok is False
    assert result.detail == "device offline"
    assert result.data == {}

    row = (
        await db_session.execute(
            select(HostPackFeatureStatus).where(
                HostPackFeatureStatus.host_id == sample_host.id,
                HostPackFeatureStatus.pack_id == PACK_ID,
                HostPackFeatureStatus.feature_id == FEATURE_ID,
            )
        )
    ).scalar_one()
    assert row.ok is False
    assert row.detail == "device offline"


@pytest.mark.asyncio
async def test_dispatch_404_when_host_missing(
    db_session: AsyncSession,
) -> None:
    await _seed_pack_with_feature(db_session, pack_id=PACK_ID, feature_id=FEATURE_ID)
    await db_session.commit()

    client = StrictAgentClient()

    with pytest.raises(HTTPException) as exc_info:
        await pack_feature_dispatch_service.dispatch_feature_action(
            db_session,
            host_id=uuid.uuid4(),  # unknown
            pack_id=PACK_ID,
            feature_id=FEATURE_ID,
            action_id=ACTION_ID,
            args={},
            http_client_factory=_factory(client),
        )
    assert exc_info.value.status_code == 404
    assert client.post_calls == []  # never reached the agent


@pytest.mark.asyncio
async def test_dispatch_404_when_pack_missing(
    db_session: AsyncSession,
    sample_host: Host,
) -> None:
    client = StrictAgentClient()

    with pytest.raises(HTTPException) as exc_info:
        await pack_feature_dispatch_service.dispatch_feature_action(
            db_session,
            host_id=sample_host.id,
            pack_id="no-such-pack",
            feature_id=FEATURE_ID,
            action_id=ACTION_ID,
            args={},
            http_client_factory=_factory(client),
        )
    assert exc_info.value.status_code == 404
    assert client.post_calls == []


@pytest.mark.asyncio
async def test_dispatch_404_when_feature_id_not_in_release(
    db_session: AsyncSession,
    sample_host: Host,
) -> None:
    await _seed_pack_with_feature(db_session, pack_id=PACK_ID, feature_id=FEATURE_ID)
    await db_session.commit()
    client = StrictAgentClient()

    with pytest.raises(HTTPException) as exc_info:
        await pack_feature_dispatch_service.dispatch_feature_action(
            db_session,
            host_id=sample_host.id,
            pack_id=PACK_ID,
            feature_id="not.in.release",
            action_id=ACTION_ID,
            args={},
            http_client_factory=_factory(client),
        )
    assert exc_info.value.status_code == 404
    assert client.post_calls == []


@pytest.mark.asyncio
async def test_dispatch_502_on_agent_5xx(
    db_session: AsyncSession,
    sample_host: Host,
) -> None:
    """Agent returns 5xx — service raises 502 *and* records degraded status."""
    await _seed_pack_with_feature(db_session, pack_id=PACK_ID, feature_id=FEATURE_ID)
    await db_session.commit()

    client = StrictAgentClient(
        post_response=_response(
            "POST",
            "http://example.test",
            status_code=503,
            payload={"detail": "agent down"},
        )
    )

    with pytest.raises(HTTPException) as exc_info:
        await pack_feature_dispatch_service.dispatch_feature_action(
            db_session,
            host_id=sample_host.id,
            pack_id=PACK_ID,
            feature_id=FEATURE_ID,
            action_id=ACTION_ID,
            args={},
            http_client_factory=_factory(client),
        )
    assert exc_info.value.status_code == 502
    await db_session.commit()

    row = (
        await db_session.execute(
            select(HostPackFeatureStatus).where(
                HostPackFeatureStatus.host_id == sample_host.id,
                HostPackFeatureStatus.pack_id == PACK_ID,
                HostPackFeatureStatus.feature_id == FEATURE_ID,
            )
        )
    ).scalar_one()
    assert row.ok is False
    assert "503" in row.detail or row.detail


@pytest.mark.asyncio
async def test_dispatch_502_on_transport_error_records_degraded(
    db_session: AsyncSession,
    sample_host: Host,
) -> None:
    """If the agent client raises ``httpx.HTTPError``, surface 502 and record degraded."""
    await _seed_pack_with_feature(db_session, pack_id=PACK_ID, feature_id=FEATURE_ID)
    await db_session.commit()

    client = StrictAgentClient(post_exception=httpx.ConnectError("connection refused"))

    with pytest.raises(HTTPException) as exc_info:
        await pack_feature_dispatch_service.dispatch_feature_action(
            db_session,
            host_id=sample_host.id,
            pack_id=PACK_ID,
            feature_id=FEATURE_ID,
            action_id=ACTION_ID,
            args={},
            http_client_factory=_factory(client),
        )
    assert exc_info.value.status_code == 502
    await db_session.commit()

    row = (
        await db_session.execute(
            select(HostPackFeatureStatus).where(
                HostPackFeatureStatus.host_id == sample_host.id,
                HostPackFeatureStatus.pack_id == PACK_ID,
                HostPackFeatureStatus.feature_id == FEATURE_ID,
            )
        )
    ).scalar_one()
    assert row.ok is False
    assert row.detail


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "message"),
    [
        (_response("POST", "http://example.test", status_code=400, payload={"ok": True}), "rejected"),
        (_response("POST", "http://example.test", status_code=500, payload={"ok": True}), "failed"),
        (
            httpx.Response(200, request=httpx.Request("POST", "http://example.test"), content=b"not-json"),
            "invalid JSON",
        ),
        (_response("POST", "http://example.test", payload=[]), "not an object"),
        (_response("POST", "http://example.test", payload={"ok": "yes"}), "missing boolean"),
    ],
)
async def test_call_agent_rejects_bad_agent_responses(
    monkeypatch: pytest.MonkeyPatch,
    response: httpx.Response,
    message: str,
) -> None:
    monkeypatch.setattr(pack_feature_dispatch_service, "agent_request", AsyncMock(return_value=response))

    with pytest.raises(pack_feature_dispatch_service._AgentDispatchError, match=message):
        await pack_feature_dispatch_service._call_agent(
            host="10.0.0.1",
            url="http://10.0.0.1:5100/agent/pack/features/f/actions/a",
            body={"pack_id": PACK_ID, "args": {}},
            http_client_factory=_factory(StrictAgentClient()),
            timeout=5,
        )
