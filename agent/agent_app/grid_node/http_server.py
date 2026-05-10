from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Protocol

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


class EventPublisher(Protocol):
    async def publish(self, event: dict[str, object]) -> None: ...


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
) -> Starlette:
    publisher = bus or NoopPublisher()

    async def status(_request: Request) -> JSONResponse:
        snapshot = state.snapshot()
        return JSONResponse(
            {
                "value": {
                    "message": "GridFleet Python Grid Node",
                    "ready": True,
                    "slots": [
                        {
                            "id": slot.slot_id,
                            "state": slot.state,
                            "sessionId": slot.session_id,
                        }
                        for slot in snapshot.slots
                    ],
                }
            }
        )

    async def owner(request: Request) -> JSONResponse:
        session_id = request.path_params["session_id"]
        owned = any(slot.session_id == session_id for slot in state.snapshot().slots)
        return JSONResponse({"value": owned})

    async def create_session(request: Request) -> Response:
        body = await request.json()
        caps = _always_match_caps(body)
        try:
            reservation = state.reserve(caps)
        except NoMatchingSlotError:
            return JSONResponse({"value": {"error": "session not created"}}, status_code=404)
        except NoFreeSlotError:
            return JSONResponse({"value": {"error": "session not created"}}, status_code=503)

        async with httpx.AsyncClient() as client:
            response = await proxy_request_func(
                request,
                upstream=appium_upstream,
                timeout=proxy_timeout,
                client=client,
            )
        if response.status_code < 200 or response.status_code >= 300:
            state.abort(reservation.id)
            return response
        session_id = _session_id_from_response(response)
        if session_id is None:
            state.abort(reservation.id)
            return JSONResponse({"value": {"error": "session not created"}}, status_code=502)
        state.commit(reservation.id, session_id=session_id, started_at=time.monotonic())
        await publisher.publish(
            event_envelope(EventType.SESSION_STARTED, {"sessionId": session_id, "slotId": reservation.slot_id})
        )
        return response

    async def delete_session(request: Request) -> Response:
        session_id = request.path_params["session_id"]
        response = await _proxy_http(request)
        if 200 <= response.status_code < 300:
            state.release(session_id)
            await publisher.publish(event_envelope(EventType.SESSION_CLOSED, {"sessionId": session_id}))
        return response

    async def drain(_request: Request) -> JSONResponse:
        state.mark_drain()
        await publisher.publish(event_envelope(EventType.NODE_DRAIN, {}))
        return JSONResponse({"value": True})

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
