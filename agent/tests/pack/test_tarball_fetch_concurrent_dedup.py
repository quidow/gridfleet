from __future__ import annotations

import asyncio
import hashlib
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent_app.pack.tarball_fetch import download_and_verify

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.asyncio


async def test_concurrent_download_for_same_pack_release_runs_one_http_fetch(
    tmp_path: Path,
) -> None:
    """Two concurrent download_and_verify calls for the same (pack_id, release)
    must dedup to a single HTTP fetch while the first GET is still in flight.
    Without the fix, both calls enter client.get. With the fix, the second
    caller waits on the per-target asyncio.Lock and returns the already-fetched
    target path.
    """

    body = b"test tarball body" * 1000
    expected_sha = hashlib.sha256(body).hexdigest()
    first_get_started = asyncio.Event()
    first_get_can_finish = asyncio.Event()
    get_calls = 0

    response = MagicMock()
    response.content = body
    response.raise_for_status = MagicMock(return_value=None)

    async def get_tarball(url: str) -> MagicMock:
        nonlocal get_calls
        assert url == "/api/driver-packs/appium-uiautomator2/releases/1.0.0/tarball"
        get_calls += 1
        if get_calls == 1:
            first_get_started.set()
            await first_get_can_finish.wait()
        return response

    client = MagicMock()
    client.get = AsyncMock(side_effect=get_tarball)

    async def fetch() -> Path:
        return await download_and_verify(
            client=client,
            pack_id="appium-uiautomator2",
            release="1.0.0",
            expected_sha256=expected_sha,
            dest_dir=tmp_path,
        )

    first = asyncio.create_task(fetch())
    await asyncio.wait_for(first_get_started.wait(), timeout=1.0)

    second = asyncio.create_task(fetch())
    await asyncio.sleep(0)

    get_calls_while_first_in_flight = get_calls
    first_get_can_finish.set()
    paths = await asyncio.gather(first, second)

    assert get_calls_while_first_in_flight == 1, (
        f"download_and_verify issued a second GET while the first GET was still in flight; "
        f"client.get was called {get_calls_while_first_in_flight} times for the same pack/release/target."
    )
    assert paths[0] == paths[1]
    assert paths[0].parent == tmp_path
    assert paths[0].name.startswith("appium-uiautomator2-1.0.0-")
    assert paths[0].name.endswith(".tar.gz")
    assert paths[0].exists()
    assert paths[0].read_bytes() == body
    assert get_calls == 1, (
        f"download_and_verify did not deduplicate concurrent fetches; "
        f"client.get was called {get_calls} times for the same target. "
        f"Expected 1 call after the per-target lock + cache "
        f"re-check pattern (mirrors adapter_loader from commit 4bea799)."
    )


async def test_same_release_for_different_pack_ids_uses_distinct_target_files(
    tmp_path: Path,
) -> None:
    """The local tarball cache path must include pack identity, not just release."""

    bodies = {
        "pack-a": b"pack-a-tarball",
        "pack-b": b"pack-b-tarball",
    }
    shas = {pack_id: hashlib.sha256(body).hexdigest() for pack_id, body in bodies.items()}

    async def get_tarball(url: str) -> MagicMock:
        if "/pack-a/" in url:
            body = bodies["pack-a"]
        elif "/pack-b/" in url:
            body = bodies["pack-b"]
        else:
            raise AssertionError(f"unexpected tarball URL: {url}")
        response = MagicMock()
        response.content = body
        response.raise_for_status = MagicMock(return_value=None)
        return response

    client = MagicMock()
    client.get = AsyncMock(side_effect=get_tarball)

    path_a = await download_and_verify(
        client=client,
        pack_id="pack-a",
        release="1.0.0",
        expected_sha256=shas["pack-a"],
        dest_dir=tmp_path,
    )
    path_b = await download_and_verify(
        client=client,
        pack_id="pack-b",
        release="1.0.0",
        expected_sha256=shas["pack-b"],
        dest_dir=tmp_path,
    )

    assert path_a != path_b
    assert path_a.name.startswith("pack-a-1.0.0-")
    assert path_b.name.startswith("pack-b-1.0.0-")
    assert path_a.read_bytes() == bodies["pack-a"]
    assert path_b.read_bytes() == bodies["pack-b"]
