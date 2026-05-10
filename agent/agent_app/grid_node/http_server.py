from __future__ import annotations

import base64
import json
import logging
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol

import httpx
from starlette.applications import Starlette
from starlette.responses import JSONResponse, Response
from starlette.routing import Route, WebSocketRoute

from agent_app.grid_node.node_state import NoFreeSlotError, NoMatchingSlotError
from agent_app.grid_node.protocol import EventType, event_envelope
from agent_app.grid_node.proxy import proxy_request, proxy_websocket

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from starlette.requests import Request
    from starlette.websockets import WebSocket

    from agent_app.grid_node.node_state import NodeState
    from agent_app.grid_node.protocol import Slot


class EventPublisher(Protocol):
    async def publish(self, event: dict[str, object]) -> None:
        raise NotImplementedError


logger = logging.getLogger(__name__)


class NoopPublisher:
    async def publish(self, event: dict[str, object]) -> None:
        return None


def build_app(
    *,
    state: NodeState,
    appium_upstream: str,
    bus: EventPublisher | None = None,
    proxy_request_func: Callable[..., Awaitable[Response]] = proxy_request,
    proxy_websocket_func: Callable[..., Awaitable[None]] = proxy_websocket,
    proxy_timeout: float = 30.0,
    node_status_payload: Callable[[], dict[str, object]] | None = None,
    node_uri: str | None = None,
    node_id: str | None = None,
    slots: list[Slot] | None = None,
) -> Starlette:
    publisher = bus or NoopPublisher()
    stereotypes_by_slot_id: dict[str, dict[str, object]] = {
        slot.id: slot.stereotype.to_dict() for slot in (slots or [])
    }
    session_metadata: dict[str, dict[str, object]] = {}

    async def status(_request: Request) -> JSONResponse:
        # Selenium hub's `RemoteNode.getStatus` parses /status as
        # `{"value": {"node": <NodeStatus>, "ready": <bool>}}`. The W3C-style
        # node summary (with `message`/`slots`) used during early development
        # is not enough — registration silently fails without a NodeStatus.
        snapshot = state.snapshot()
        node_status = (
            node_status_payload()
            if node_status_payload is not None
            else {
                "message": "GridFleet Python Grid Node",
                "slots": [
                    {
                        "id": slot.slot_id,
                        "state": slot.state,
                        "sessionId": slot.session_id,
                    }
                    for slot in snapshot.slots
                ],
            }
        )
        return JSONResponse(
            {
                "value": {
                    "ready": True,
                    "message": "GridFleet Python Grid Node",
                    "node": node_status,
                }
            }
        )

    async def owner(request: Request) -> JSONResponse:
        session_id = request.path_params["session_id"]
        owned = any(slot.session_id == session_id for slot in state.snapshot().slots)
        return JSONResponse({"value": owned})

    async def create_session(request: Request) -> Response:
        try:
            body = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            return JSONResponse(
                {"value": {"error": "invalid argument", "message": "Malformed JSON request body"}}, status_code=400
            )
        caps = _always_match_caps(body)
        try:
            reservation = state.reserve(caps)
        except NoMatchingSlotError:
            return JSONResponse({"value": {"error": "session not created"}}, status_code=404)
        except NoFreeSlotError:
            return JSONResponse({"value": {"error": "session not created"}}, status_code=503)

        try:
            async with httpx.AsyncClient() as client:
                response = await proxy_request_func(
                    request,
                    upstream=appium_upstream,
                    timeout=proxy_timeout,
                    client=client,
                )
        except Exception:
            state.abort(reservation.id)
            logger.warning("upstream session creation proxy failed", exc_info=True)
            return JSONResponse({"value": {"error": "session not created"}}, status_code=502)
        if response.status_code < 200 or response.status_code >= 300:
            state.abort(reservation.id)
            return response
        session_id = _session_id_from_response(response)
        if session_id is None:
            state.abort(reservation.id)
            return JSONResponse({"value": {"error": "session not created"}}, status_code=502)
        state.commit(reservation.id, session_id=session_id, started_at=time.monotonic())
        await _publish_safely(
            publisher,
            event_envelope(EventType.SESSION_STARTED, {"sessionId": session_id, "slotId": reservation.slot_id}),
        )
        return response

    async def delete_session(request: Request) -> Response:
        session_id = request.path_params["session_id"]
        response = await _proxy_http(request)
        if 200 <= response.status_code < 300 or response.status_code in {404, 410}:
            state.release(session_id)
            metadata = session_metadata.pop(session_id, None)
            now_iso = datetime.now(UTC).isoformat().replace("+00:00", "Z")
            # Selenium SessionClosedEvent expects a SessionClosedData object —
            # not just a SessionId. Missing fields cause the listener to drop
            # the event silently.
            payload: dict[str, object] = {
                "sessionId": session_id,
                "reason": "QUIT_COMMAND",
                "nodeId": node_id or "",
                "nodeUri": node_uri or "",
                "capabilities": (metadata or {}).get("capabilities", {}),
                "startTime": (metadata or {}).get("startTime", now_iso),
                "endTime": now_iso,
            }
            await _publish_safely(publisher, event_envelope(EventType.SESSION_CLOSED, payload))
        return response

    async def drain(_request: Request) -> JSONResponse:
        state.mark_drain()
        await _publish_safely(publisher, event_envelope(EventType.NODE_DRAIN, {}))
        return JSONResponse({"value": True})

    async def hub_create_session(request: Request) -> Response:
        # Selenium hub posts CreateSessionRequest here when routing a session to
        # this node. The body wraps the W3C `capabilities` object plus the set of
        # downstream dialects the client supports.
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return JSONResponse(
                {"value": {"error": "invalid argument", "message": "Malformed JSON request body"}}, status_code=400
            )
        raw_capabilities = body.get("capabilities") if isinstance(body, dict) else None
        capabilities: dict[str, Any] = raw_capabilities if isinstance(raw_capabilities, dict) else {}
        # Selenium's hub sends `capabilities` as a flat capabilities map (no
        # alwaysMatch/firstMatch wrapping), but Appium's /session endpoint
        # requires the W3C `{alwaysMatch, firstMatch}` shape.
        if "alwaysMatch" in capabilities or "firstMatch" in capabilities:
            w3c_capabilities: dict[str, Any] = capabilities
            raw_always = capabilities.get("alwaysMatch")
            always_match: dict[str, Any] = raw_always if isinstance(raw_always, dict) else {}
        else:
            always_match = dict(capabilities)
            w3c_capabilities = {"alwaysMatch": always_match, "firstMatch": [{}]}
        try:
            reservation = state.reserve(always_match)
        except NoMatchingSlotError:
            return JSONResponse({"value": {"error": "session not created"}}, status_code=404)
        except NoFreeSlotError:
            return JSONResponse({"value": {"error": "session not created"}}, status_code=503)

        async with httpx.AsyncClient() as client:
            try:
                upstream = await client.post(
                    f"{appium_upstream}/session",
                    json={"capabilities": w3c_capabilities},
                    timeout=proxy_timeout,
                )
            except httpx.HTTPError:
                state.abort(reservation.id)
                return JSONResponse({"value": {"error": "session not created"}}, status_code=502)

        if upstream.status_code < 200 or upstream.status_code >= 300:
            state.abort(reservation.id)
            return Response(content=upstream.content, status_code=upstream.status_code)

        upstream_bytes = upstream.content
        try:
            upstream_payload = json.loads(upstream_bytes)
        except json.JSONDecodeError:
            state.abort(reservation.id)
            return JSONResponse({"value": {"error": "session not created"}}, status_code=502)

        upstream_value = upstream_payload.get("value") if isinstance(upstream_payload, dict) else None
        if not isinstance(upstream_value, dict):
            state.abort(reservation.id)
            return JSONResponse({"value": {"error": "session not created"}}, status_code=502)

        session_id = upstream_value.get("sessionId")
        returned_caps = upstream_value.get("capabilities")
        if not isinstance(session_id, str) or not isinstance(returned_caps, dict):
            state.abort(reservation.id)
            return JSONResponse({"value": {"error": "session not created"}}, status_code=502)

        state.commit(reservation.id, session_id=session_id, started_at=time.monotonic())
        await _publish_safely(
            publisher,
            event_envelope(EventType.SESSION_STARTED, {"sessionId": session_id, "slotId": reservation.slot_id}),
        )

        stereotype = stereotypes_by_slot_id.get(reservation.slot_id) or dict(always_match)
        start_iso = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        session_metadata[session_id] = {
            "capabilities": returned_caps,
            "startTime": start_iso,
            "slotId": reservation.slot_id,
        }
        session_payload: dict[str, object] = {
            "sessionId": session_id,
            "uri": node_uri or "",
            "capabilities": returned_caps,
            "stereotype": stereotype,
            "start": start_iso,
        }
        return JSONResponse(
            {
                "value": {
                    "sessionResponse": {
                        "session": session_payload,
                        "downstreamEncodedResponse": base64.b64encode(upstream_bytes).decode("ascii"),
                    }
                }
            }
        )

    async def command_proxy(request: Request) -> Response:
        return await _proxy_http(request)

    async def websocket_proxy(websocket: WebSocket) -> None:
        await proxy_websocket_func(websocket, upstream=appium_upstream)

    async def _proxy_http(request: Request) -> Response:
        async with httpx.AsyncClient() as client:
            return await proxy_request_func(
                request,
                upstream=appium_upstream,
                timeout=proxy_timeout,
                client=client,
            )

    return Starlette(
        routes=[
            Route("/status", status, methods=["GET"]),
            Route("/session", create_session, methods=["POST"]),
            Route("/session/{session_id}", delete_session, methods=["DELETE"]),
            Route("/se/grid/node/session", hub_create_session, methods=["POST"]),
            Route("/se/grid/node/drain", drain, methods=["POST"]),
            Route("/se/grid/node/owner/{session_id}", owner, methods=["POST"]),
            Route("/{path:path}", command_proxy, methods=["GET", "POST", "PUT", "DELETE", "PATCH"]),
            WebSocketRoute("/{path:path}", websocket_proxy),
        ]
    )


def _always_match_caps(body: object) -> dict[str, object]:
    if not isinstance(body, dict):
        return {}
    capabilities = body.get("capabilities")
    if not isinstance(capabilities, dict):
        return {}
    always_match = capabilities.get("alwaysMatch")
    if not isinstance(always_match, dict):
        return {}
    return dict(always_match)


def _session_id_from_response(response: Response) -> str | None:
    try:
        payload = json.loads(bytes(response.body))
    except (AttributeError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    value = payload.get("value")
    if isinstance(value, dict):
        value_session_id = value.get("sessionId")
        if isinstance(value_session_id, str):
            return value_session_id
    session_id = payload.get("sessionId")
    if isinstance(session_id, str):
        return session_id
    return None


async def _publish_safely(publisher: EventPublisher, event: dict[str, object]) -> None:
    try:
        await publisher.publish(event)
    except Exception:
        logger.warning("grid node event publish failed after HTTP state transition", exc_info=True)
