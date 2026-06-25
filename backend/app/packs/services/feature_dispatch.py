"""Backend-side dispatcher for driver-pack feature actions.

The user clicks "collect bugreport" (or any other adapter-defined action) on
the host detail page; the frontend POSTs to
``/api/hosts/{host_id}/driver-packs/{pack_id}/features/{feature_id}/actions/{action_id}``;
this dispatcher resolves the host, validates the feature exists in the pack
release, then forwards the call to the host agent's feature-action endpoint.

It always records the result via :meth:`FeatureService.record_feature_status`
so the existing ``pack_feature.degraded`` / ``pack_feature.recovered`` SystemEvent
fires on transitions — including transient agent failures, which are
treated as degraded.
"""

from __future__ import annotations

from dataclasses import dataclass
from http import HTTPStatus
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import quote

import httpx2 as httpx
from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import selectinload

from app.agent_comm.client import AgentClientFactory, AgentHttpClient
from app.agent_comm.client import request as agent_request
from app.core.errors import AgentCallError
from app.hosts.models import Host
from app.packs.adapter import FeatureActionResult
from app.packs.models import DriverPack, DriverPackRelease, HostPackFeatureStatus
from app.packs.services.release_ordering import selected_release

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.agent_comm.protocols import CircuitBreakerProtocol
    from app.events.protocols import EventPublisher


_DEFAULT_TIMEOUT_SEC: float = 30.0
type _AgentClientLike = AgentHttpClient | httpx.AsyncClient

EVENT_DEGRADED = "pack_feature.degraded"
EVENT_RECOVERED = "pack_feature.recovered"


def _as_agent_client(client: _AgentClientLike) -> AgentHttpClient:
    return cast("AgentHttpClient", client)


