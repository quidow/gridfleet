from __future__ import annotations

import hashlib
import io
import tarfile
from contextlib import contextmanager
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

from app.packs.routers import authoring as pack_authoring
from app.packs.services import export as pack_export
from app.packs.services import ingest as pack_ingest

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

pytestmark = pytest.mark.asyncio


class _ScalarResult:
    def __init__(self, *, one_or_none: object = None, one: object = None) -> None:
        self._one_or_none = one_or_none
        self._one = one

    def scalar_one_or_none(self) -> object:
        return self._one_or_none

    def scalar_one(self) -> object:
        return self._one


class _SequenceSession:
    def __init__(self, *results: _ScalarResult) -> None:
        self._results = list(results)
        self.added: list[object] = []
        self.flushed = 0

    async def execute(self, _statement: object) -> _ScalarResult:
        return self._results.pop(0)

    def add(self, obj: object) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        self.flushed += 1


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


async def test_fork_builds_tarball_off_event_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(pack_authoring.asyncio, "to_thread", _spy_to_thread(calls))

    release = SimpleNamespace(release="1.0.0", manifest_json={"id": "source", "release": "1.0.0", "platforms": []})
    source = SimpleNamespace(id="source", current_release="1.0.0", releases=[release])
    session = AsyncMock()
    session.get.return_value = None
    session.execute.return_value = _ScalarResult(one_or_none=source)
    monkeypatch.setattr(pack_authoring, "selected_release", lambda _releases, _current: release)
    monkeypatch.setattr(pack_authoring, "PackStorageService", lambda _root: object())

    async def fake_ingest_pack_tarball(*_args: object, data: bytes, **_kwargs: object) -> object:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            assert tar.getmember("manifest.yaml").isfile()
        return SimpleNamespace(id="local/fork")

    monkeypatch.setattr(pack_authoring, "ingest_pack_tarball", fake_ingest_pack_tarball)
    monkeypatch.setattr(pack_authoring, "build_pack_out", lambda pack: {"id": pack.id})

    body = pack_authoring.ForkPackBody(new_pack_id="local/fork")

    assert await pack_authoring.fork("source", body, _username="admin", session=session) == {"id": "local/fork"}
    assert "_build_fork_tarball" in calls
    session.commit.assert_awaited_once()


async def test_export_reads_stored_artifact_off_event_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(pack_export.asyncio, "to_thread", _spy_to_thread(calls))

    row = SimpleNamespace(artifact_path="/tmp/artifact.tar.gz")
    session = _SequenceSession(_ScalarResult(one_or_none=row))

    class Storage:
        @contextmanager
        def open(self, _path: str) -> Iterator[io.BytesIO]:
            yield io.BytesIO(b"stored")

    data, sha = await pack_export.export_pack(session, Storage(), "pack", "1")

    assert data == b"stored"
    assert sha == hashlib.sha256(b"stored").hexdigest()
    assert calls == ["_read_artifact"]


async def test_export_synthesises_tarball_off_event_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(pack_export.asyncio, "to_thread", _spy_to_thread(calls))

    row = SimpleNamespace(artifact_path=None, manifest_json={"id": "pack", "release": "1", "platforms": []})
    session = _SequenceSession(_ScalarResult(one_or_none=row))

    data, sha = await pack_export.export_pack(session, object(), "pack", "1")

    assert sha == hashlib.sha256(data).hexdigest()
    assert calls == ["_synthesise_tarball"]


async def test_ingest_validates_tarball_and_stores_artifact_off_event_loop(monkeypatch: pytest.MonkeyPatch) -> None:
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
    )
    monkeypatch.setattr(pack_ingest, "load_manifest_yaml", lambda _text: manifest)
    record_upload = AsyncMock()
    monkeypatch.setattr(pack_ingest, "record_pack_upload", record_upload)

    class Storage:
        def store(self, *, pack_id: str, release: str, data: bytes) -> object:
            return SimpleNamespace(path=f"/tmp/{pack_id}-{release}.tar.gz", sha256=hashlib.sha256(data).hexdigest())

    returned_pack = SimpleNamespace(id="async-pack")
    session = _SequenceSession(_ScalarResult(one_or_none=None), _ScalarResult(one=returned_pack))

    assert (
        await pack_ingest.ingest_pack_tarball(
            session,
            storage=Storage(),
            username="admin",
            origin_filename="pack.tar.gz",
            data=data,
        )
    ) is returned_pack
    assert "_extract_manifest_text" in calls
    assert "_store_artifact" in calls
    record_upload.assert_awaited_once()
