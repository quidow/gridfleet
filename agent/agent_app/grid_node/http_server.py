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

from agent_app.grid_node.node_state import NodeState, NoFreeSlotError, NoMatchingSlotError, Reservation
from agent_app.grid_node.protocol import EventType, event_envelope
from agent_app.grid_node.proxy import proxy_request, proxy_websocket

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from starlette.requests import Request
    from starlette.websockets import WebSocket

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
    http_client: httpx.AsyncClient,
    bus: EventPublisher | None = None,
    proxy_request_func: Callable[..., Awaitable[Response]] = proxy_request,
    proxy_websocket_func: Callable[..., Awaitable[None]] = proxy_websocket,
    proxy_timeout: float = 30.0,
    node_status_payload: Callable[[], dict[str, object]] | None = None,
    node_uri: str | None = None,
    node_id: str | None = None,
    slots: list[Slot] | None = None,
) -> Starlette:
    # A single httpx.AsyncClient is shared across all routes for the lifetime
    # of the app. Instantiating one per request leaks ~0.8 MB per call on
    # macOS — TLS contexts, anyio sync primitives, and certifi parse caches
    # are not fully released by the native allocator even after `aclose()`.
    publisher = bus or NoopPublisher()
    stereotypes_by_slot_id: dict[str, dict[str, object]] = {
        slot.id: slot.stereotype.to_dict() for slot in (slots or [])
    }

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
        # W3C clients can place required fields in `firstMatch[i]` rather than
        # `alwaysMatch`; honor both by trying every (alwaysMatch + firstMatch[i])
        # candidate against the slot stereotypes.
        candidates = _w3c_candidate_caps(body)
        reservation, reserve_error = _reserve_first_matching(state, candidates)
        if reservation is None:
            if isinstance(reserve_error, NoFreeSlotError):
                return JSONResponse({"value": {"error": "session not created"}}, status_code=503)
            return JSONResponse({"value": {"error": "session not created"}}, status_code=404)

        try:
            response = await proxy_request_func(
                request,
                upstream=appium_upstream,
                timeout=proxy_timeout,
                client=http_client,
            )
        except Exception:
            state.abort(reservation.id)
            logger.warning("upstream session creation proxy failed", exc_info=True)
            return JSONResponse({"value": {"error": "session not created"}}, status_code=502)
        if response.status_code < 200 or response.status_code >= 300:
            state.abort(reservation.id)
            return response
        session_id, returned_caps = _session_info_from_response(response)
        if session_id is None:
            state.abort(reservation.id)
            return JSONResponse({"value": {"error": "session not created"}}, status_code=502)
        start_iso = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        state.commit(
            reservation.id,
            session_id=session_id,
            started_at=time.monotonic(),
            capabilities=returned_caps,
            session_start_iso=start_iso,
        )
        await _publish_safely(
            publisher,
            event_envelope(EventType.SESSION_STARTED, {"sessionId": session_id, "slotId": reservation.slot_id}),
        )
        return response

    async def delete_session(request: Request) -> Response:
        session_id = request.path_params["session_id"]
        # Capture session capabilities + start time from NodeState BEFORE the
        # upstream proxy call, since `state.release()` clears them afterward.
        prev_caps: dict[str, object] = {}
        prev_start: str | None = None
        for slot in state.snapshot().slots:
            if slot.session_id == session_id:
                prev_caps = dict(slot.session_capabilities or {})
                prev_start = slot.session_start_iso
                break
        try:
            response = await _proxy_http(request)
        except Exception:
            # If the upstream proxy fails (transport reset, protocol error,
            # etc.), the upstream session may or may not be gone. Release the
            # local slot anyway so a stuck session does not pin the node, and
            # surface a 502 to the caller. Selenium's hub treats DELETE 502
            # the same as a transient cleanup failure and retries.
            logger.warning("delete_session upstream proxy failed for %s", session_id, exc_info=True)
            state.release(session_id)
            return JSONResponse(
                {"value": {"error": "session not deleted", "message": "Upstream session delete failed"}},
                status_code=502,
            )
        if 200 <= response.status_code < 300 or response.status_code in {404, 410}:
            state.release(session_id)
            now_iso = datetime.now(UTC).isoformat().replace("+00:00", "Z")
            # Selenium SessionClosedEvent expects a SessionClosedData object —
            # not just a SessionId. Missing fields cause the listener to drop
            # the event silently.
            payload: dict[str, object] = {
                "sessionId": session_id,
                "reason": "QUIT_COMMAND",
                "nodeId": node_id or "",
                "nodeUri": node_uri or "",
                "capabilities": prev_caps,
                "startTime": prev_start or now_iso,
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
        except (json.JSONDecodeError, UnicodeDecodeError):
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
        # Reservation tries each W3C candidate (alwaysMatch + firstMatch[i])
        # so a hub-routed request whose required fields live in `firstMatch`
        # is still matched against the slot stereotypes.
        candidates = _w3c_candidate_caps({"capabilities": w3c_capabilities})
        reservation, reserve_error = _reserve_first_matching(state, candidates)
        if reservation is None:
            if isinstance(reserve_error, NoFreeSlotError):
                return JSONResponse({"value": {"error": "session not created"}}, status_code=503)
            return JSONResponse({"value": {"error": "session not created"}}, status_code=404)

        try:
            upstream = await http_client.post(
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
        except (json.JSONDecodeError, UnicodeDecodeError):
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

        start_iso = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        state.commit(
            reservation.id,
            session_id=session_id,
            started_at=time.monotonic(),
            capabilities=returned_caps,
            session_start_iso=start_iso,
        )
        await _publish_safely(
            publisher,
            event_envelope(EventType.SESSION_STARTED, {"sessionId": session_id, "slotId": reservation.slot_id}),
        )

        stereotype = stereotypes_by_slot_id.get(reservation.slot_id) or dict(always_match)
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
        return await proxy_request_func(
            request,
            upstream=appium_upstream,
            timeout=proxy_timeout,
            client=http_client,
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


def _w3c_candidate_caps(body: object) -> list[dict[str, Any]]:
    """Return ordered (alwaysMatch + firstMatch[i]) capability candidates.

    W3C 8.1 requires the server to merge `alwaysMatch` with each entry of
    `firstMatch` and try the resulting capability sets in order. Per W3C
    7.2, a `firstMatch` entry that re-declares an `alwaysMatch` key with a
    different value is invalid — drop the candidate instead of letting
    `firstMatch` overwrite a required constraint. Returning `[{}]` for
    non-W3C bodies keeps the existing "match anything" behavior so the
    caller still hits its NoMatchingSlot/NoFreeSlot branches as before.
    """
    if not isinstance(body, dict):
        return [{}]
    capabilities = body.get("capabilities")
    if not isinstance(capabilities, dict):
        return [{}]
    raw_always = capabilities.get("alwaysMatch")
    always_match: dict[str, Any] = dict(raw_always) if isinstance(raw_always, dict) else {}
    first_match = capabilities.get("firstMatch")
    if not isinstance(first_match, list) or not first_match:
        return [always_match]
    candidates: list[dict[str, Any]] = []
    for entry in first_match:
        if not isinstance(entry, dict):
            continue
        if any(key in always_match and always_match[key] != value for key, value in entry.items()):
            # Conflicting key with `alwaysMatch` — reject this candidate.
            continue
        merged = dict(always_match)
        merged.update(entry)
        candidates.append(merged)
    return candidates or [always_match]


def _reserve_first_matching(
    state: NodeState, candidates: list[dict[str, Any]]
) -> tuple[Reservation | None, Exception | None]:
    # NoFreeSlotError (slot exists but is BUSY) must take precedence over
    # NoMatchingSlotError (no compatible stereotype) so the caller returns
    # 503 instead of 404 when capacity is the real failure. Without this,
    # a later non-matching candidate would silently downgrade an earlier
    # capacity error.
    no_free_slot_error: Exception | None = None
    no_match_error: Exception | None = None
    for caps in candidates:
        try:
            return state.reserve(caps), None
        except NoFreeSlotError as exc:
            no_free_slot_error = exc
            continue
        except NoMatchingSlotError as exc:
            no_match_error = exc
            continue
    return None, no_free_slot_error or no_match_error


def _session_info_from_response(response: Response) -> tuple[str | None, dict[str, Any] | None]:
    try:
        payload = json.loads(bytes(response.body))
    except (AttributeError, json.JSONDecodeError, UnicodeDecodeError):
        return None, None
    if not isinstance(payload, dict):
        return None, None
    value = payload.get("value")
    if isinstance(value, dict):
        session_id = value.get("sessionId")
        caps = value.get("capabilities")
        if isinstance(session_id, str):
            return session_id, caps if isinstance(caps, dict) else None
    session_id = payload.get("sessionId")
    if isinstance(session_id, str):
        caps = payload.get("capabilities")
        return session_id, caps if isinstance(caps, dict) else None
    return None, None


async def _publish_safely(publisher: EventPublisher, event: dict[str, object]) -> None:
    try:
        await publisher.publish(event)
    except Exception:
        logger.warning("grid node event publish failed after HTTP state transition", exc_info=True)
