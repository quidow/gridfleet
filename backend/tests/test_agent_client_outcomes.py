from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.agent_client import request as agent_request
from app.errors import AgentUnreachableError, CircuitOpenError


def _factory_raising(exc: BaseException) -> MagicMock:
    """Return a synchronous factory callable that yields an async context manager whose get() raises exc."""
    client = AsyncMock()
    client.get = AsyncMock(side_effect=exc)
    ctx_manager = AsyncMock()
    ctx_manager.__aenter__.return_value = client
    ctx_manager.__aexit__.return_value = False
    factory = MagicMock(return_value=ctx_manager)
    return factory


@pytest.mark.asyncio
async def test_timeout_sets_transport_outcome_timeout() -> None:
    factory = _factory_raising(httpx.ReadTimeout("boom"))
    with pytest.raises(AgentUnreachableError) as caught:
        await agent_request(
            "GET",
            "http://1.2.3.4:5100/agent/health",
            endpoint="agent_health",
            host="1.2.3.4",
            client_factory=factory,
            client_mode="fresh",
        )
    assert caught.value.transport_outcome == "timeout"
    assert caught.value.error_category == "ReadTimeout"


@pytest.mark.asyncio
async def test_connect_error_sets_transport_outcome_connect_error() -> None:
    factory = _factory_raising(httpx.ConnectError("nope"))
    with pytest.raises(AgentUnreachableError) as caught:
        await agent_request(
            "GET",
            "http://1.2.3.4:5100/agent/health",
            endpoint="agent_health",
            host="1.2.3.4",
            client_factory=factory,
            client_mode="pooled",
        )
    assert caught.value.transport_outcome == "connect_error"
    assert caught.value.error_category == "ConnectError"


@pytest.mark.asyncio
async def test_dns_error_classified_as_dns() -> None:
    factory = _factory_raising(
        httpx.ConnectError("[Errno -3] Temporary failure in name resolution"),
    )
    with pytest.raises(AgentUnreachableError) as caught:
        await agent_request(
            "GET",
            "http://no-such.invalid:5100/agent/health",
            endpoint="agent_health",
            host="no-such.invalid",
            client_factory=factory,
            client_mode="fresh",
        )
    assert caught.value.transport_outcome == "dns_error"


@pytest.mark.asyncio
async def test_circuit_open_path_unaffected() -> None:
    with (
        patch("app.agent_client.agent_circuit_breaker.before_request", new=AsyncMock(return_value=10.0)),
        pytest.raises(CircuitOpenError),
    ):
        await agent_request(
            "GET",
            "http://1.2.3.4:5100/agent/health",
            endpoint="agent_health",
            host="1.2.3.4",
            client_factory=MagicMock(),
            client_mode="pooled",
        )
