from __future__ import annotations

import asyncio
import hashlib
import os
import re
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    import httpx


class TarballSha256MismatchError(RuntimeError):
    pass


@dataclass(frozen=True)
class _FetchKey:
    target_path: str


_UNSAFE_FILENAME_SEGMENT_RE = re.compile(r"[^a-zA-Z0-9._-]")
_fetch_locks: dict[_FetchKey, asyncio.Lock] = {}
_fetch_lock_factory_lock = asyncio.Lock()


async def _get_or_create_fetch_lock(key: _FetchKey) -> asyncio.Lock:
    async with _fetch_lock_factory_lock:
        lock = _fetch_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _fetch_locks[key] = lock
        return lock


def _safe_filename_segment(value: str) -> str:
    segment = _UNSAFE_FILENAME_SEGMENT_RE.sub("_", value).strip("._")
    return segment or "driver-pack"


def _tarball_target(dest_dir: Path, pack_id: str, release: str) -> Path:
    identity_hash = hashlib.sha256(f"{pack_id}\0{release}".encode()).hexdigest()[:12]
    return dest_dir / f"{_safe_filename_segment(pack_id)}-{_safe_filename_segment(release)}-{identity_hash}.tar.gz"


def _existing_target_matches(target: Path, expected_sha256: str) -> bool:
    if not target.exists():
        return False
    actual = hashlib.sha256(target.read_bytes()).hexdigest()
    return actual == expected_sha256


async def download_and_verify(
    *,
    client: httpx.AsyncClient,
    pack_id: str,
    release: str,
    expected_sha256: str,
    dest_dir: Path,
) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = _tarball_target(dest_dir, pack_id, release)

    key = _FetchKey(target_path=str(target.resolve()))
    fetch_lock = await _get_or_create_fetch_lock(key)
    async with fetch_lock:
        # Re-check after acquiring the lock: a prior holder may have already
        # fetched and verified this target while this caller waited.
        if _existing_target_matches(target, expected_sha256):
            return target

        response = await client.get(f"/api/driver-packs/{pack_id}/releases/{release}/tarball")
        response.raise_for_status()
        body = response.content
        actual = hashlib.sha256(body).hexdigest()
        if actual != expected_sha256:
            raise TarballSha256MismatchError(
                f"tarball for {pack_id}@{release} sha mismatch: got {actual} expected {expected_sha256}"
            )
        tmp = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        try:
            tmp.write_bytes(body)
            os.replace(tmp, target)
        finally:
            if tmp.exists():
                tmp.unlink()
        return target
