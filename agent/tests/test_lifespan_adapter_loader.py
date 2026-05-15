"""Verify _build_adapter_loader does not allocate a new AsyncClient per call."""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from agent_app.http_client import close as close_shared_http_client
from agent_app.lifespan import _build_adapter_loader
from agent_app.pack.adapter_registry import AdapterRegistry

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.asyncio
async def test_adapter_loader_reuses_shared_client(tmp_path: Path) -> None:
    body = b"pack-bytes"
    sha = hashlib.sha256(body).hexdigest()

    class FakePack:
        id = "foo"
        release = "1.0.0"
        tarball_sha256 = sha

    class FakeEnv:
        appium_home = str(tmp_path)

    registry = AdapterRegistry()
    loader = _build_adapter_loader("http://backend.test", registry)

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body, request=request)

    shared_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    try:
        with (
            patch("agent_app.lifespan.httpx.AsyncClient", side_effect=AssertionError("must not allocate AsyncClient")),
            patch("agent_app.lifespan.get_shared_http_client", return_value=shared_client),
            patch("agent_app.lifespan.load_adapter", new_callable=AsyncMock, return_value=object()),
        ):
            await loader(FakePack(), FakeEnv())  # type: ignore[arg-type]
    finally:
        await shared_client.aclose()
        await close_shared_http_client()

    assert registry.get_current("foo") is not None
