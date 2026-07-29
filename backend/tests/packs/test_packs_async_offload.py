from __future__ import annotations

import hashlib
import io
import tarfile
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

from app.packs.services import ingest as pack_ingest

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

pytestmark = pytest.mark.asyncio


def _tarball_with_manifest(manifest: bytes) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo("manifest.yaml")
        info.size = len(manifest)
        tar.addfile(info, io.BytesIO(manifest))
    return buf.getvalue()


def _spy_to_thread(calls: list[str]) -> Callable[..., object]:
    async def spy(fn: Callable[..., object], /, *args: object, **kwargs: object) -> object:
        calls.append(fn.__name__)
        return fn(*args, **kwargs)

    return spy


class _StubSessionFactory:
    """A ``begin()``-only factory. The phases are stubbed, so the session is inert."""

    @asynccontextmanager
    async def begin(self) -> AsyncIterator[object]:
        yield object()


async def test_ingest_parses_and_stores_the_artifact_off_the_event_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(pack_ingest.asyncio, "to_thread", _spy_to_thread(calls))

    manifest_text = b"schema_version: 1\nid: async-pack\nrelease: 1\nplatforms: []\n"
    data = _tarball_with_manifest(manifest_text)
    manifest = SimpleNamespace(
        id="async-pack",
        release="1",
        display_name="Async Pack",
        maintainer=None,
        license=None,
        derived_from=None,
        template_id=None,
        platforms=[],
        features={},
        insecure_features=[],
    )
    monkeypatch.setattr(pack_ingest, "load_manifest_yaml", lambda _text: manifest)
    monkeypatch.setattr(
        pack_ingest,
        "reserve_pack_upload",
        AsyncMock(
            return_value=pack_ingest.ArtifactReservation(artifact_path="/tmp/async-pack-1.tar.gz", needs_write=True)
        ),
    )
    activate = AsyncMock(return_value="pack-out")
    monkeypatch.setattr(pack_ingest, "activate_pack_upload", activate)

    class Storage:
        def store(self, *, pack_id: str, release: str, data: bytes) -> object:
            return SimpleNamespace(
                path=f"/tmp/{pack_id}-{release}.tar.gz", sha256=hashlib.sha256(data).hexdigest(), size=len(data)
            )

    result = await pack_ingest.ingest_pack_tarball(
        _StubSessionFactory(),  # type: ignore[arg-type]
        storage=Storage(),  # type: ignore[arg-type]
        username="admin",
        origin_filename="pack.tar.gz",
        data=data,
    )

    assert result == "pack-out"
    assert "parse_pack_tarball" in calls
    assert "_store_artifact" in calls
    activate.assert_awaited_once()
