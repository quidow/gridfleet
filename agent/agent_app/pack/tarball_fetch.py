from __future__ import annotations

import hashlib
import os
import uuid
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    import httpx


class TarballSha256MismatchError(RuntimeError):
    pass


async def download_and_verify(
    *,
    client: httpx.AsyncClient,
    pack_id: str,
    release: str,
    expected_sha256: str,
    dest_dir: Path,
) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = dest_dir / f"{release}.tar.gz"
    response = await client.get(f"/api/driver-packs/{pack_id}/releases/{release}/tarball")
    response.raise_for_status()
    body = response.content
    actual = hashlib.sha256(body).hexdigest()
    if actual != expected_sha256:
        raise TarballSha256MismatchError(
            f"tarball for {pack_id}@{release} sha mismatch: got {actual} expected {expected_sha256}"
        )
    tmp = dest_dir / f".{release}.{uuid.uuid4().hex}.tmp"
    try:
        tmp.write_bytes(body)
        os.replace(tmp, target)
    finally:
        if tmp.exists():
            tmp.unlink()
    return target
