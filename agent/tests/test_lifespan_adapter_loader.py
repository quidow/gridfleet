"""Verify _build_adapter_loader does not allocate a new AsyncClient per call."""

from __future__ import annotations

import hashlib
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import httpx2 as httpx
import pytest

from agent_app.http_client import close as close_shared_http_client
from agent_app.lifespan import _build_adapter_loader
from agent_app.pack.adapter_registry import AdapterRegistry
from agent_app.pack.worker_supervisor import WorkerSupervisor

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
    supervisor = AsyncMock(spec=WorkerSupervisor)
    supervisor.start.return_value = SimpleNamespace(pack_id="foo", release="1.0.0")
    loader = _build_adapter_loader("http://backend.test", registry, supervisor)

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body, request=request)

    shared_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    try:
        with (
            patch("httpx2.AsyncClient", side_effect=AssertionError("must not allocate AsyncClient")),
            patch("agent_app.lifespan.get_shared_http_client", return_value=shared_client),
            patch("agent_app.lifespan.prepare_adapter_site", new_callable=AsyncMock, return_value=tmp_path / "site"),
        ):
            await loader(FakePack(), FakeEnv())  # type: ignore[arg-type]
    finally:
        await shared_client.aclose()
        await close_shared_http_client()

    assert registry.get_current("foo") is not None


@pytest.mark.asyncio
async def test_adapter_loader_marks_wheelless_tarball_adapterless(tmp_path: Path) -> None:
    """A tarball without adapter/*.whl (Tier-1 manifest-only pack) must be
    recorded as adapterless — no worker started, no load failure."""
    import io
    import tarfile

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz"):
        pass
    body = buf.getvalue()
    sha = hashlib.sha256(body).hexdigest()

    class FakePack:
        id = "manifest-only"
        release = "1.0.0"
        tarball_sha256 = sha

    class FakeEnv:
        appium_home = str(tmp_path)

    registry = AdapterRegistry()
    supervisor = AsyncMock(spec=WorkerSupervisor)
    loader = _build_adapter_loader("http://backend.test", registry, supervisor)

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body, request=request)

    shared_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    try:
        with patch("agent_app.lifespan.get_shared_http_client", return_value=shared_client):
            await loader(FakePack(), FakeEnv())  # type: ignore[arg-type]
    finally:
        await shared_client.aclose()
        await close_shared_http_client()

    assert registry.is_adapterless("manifest-only", "1.0.0")
    assert registry.get_current("manifest-only") is None
    supervisor.start.assert_not_awaited()