class _AgentDispatchError(Exception):
    """Internal wrapper carrying the user-facing detail string for a failed call."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


@dataclass(frozen=True, slots=True)
class FeatureActionTarget:
    """The (host, pack, feature, action) identity for a feature-action dispatch."""

    host_id: uuid.UUID
    pack_id: str
    feature_id: str
    action_id: str
    args: dict[str, Any]


class FeatureService:
    """Service class for feature-action dispatch and feature status recording."""

    def __init__(self, *, publisher: EventPublisher, circuit_breaker: CircuitBreakerProtocol) -> None:
        self._publisher = publisher
        self._circuit_breaker = circuit_breaker

    async def dispatch_feature_action(
        self,
        session: AsyncSession,
        *,
        target: FeatureActionTarget,
        http_client_factory: AgentClientFactory = httpx.AsyncClient,
        timeout: float | int = _DEFAULT_TIMEOUT_SEC,
        agent_auth: httpx.BasicAuth | None = None,
    ) -> FeatureActionResult:
        """Forward a feature-action call to the host agent and persist the result.

        Raises:
            HTTPException(404): host, pack, or feature not found.
            HTTPException(502): the agent returned a 5xx, was unreachable, or
                replied with malformed JSON. The status row is still recorded as
                ``ok=False`` so subscribers see a ``pack_feature.degraded`` event.
        """
        host = await session.get(Host, target.host_id)
        if host is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Host {target.host_id} not found")

        pack = (
            await session.execute(
                select(DriverPack)
                .where(DriverPack.id == target.pack_id)
                .options(selectinload(DriverPack.releases).selectinload(DriverPackRelease.features))
            )
        ).scalar_one_or_none()
        if pack is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Pack {target.pack_id} not found")

        release = selected_release(list(pack.releases), pack.current_release)
        if release is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Pack {target.pack_id} has no releases",
            )

        feature_ids = {feat.manifest_feature_id for feat in release.features}
        if target.feature_id not in feature_ids:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Feature {target.feature_id} not found in pack {target.pack_id} release {release.release}",
            )

        agent_url = (
            f"http://{host.ip}:{host.agent_port}"
            f"/agent/pack/features/{quote(target.feature_id, safe='')}/actions/{quote(target.action_id, safe='')}"
        )
        body = {"pack_id": target.pack_id, "args": dict(target.args)}

        try:
            result = await self._call_agent(
                host=host.ip,
                url=agent_url,
                body=body,
                http_client_factory=http_client_factory,
                timeout=timeout,
                agent_auth=agent_auth,
            )
        except _AgentDispatchError as exc:
            # Record the degraded state so event subscribers learn about the
            # outage immediately, then convert to 502 for the caller.
            await self.record_feature_status(
                session,
                host_id=target.host_id,
                pack_id=target.pack_id,
                feature_id=target.feature_id,
                ok=False,
                detail=exc.detail,
            )
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=exc.detail) from exc

        await self.record_feature_status(
            session,
            host_id=target.host_id,
            pack_id=target.pack_id,
            feature_id=target.feature_id,
            ok=result.ok,
            detail=result.detail,
        )
        return result

    async def record_feature_status(
        self,
        session: AsyncSession,
        *,
        host_id: uuid.UUID,
        pack_id: str,
        feature_id: str,
        ok: bool,
        detail: str,
    ) -> bool:
        """Upsert the (host, pack, feature) status row and emit an event on transition.

        Returns ``True`` when the persisted ``ok`` flipped (or was newly recorded
        as degraded), otherwise ``False``.
        """
        existing = (
            await session.execute(
                select(HostPackFeatureStatus).where(
                    HostPackFeatureStatus.host_id == host_id,
                    HostPackFeatureStatus.pack_id == pack_id,
                    HostPackFeatureStatus.feature_id == feature_id,
                )
            )
        ).scalar_one_or_none()

        transitioned: bool
        event_type: str | None
        if existing is None:
            transitioned = not ok
            event_type = EVENT_DEGRADED if not ok else None
        elif existing.ok != ok:
            transitioned = True
            event_type = EVENT_RECOVERED if ok else EVENT_DEGRADED
        else:
            transitioned = False
            event_type = None

        stmt = insert(HostPackFeatureStatus).values(
            host_id=host_id,
            pack_id=pack_id,
            feature_id=feature_id,
            ok=ok,
            detail=detail,
        )
        stmt = stmt.on_conflict_do_update(
            constraint="host_pack_feature_status_uq",
            set_={"ok": stmt.excluded.ok, "detail": stmt.excluded.detail},
        )
        await session.execute(stmt)
        await session.flush()
        if existing is not None:
            await session.refresh(existing)

        if event_type is not None:
            self._publisher.queue_for_session(
                session,
                event_type,
                {
                    "host_id": str(host_id),
                    "pack_id": pack_id,
                    "feature_id": feature_id,
                    "ok": ok,
                    "detail": detail,
                },
            )

        return transitioned

    async def _call_agent(
        self,
        *,
        host: str,
        url: str,
        body: dict[str, Any],
        http_client_factory: AgentClientFactory,
        timeout: float | int,
        agent_auth: httpx.BasicAuth | None = None,
    ) -> FeatureActionResult:
        """POST the action body to the agent and parse the response.

        Mirrors the ``_send_request`` pattern used by
        :mod:`app.agent_comm.operations` (see ``node_service.py``
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
                    client_mode="fresh",
                    client=_as_agent_client(client),
                    json_body=body,
                    timeout=timeout,
                    circuit_breaker=self._circuit_breaker,
                    auth=agent_auth,
                )
        except AgentCallError as exc:
            raise _AgentDispatchError(f"Agent unreachable: {exc.message}") from exc
        except httpx.HTTPError as exc:
            raise _AgentDispatchError(f"Agent transport error: {exc}") from exc

        status_code = response.status_code
        if status_code >= HTTPStatus.INTERNAL_SERVER_ERROR:
            raise _AgentDispatchError(f"Agent feature action failed (HTTP {status_code})")
        if status_code >= HTTPStatus.BAD_REQUEST:
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
