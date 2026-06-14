"""_resolve_host_identity must receive a session_factory; no module-global fallback."""

from __future__ import annotations

import inspect

import pytest

from app.agent_comm.circuit_breaker import _resolve_host_identity


def test_session_factory_is_required_kwarg() -> None:
    sig = inspect.signature(_resolve_host_identity)
    param = sig.parameters["session_factory"]
    assert param.default is inspect.Parameter.empty
    assert param.kind is inspect.Parameter.KEYWORD_ONLY


@pytest.mark.asyncio
async def test_calling_without_session_factory_raises_type_error() -> None:
    with pytest.raises(TypeError):
        await _resolve_host_identity("1.2.3.4")  # type: ignore[call-arg]
