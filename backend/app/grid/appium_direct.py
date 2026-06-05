"""Direct Appium HTTP operations (spec §6) — the only backend->Appium call site."""

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)


async def terminate_session(target: str, session_id: str, *, timeout: float = 10.0) -> bool:
    """DELETE a session on the Appium node. 404 means already gone (success)."""
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.delete(f"{target}/session/{session_id}", timeout=timeout)
        return resp.status_code == 404 or resp.is_success
    except httpx.HTTPError as exc:
        logger.warning("appium_terminate_failed target=%s session=%s err=%s", target, session_id, exc)
        return False


async def session_alive(target: str, session_id: str, *, timeout: float = 10.0) -> bool | None:
    """W3C Get Timeouts as a side-effect-free liveness probe.

    True = alive; False = Appium answered 'this session does not exist';
    None = indeterminate (network error) — callers MUST NOT treat None as dead.
    """
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{target}/session/{session_id}/timeouts", timeout=timeout)
    except httpx.HTTPError:
        return None
    if resp.is_success:
        return True
    return False if resp.status_code == 404 else None


async def list_sessions(target: str, *, timeout: float = 10.0) -> list[str] | None:
    """Enumerate active sessions via GET /appium/sessions (Appium 3.x; requires the
    'session_discovery' insecure feature on the node). None = unsupported/unreachable.
    """
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{target}/appium/sessions", timeout=timeout)
    except httpx.HTTPError:
        return None
    if not resp.is_success:
        return None
    value = resp.json().get("value")
    if not isinstance(value, list):
        return None
    return [s["id"] for s in value if isinstance(s, dict) and isinstance(s.get("id"), str)]


async def create_session(target: str, capabilities: dict[str, Any], *, timeout: float) -> tuple[str | None, str | None]:
    """POST /session for viability probes. Returns (session_id, error).

    ``timeout`` is keyword-required on purpose: the probe timeout is caller-driven.
    """
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(f"{target}/session", json=capabilities, timeout=timeout)
    except httpx.HTTPError as exc:
        return None, str(exc)
    # A non-JSON error body (HTML 502, plain-text crash dump) must not escape as a
    # JSONDecodeError — fall back to the raw text/status. Mirrors service_viability.
    try:
        body = resp.json()
    except ValueError:
        return None, resp.text or f"status {resp.status_code}"
    value = body.get("value")
    value = value if isinstance(value, dict) else {}
    sid = value.get("sessionId")
    if resp.is_success and isinstance(sid, str):
        return sid, None
    return None, str(value.get("message", f"status {resp.status_code}"))
