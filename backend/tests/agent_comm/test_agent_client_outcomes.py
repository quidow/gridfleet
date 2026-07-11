from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx2 as httpx
import pytest

from app.agent_comm.client import request as agent_request
from app.core.errors import AgentUnreachableError, CircuitOpenError


def _factory_raising(exc: BaseException) -> MagicMock:
    """Return a synchronous factory callable that yields an async context manager whose get() raises exc."""
    client = AsyncMock()
    client.get = AsyncMock(side_effect=exc)
    ctx_manager = AsyncMock()
    ctx_manager.__aenter__.return_value = client
    ctx_manager.__aexit__.return_value = False
    factory = MagicMock(return_value=ctx_manager)
    return factory


def _noop_breaker() -> AsyncMock:
    breaker = AsyncMock()
    breaker.before_request = AsyncMock(return_value=None)
    return breaker


def _pooled_client(method: str, side_effect: list[object]) -> MagicMock:
    """A pooled client whose `method` returns/raises each item in side_effect in turn."""
    client = MagicMock()
    setattr(client, method, AsyncMock(side_effect=side_effect))
    return client


@pytest.mark.asyncio
async def test_pooled_get_retries_once_on_stale_keepalive_disconnect() -> None:
    # The agent's uvicorn keep-alive (default 5s) is shorter than our pool idle
    # (POOL_KEEPALIVE_EXPIRY_SEC, 60s), so the pool can hand back a
    # connection the server already closed -> RemoteProtocolError before the
    # request is processed. One retry on a fresh connection must recover it.
    ok = httpx.Response(status_code=200)
    client = _pooled_client(
        "get",
        [httpx.RemoteProtocolError("Server disconnected without sending a response"), ok],
    )
    breaker = _noop_breaker()
    response = await agent_request(
        "GET",
        "http://1.2.3.4:5100/agent/health",
        endpoint="agent_health",
        host="1.2.3.4",
        client=client,
        client_mode="pooled",
        circuit_breaker=breaker,
    )
    assert response is ok
    assert client.get.await_count == 2
    breaker.record_success.assert_awaited_once()
    breaker.record_failure.assert_not_awaited()


@pytest.mark.asyncio
async def test_pooled_post_not_retried_on_disconnect() -> None:
    # POST is not idempotent; a disconnect must NOT trigger a silent retry.
    client = _pooled_client(
        "post",
        [httpx.RemoteProtocolError("Server disconnected without sending a response")],
    )
    breaker = _noop_breaker()
    with pytest.raises(AgentUnreachableError):
        await agent_request(
            "POST",
            "http://1.2.3.4:5100/agent/pack/devices/normalize",
            endpoint="pack_device_normalize",
            host="1.2.3.4",
            client=client,
            client_mode="pooled",
            circuit_breaker=breaker,
        )
    assert client.post.await_count == 1
    breaker.record_failure.assert_awaited_once()


@pytest.mark.asyncio
async def test_pooled_get_retry_exhaustion_raises() -> None:
    client = _pooled_client(
        "get",
        [
            httpx.RemoteProtocolError("Server disconnected without sending a response"),
            httpx.RemoteProtocolError("Server disconnected without sending a response"),
        ],
    )
    breaker = _noop_breaker()
    with pytest.raises(AgentUnreachableError):
        await agent_request(
            "GET",
            "http://1.2.3.4:5100/agent/health",
            endpoint="agent_health",
            host="1.2.3.4",
            client=client,
            client_mode="pooled",
            circuit_breaker=breaker,
        )
    assert client.get.await_count == 2
    breaker.record_failure.assert_awaited_once()


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
            circuit_breaker=_noop_breaker(),
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
            circuit_breaker=_noop_breaker(),
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
            circuit_breaker=_noop_breaker(),
        )
    assert caught.value.transport_outcome == "dns_error"


@pytest.mark.asyncio
async def test_dns_error_musl_pattern_classified_as_dns() -> None:
    factory = _factory_raising(
        httpx.ConnectError("[Errno -2] Name or service not known"),
    )
    with pytest.raises(AgentUnreachableError) as caught:
        await agent_request(
            "GET",
            "http://no-such.invalid:5100/agent/health",
            endpoint="agent_health",
            host="no-such.invalid",
            client_factory=factory,
            client_mode="fresh",
            circuit_breaker=_noop_breaker(),
        )
    assert caught.value.transport_outcome == "dns_error"


@pytest.mark.asyncio
async def test_circuit_open_path_unaffected() -> None:
    mock_breaker = AsyncMock()
    mock_breaker.before_request = AsyncMock(return_value=10.0)
    with pytest.raises(CircuitOpenError):
        await agent_request(
            "GET",
            "http://1.2.3.4:5100/agent/health",
            endpoint="agent_health",
            host="1.2.3.4",
            client_factory=MagicMock(),
            client_mode="pooled",
            circuit_breaker=mock_breaker,
        )


@pytest.mark.asyncio
async def test_empty_message_readtimeout_records_breaker_error_with_class_name() -> None:
    """Regression: httpx.ReadTimeout('') used to flow into record_failure as error=''.
    The empty string made `agent_circuit_open` events impossible to triage.
    The error string passed to the breaker MUST identify the exception class even when str(exc) is empty.
    """
    factory = _factory_raising(httpx.ReadTimeout(""))
    mock_breaker = AsyncMock()
    mock_breaker.before_request = AsyncMock(return_value=None)
    mock_breaker.record_failure = AsyncMock()
    with pytest.raises(AgentUnreachableError):
        await agent_request(
            "GET",
            "http://1.2.3.4:5100/agent/pack/devices/x/properties",
            endpoint="pack_device_properties",
            host="1.2.3.4",
            client_factory=factory,
            client_mode="fresh",
            circuit_breaker=mock_breaker,
        )
    mock_breaker.record_failure.assert_awaited_once()
    args = mock_breaker.record_failure.await_args.args
    kwargs = mock_breaker.record_failure.await_args.kwargs
    assert args == ("1.2.3.4",)
    assert kwargs["error"] == "ReadTimeout"


@pytest.mark.asyncio
async def test_nonempty_readtimeout_records_breaker_error_with_class_and_message() -> None:
    """When str(exc) is non-empty, the breaker error string must include both the class and the message."""
    factory = _factory_raising(httpx.ReadTimeout("read timeout after 10s"))
    mock_breaker = AsyncMock()
    mock_breaker.before_request = AsyncMock(return_value=None)
    mock_breaker.record_failure = AsyncMock()
    with pytest.raises(AgentUnreachableError):
        await agent_request(
            "GET",
            "http://1.2.3.4:5100/agent/pack/devices/x/properties",
            endpoint="pack_device_properties",
            host="1.2.3.4",
            client_factory=factory,
            client_mode="fresh",
            circuit_breaker=mock_breaker,
        )
    assert mock_breaker.record_failure.await_args.args == ("1.2.3.4",)
    assert mock_breaker.record_failure.await_args.kwargs["error"] == "ReadTimeout: read timeout after 10s"
