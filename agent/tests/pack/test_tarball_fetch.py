from __future__ import annotations

import hashlib
from pathlib import Path

import httpx
import pytest

from agent_app.pack import tarball_fetch
from agent_app.pack.tarball_fetch import TarballSha256MismatchError, download_and_verify


@pytest.mark.asyncio
async def test_download_writes_and_verifies(tmp_path: Path) -> None:
    payload = b"vendor-tarball"
    sha = hashlib.sha256(payload).hexdigest()

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=payload)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://backend") as client:
        path = await download_and_verify(
            client=client,
            pack_id="vendor-foo",
            release="0.1.0",
            expected_sha256=sha,
            dest_dir=tmp_path,
        )
    assert path.read_bytes() == payload


@pytest.mark.asyncio
async def test_download_rejects_sha_mismatch(tmp_path: Path) -> None:
    payload = b"vendor-tarball"

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=payload)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://backend") as client:
        with pytest.raises(TarballSha256MismatchError):
            await download_and_verify(
                client=client,
                pack_id="vendor-foo",
                release="0.1.0",
                expected_sha256="b" * 64,
                dest_dir=tmp_path,
            )


@pytest.mark.asyncio
async def test_download_and_verify_replaces_existing_file_atomically(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = b"new-tarball"
    expected = hashlib.sha256(body).hexdigest()
    dest = tmp_path / "packs"
    dest.mkdir()
    target = dest / "1.0.0.tar.gz"
    target.write_bytes(b"old")
    replaced: list[tuple[Path, Path]] = []
    original_replace = tarball_fetch.os.replace

    def replace(src: Path, dst: Path) -> None:
        replaced.append((Path(src), Path(dst)))
        original_replace(src, dst)

    monkeypatch.setattr(tarball_fetch.os, "replace", replace)

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/driver-packs/pack-a/releases/1.0.0/tarball"
        return httpx.Response(200, content=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://backend") as client:
        path = await tarball_fetch.download_and_verify(
            client=client,
            pack_id="pack-a",
            release="1.0.0",
            expected_sha256=expected,
            dest_dir=dest,
        )

    assert path == target
    assert target.read_bytes() == body
    assert replaced and replaced[0][1] == target
    assert replaced[0][0].name.startswith(".1.0.0.")
    assert list(dest.glob("*.tmp")) == []
