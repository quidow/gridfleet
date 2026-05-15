from __future__ import annotations

import asyncio
import hashlib
import os
import re
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

    import httpx


class TarballSha256MismatchError(RuntimeError):
    pass


@dataclass(frozen=True)
class _FetchKey:
    target_path: str


@dataclass
class _FetchLockEntry:
    lock: asyncio.Lock
    users: int = 0


_UNSAFE_FILENAME_SEGMENT_RE = re.compile(r"[^a-zA-Z0-9._-]")
_fetch_locks: dict[_FetchKey, _FetchLockEntry] = {}
_fetch_lock_factory_lock = asyncio.Lock()


async def _get_or_create_fetch_lock(key: _FetchKey) -> _FetchLockEntry:
    async with _fetch_lock_factory_lock:
        entry = _fetch_locks.get(key)
        if entry is None:
            entry = _FetchLockEntry(lock=asyncio.Lock())
            _fetch_locks[key] = entry
        entry.users += 1
        return entry


async def _release_fetch_lock(key: _FetchKey, entry: _FetchLockEntry) -> None:
    async with _fetch_lock_factory_lock:
        entry.users -= 1
        if entry.users == 0 and _fetch_locks.get(key) is entry:
            _fetch_locks.pop(key, None)


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
    base_url: str | None = None,
    auth: httpx.Auth | None = None,
    timeout: float | None = None,
) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = _tarball_target(dest_dir, pack_id, release)

    key = _FetchKey(target_path=str(target.resolve()))
    fetch_entry = await _get_or_create_fetch_lock(key)
    try:
        async with fetch_entry.lock:
            # Re-check after acquiring the lock: a prior holder may have already
            # fetched and verified this target while this caller waited.
            if _existing_target_matches(target, expected_sha256):
                return target

            path = f"/api/driver-packs/{pack_id}/releases/{release}/tarball"
            url = f"{base_url.rstrip('/')}{path}" if base_url else path
            request_kwargs: dict[str, Any] = {}
            if auth is not None:
                request_kwargs["auth"] = auth
            if timeout is not None:
                request_kwargs["timeout"] = timeout

            response = await client.get(url, **request_kwargs)
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
    finally:
        await _release_fetch_lock(key, fetch_entry)
