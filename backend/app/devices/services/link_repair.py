"""Generic adapter-recommended link repair: dispatch + attempt budget.

Driver-agnostic. The android pack adapter decides *whether* repair applies and
*which* manifest action remediates (``recommended_action`` on its health
result). This module only dispatches that action and bounds retries; it knows
nothing about adb.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx2 as httpx

from app.agent_comm.operations import pack_device_lifecycle_action
from app.core.leader import state_store

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.agent_comm.http_pool import AgentHttpPool
    from app.agent_comm.protocols import CircuitBreakerProtocol
    from app.devices.models import Device

REPAIR_ATTEMPTS_NAMESPACE = "device_checks.repair_attempts"
REPAIR_MAX_ATTEMPTS = 3


async def next_repair_attempt(db: AsyncSession, identity_value: str) -> int | None:
    """Return the 1-based attempt number to use now, or None if the budget is spent.

    One-tick backoff is implicit: the connectivity loop dispatches at most once
    per device per cycle, so consecutive cycles naturally space attempts by the
    probe interval. The counter resets on any healthy probe (see
    ``reset_repair_attempts``), so a later genuine recovery re-arms repair.
    """
    # Read-modify-write is safe because the connectivity loop is leader-owned (a
    # single serialized writer per device); the only overlap window is a brief
    # leader handoff, where a lost increment merely shifts one attempt and the
    # budget self-corrects on the next healthy probe (reset_repair_attempts).
    current = await state_store.get_value(db, REPAIR_ATTEMPTS_NAMESPACE, identity_value)
    used = int(current) if isinstance(current, int) else 0
    if used >= REPAIR_MAX_ATTEMPTS:
        return None
    attempt = used + 1
    await state_store.set_value(db, REPAIR_ATTEMPTS_NAMESPACE, identity_value, attempt)
    return attempt


async def reset_repair_attempts(db: AsyncSession, identity_value: str) -> None:
    await state_store.delete_value(db, REPAIR_ATTEMPTS_NAMESPACE, identity_value)


async def dispatch_recommended_action(
    device: Device,
    action: str,
    *,
    circuit_breaker: CircuitBreakerProtocol,
    pool: AgentHttpPool | None = None,
    extra_args: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Dispatch a manifest-declared lifecycle action for ``device`` via its agent.

    Mirrors the operator ``/reconnect`` route's dispatch so both paths are
    identical. Caller guarantees ``device.host`` / ``connection_target`` are present.
    """
    host = device.host
    if host is None or device.connection_target is None:
        raise ValueError("dispatch_recommended_action requires a host and connection_target")
    return await pack_device_lifecycle_action(
        host.ip,
        host.agent_port,
        device.connection_target,
        pack_id=device.pack_id,
        platform_id=device.platform_id,
        action=action,
        # Driver-agnostic: the generic target plus caller-supplied facts (e.g.
        # claimed_ports / live-session flags). Core never interprets them.
        args={"ip_address": device.ip_address, **(extra_args or {})},
        http_client_factory=httpx.AsyncClient,
        circuit_breaker=circuit_breaker,
        pool=pool,
    )
