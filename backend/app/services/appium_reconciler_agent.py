"""Reconciler-owned raw Appium agent-operation helpers."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.services.agent_operations import appium_start as _appium_start
from app.services.agent_operations import appium_stop as _appium_stop

if TYPE_CHECKING:
    import httpx

    from app.agent_client import AgentClientFactory


async def appium_start(
    agent_base: str,
    *,
    host: str,
    agent_port: int,
    payload: dict[str, Any],
    http_client_factory: AgentClientFactory,
    timeout: float | int,
) -> httpx.Response:
    return await _appium_start(
        agent_base,
        host=host,
        agent_port=agent_port,
        payload=payload,
        http_client_factory=http_client_factory,
        timeout=timeout,
    )


async def appium_stop(
    agent_base: str,
    *,
    host: str,
    agent_port: int,
    port: int,
    http_client_factory: AgentClientFactory,
) -> httpx.Response:
    return await _appium_stop(
        agent_base,
        host=host,
        agent_port=agent_port,
        port=port,
        http_client_factory=http_client_factory,
    )
