"""Direct Appium HTTP operations (spec §6) — the only backend->Appium call site."""

import functools
import json
import logging
from http import HTTPStatus
from typing import Any
from urllib.parse import quote

import httpx2 as httpx

from app.core import metrics_recorders

logger = logging.getLogger(__name__)


@functools.cache
def _get_client() -> httpx.AsyncClient:
    """Return a shared, connection-pooled client.

    A fresh ``httpx.AsyncClient()`` per call re-runs ``create_ssl_context`` every
    time — pathological here because the session-observation sweep hits this module
    on every tick for every running session. Per-call ``timeout=`` arguments stay on
    each request; the shared client carries no default timeout that would fight them.
    The cache is the process-lifetime singleton; ``aclose`` clears it.
    """
    return httpx.AsyncClient(
        limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
    )


async def aclose() -> None:
    """Close the shared client (app lifespan shutdown; tests)."""
    if _get_client.cache_info().currsize == 0:
        return
    client = _get_client()
    if not client.is_closed:
        await client.aclose()
    _get_client.cache_clear()


async def terminate_session(target: str, session_id: str, *, timeout: float = 10.0) -> bool:
    """DELETE a session on the Appium node. 404 means already gone (success).

    ``session_id`` can originate from an operator request path (the kill
    endpoint) and session rows can be registered with arbitrary ids, so it is
    percent-encoded into both the URL and the log line — a crafted id must not
    alter the request path or forge log entries.
    """
    sid = quote(session_id, safe="")
    try:
        resp = await _get_client().delete(f"{target}/session/{sid}", timeout=timeout)
        return resp.status_code == HTTPStatus.NOT_FOUND or resp.is_success
    except httpx.HTTPError as exc:
        metrics_recorders.APPIUM_TERMINATE_FAILED_TOTAL.inc()
        logger.warning("appium_terminate_failed target=%s session=%s err=%s", target, sid, exc)
        return False


async def session_alive(target: str, session_id: str, *, timeout: float = 10.0) -> bool | None:
    """W3C Get Timeouts as a side-effect-free liveness probe.

    True = alive; False = Appium answered 'this session does not exist';
    None = indeterminate (network error) — callers MUST NOT treat None as dead.
    """
    try:
        resp = await _get_client().get(f"{target}/session/{session_id}/timeouts", timeout=timeout)
    except httpx.HTTPError:
        return None
    if resp.is_success:
        return True
    return False if resp.status_code == HTTPStatus.NOT_FOUND else None


async def list_sessions(target: str, *, timeout: float = 10.0) -> list[str] | None:
    """Enumerate active sessions via GET /appium/sessions (Appium 3.x; requires the
    'session_discovery' insecure feature on the node). None = unsupported/unreachable.
    """
    try:
        resp = await _get_client().get(f"{target}/appium/sessions", timeout=timeout)
    except httpx.HTTPError:
        return None
    if not resp.is_success:
        return None
    value = resp.json().get("value")
    if not isinstance(value, list):
        return None
    return [s["id"] for s in value if isinstance(s, dict) and isinstance(s.get("id"), str)]


async def create_session(
    target: str, capabilities: dict[str, Any], *, timeout: float
) -> tuple[str | None, str | None, bool]:
    """POST /session for viability probes. Returns (session_id, error, transport_error).

    ``transport_error`` is True only when the request never reached an HTTP response
    (``httpx.HTTPError`` — connect/read failure). HTTP refusals (status >=400) and
    non-JSON bodies return False: the node answered, the session was just refused.
    Callers map ``transport_error`` to an indeterminate probe verdict.

    ``timeout`` is keyword-required on purpose: the probe timeout is caller-driven.
    """
    try:
        resp = await _get_client().post(f"{target}/session", json=capabilities, timeout=timeout)
    except httpx.HTTPError as exc:
        return None, str(exc), True
    # A non-JSON error body (HTML 502, plain-text crash dump) must not escape as a
    # JSONDecodeError — fall back to the raw text/status. Mirrors service_viability.
    try:
        body = resp.json()
    except ValueError:
        return None, resp.text or f"status {resp.status_code}", False
    value = body.get("value")
    value = value if isinstance(value, dict) else {}
    sid = value.get("sessionId")
    if resp.is_success and isinstance(sid, str):
        return sid, None, False
    return None, str(value.get("message", f"status {resp.status_code}")), False


async def create_session_raw(target: str, body: bytes, *, timeout: float) -> tuple[int, bytes, str | None]:
    """POST /session for the router flow with the client's serialized W3C body."""
    try:
        resp = await _get_client().post(
            f"{target}/session",
            content=body,
            headers={"Content-Type": "application/json"},
            timeout=timeout,
        )
    except httpx.HTTPError as exc:
        return 0, b"", str(exc)
    return resp.status_code, resp.content, None


def extract_session_id(body: bytes) -> str | None:
    """Extract a W3C or legacy JSONWP session id from an Appium response."""
    try:
        parsed = json.loads(body)
    except ValueError:
        return None
    if not isinstance(parsed, dict):
        return None
    value = parsed.get("value")
    sid = value.get("sessionId") if isinstance(value, dict) else None
    if isinstance(sid, str) and sid:
        return sid
    top = parsed.get("sessionId")
    return top if isinstance(top, str) and top else None
