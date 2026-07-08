import asyncio
import contextlib
from unittest.mock import AsyncMock, patch

from agent_app.host.capabilities import CapabilitiesCache
from agent_app.pack.adapter_registry import AdapterRegistry


def _cache(registry: AdapterRegistry | None = None) -> CapabilitiesCache:
    return CapabilitiesCache(adapter_registry=registry)


async def test_detect_capabilities_includes_adapter_tool_versions() -> None:
    class FakeAdapter:
        pack_id = "test-pack"
        pack_release = "1.0"

        def tool_versions(self) -> dict[str, str | None]:
            return {"adb": "1.0.41", "xcodebuild": "16.2"}

    registry = AdapterRegistry()
    registry.set("test-pack", "1.0", FakeAdapter())  # type: ignore[arg-type]

    capabilities = await _cache(registry).detect()
    assert capabilities["tools"]["adb"] == "1.0.41"
    assert capabilities["tools"]["xcodebuild"] == "16.2"


async def test_detect_capabilities_works_without_adapter_registry() -> None:
    capabilities = await _cache(None).detect()
    assert capabilities["tools"] == {}


async def test_node_desired_pull_capability_is_always_advertised() -> None:
    assert (await _cache(None).detect())["node_desired_pull"] == 1


async def test_detect_capabilities_merges_multiple_adapters() -> None:
    class AndroidAdapter:
        pack_id = "android"
        pack_release = "1.0"

        def tool_versions(self) -> dict[str, str | None]:
            return {"adb": "1.0.41"}

    class AppleAdapter:
        pack_id = "apple"
        pack_release = "1.0"

        def tool_versions(self) -> dict[str, str | None]:
            return {"xcodebuild": "16.2", "go_ios": "1.0.301"}

    registry = AdapterRegistry()
    registry.set("android", "1.0", AndroidAdapter())  # type: ignore[arg-type]
    registry.set("apple", "1.0", AppleAdapter())  # type: ignore[arg-type]

    capabilities = await _cache(registry).detect()
    assert capabilities["tools"] == {"adb": "1.0.41", "xcodebuild": "16.2", "go_ios": "1.0.301"}


async def test_detect_capabilities_skips_none_versions() -> None:
    class FakeAdapter:
        pack_id = "test"
        pack_release = "1.0"

        def tool_versions(self) -> dict[str, str | None]:
            return {"adb": None, "xcodebuild": "16.2"}

    registry = AdapterRegistry()
    registry.set("test", "1.0", FakeAdapter())  # type: ignore[arg-type]

    capabilities = await _cache(registry).detect()
    assert "adb" not in capabilities["tools"]
    assert capabilities["tools"]["xcodebuild"] == "16.2"


async def test_detect_capabilities_skips_adapters_without_tool_versions() -> None:
    class OldAdapter:
        pack_id = "old"
        pack_release = "1.0"

    registry = AdapterRegistry()
    registry.set("old", "1.0", OldAdapter())  # type: ignore[arg-type]

    capabilities = await _cache(registry).detect()
    assert capabilities["tools"] == {}


async def test_capabilities_snapshot_refreshes_only_when_missing_or_forced() -> None:
    cache = _cache()
    first_snapshot = {
        "platforms": ["roku"],
        "tools": {"adb": "1.0.41"},
        "missing_prerequisites": ["java"],
        "orchestration_contract_version": 3,
    }
    second_snapshot = {
        "platforms": ["roku"],
        "tools": {"adb": "1.0.42"},
        "missing_prerequisites": [],
        "orchestration_contract_version": 3,
    }
    default_snapshot = {
        "platforms": [],
        "tools": {},
        "missing_prerequisites": [],
        "orchestration_contract_version": 3,
        "node_desired_pull": 1,
    }

    with patch.object(
        cache,
        "detect",
        new_callable=AsyncMock,
        side_effect=[first_snapshot, second_snapshot],
    ) as detect:
        assert cache.get() == default_snapshot
        assert await cache.get_or_refresh() == {**first_snapshot, "orchestration_contract_version": 3}
        expected_second_call = {**first_snapshot, "orchestration_contract_version": 3, "node_desired_pull": 1}
        assert await cache.get_or_refresh() == expected_second_call
        assert await cache.get_or_refresh(force=True) == {**second_snapshot, "orchestration_contract_version": 3}

    assert detect.await_count == 2


async def test_capabilities_refresh_loop_sleeps_first_when_refresh_immediately_false() -> None:
    cache = _cache()
    hit_sleep = asyncio.Event()
    _orig_sleep = asyncio.sleep

    async def fake_sleep(d: float) -> None:
        hit_sleep.set()
        await _orig_sleep(0.001)

    with (
        patch.object(cache, "refresh", new_callable=AsyncMock) as refresh,
        patch("asyncio.sleep", side_effect=fake_sleep),
    ):
        task = asyncio.create_task(cache.run_refresh_loop(interval_sec=1, refresh_immediately=False))
        await asyncio.wait_for(hit_sleep.wait(), timeout=2.0)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=1.0)
    refresh.assert_not_awaited()


async def test_capabilities_refresh_loop_exception_logged() -> None:
    cache = _cache()
    hit_sleep = asyncio.Event()
    _orig_sleep = asyncio.sleep

    async def fake_sleep(d: float) -> None:
        hit_sleep.set()
        await _orig_sleep(0.001)

    with (
        patch.object(cache, "refresh", new_callable=AsyncMock, side_effect=RuntimeError("boom")) as refresh,
        patch("asyncio.sleep", side_effect=fake_sleep),
    ):
        task = asyncio.create_task(cache.run_refresh_loop(interval_sec=1, refresh_immediately=True))
        await asyncio.wait_for(hit_sleep.wait(), timeout=2.0)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=1.0)
    refresh.assert_awaited()
