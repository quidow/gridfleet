"""Backend-side dispatcher for driver-pack feature actions.

The user clicks "collect bugreport" (or any other adapter-defined action) on
the host detail page; the frontend POSTs to
``/api/hosts/{host_id}/driver-packs/{pack_id}/features/{feature_id}/actions/{action_id}``;
this dispatcher resolves the host, validates the feature exists in the pack
release, then forwards the call to the host agent's feature-action endpoint.

It always records the result via :func:`pack_feature_status_service.record_feature_status`
so the existing ``pack_feature.degraded`` / ``pack_feature.recovered`` SystemEvent
webhook fires on transitions — including transient agent failures, which are
treated as degraded.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast
from urllib.parse import quote

import httpx
from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.agent_client import AgentClientFactory, AgentHttpClient
from app.agent_client import request as agent_request
from app.errors import AgentCallError
from app.models.driver_pack import DriverPack, DriverPackRelease
from app.models.host import Host
from app.pack.adapter import FeatureActionResult
from app.services.pack_feature_status_service import record_feature_status
from app.services.pack_release_ordering import selected_release

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession


_DEFAULT_TIMEOUT_SEC: float = 30.0


async def dispatch_feature_action(
    session: AsyncSession,
    *,
    host_id: uuid.UUID,
    pack_id: str,
    feature_id: str,
    action_id: str,
    args: dict[str, Any],
    http_client_factory: AgentClientFactory = httpx.AsyncClient,
    timeout: float | int = _DEFAULT_TIMEOUT_SEC,
) -> FeatureActionResult:
    """Forward a feature-action call to the host agent and persist the result.

    Raises:
        HTTPException(404): host, pack, or feature not found.
        HTTPException(502): the agent returned a 5xx, was unreachable, or
            replied with malformed JSON. The status row is still recorded as
            ``ok=False`` so subscribers see a ``pack_feature.degraded`` event.
    """
    host = await session.get(Host, host_id)
    if host is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Host {host_id} not found")

    pack = (
        await session.execute(
            select(DriverPack)
            .where(DriverPack.id == pack_id)
            .options(selectinload(DriverPack.releases).selectinload(DriverPackRelease.features))
        )
    ).scalar_one_or_none()
    if pack is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Pack {pack_id} not found")

    release = selected_release(list(pack.releases), pack.current_release)
    if release is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Pack {pack_id} has no releases",
        )

    feature_ids = {feat.manifest_feature_id for feat in release.features}
    if feature_id not in feature_ids:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Feature {feature_id} not found in pack {pack_id} release {release.release}",
        )

    agent_url = (
        f"http://{host.ip}:{host.agent_port}"
        f"/agent/pack/features/{quote(feature_id, safe='')}/actions/{quote(action_id, safe='')}"
    )
    body = {"pack_id": pack_id, "args": dict(args)}

    try:
        result = await _call_agent(
            host=host.ip,
            url=agent_url,
            body=body,
            http_client_factory=http_client_factory,
            timeout=timeout,
        )
    except _AgentDispatchError as exc:
        # Record the degraded state so webhook subscribers learn about the
        # outage immediately, then convert to 502 for the caller.
        await record_feature_status(
            session,
            host_id=host_id,
            pack_id=pack_id,
            feature_id=feature_id,
            ok=False,
            detail=exc.detail,
        )
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=exc.detail) from exc

    await record_feature_status(
        session,
        host_id=host_id,
        pack_id=pack_id,
        feature_id=feature_id,
        ok=result.ok,
        detail=result.detail,
    )
    return result


class _AgentDispatchError(Exception):
    """Internal wrapper carrying the user-facing detail string for a failed call."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


async def _call_agent(
    *,
    host: str,
    url: str,
    body: dict[str, Any],
    http_client_factory: AgentClientFactory,
    timeout: float | int,
) -> FeatureActionResult:
    """POST the action body to the agent and parse the response.

    Mirrors the ``_send_request`` pattern used by
    :mod:`app.services.agent_operations` (see ``node_service.py``
    and ``agent_operations.py:24-47``) so that circuit-breaker bookkeeping,
    request-id headers, and timeout semantics stay consistent across all
    backend → agent calls.
    """
    try:
        client_manager = http_client_factory(timeout=timeout)
        async with client_manager as client:
            response = await agent_request(
                "POST",
                url,
                endpoint="pack_feature_action",
                host=host,
                client=cast("AgentHttpClient", client),
                json_body=body,
                timeout=timeout,
            )
    except AgentCallError as exc:
        raise _AgentDispatchError(f"Agent unreachable: {exc.message}") from exc
    except httpx.HTTPError as exc:
        raise _AgentDispatchError(f"Agent transport error: {exc}") from exc

    status_code = response.status_code
    if status_code >= 500:
        raise _AgentDispatchError(f"Agent feature action failed (HTTP {status_code})")
    if status_code >= 400:
        # 4xx from the agent is a permanent error — surface as failed dispatch.
        raise _AgentDispatchError(f"Agent rejected feature action (HTTP {status_code})")

    try:
        payload = response.json()
    except ValueError as exc:
        raise _AgentDispatchError("Agent feature action returned invalid JSON") from exc

    if not isinstance(payload, dict):
        raise _AgentDispatchError("Agent feature action payload is not an object")

    ok_value = payload.get("ok")
    if not isinstance(ok_value, bool):
        raise _AgentDispatchError("Agent feature action payload missing boolean 'ok'")

    detail_value = payload.get("detail", "")
    detail_str = detail_value if isinstance(detail_value, str) else ""

    data_value = payload.get("data", {})
    data_dict: dict[str, Any] = data_value if isinstance(data_value, dict) else {}

    return FeatureActionResult(ok=ok_value, detail=detail_str, data=data_dict)
