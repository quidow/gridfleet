import pytest

from agent_app.pack.version_catalog import NpmVersionCatalog, parse_npm_versions


def test_parse_npm_versions_filters_to_strings() -> None:
    assert parse_npm_versions('["2.11.5", 12, "2.11.9"]') == ["2.11.5", "2.11.9"]


def test_parse_npm_versions_rejects_object_payload() -> None:
    with pytest.raises(ValueError, match="expected npm versions array"):
        parse_npm_versions('{"latest": "2.11.9"}')


@pytest.mark.asyncio
async def test_npm_version_catalog_caches_successful_lookup() -> None:
    calls: list[list[str]] = []

    async def runner(cmd: list[str], timeout: float) -> str:
        calls.append(cmd)
        return '["2.11.5", "2.11.9"]'

    catalog = NpmVersionCatalog(runner=runner, ttl_seconds=60)

    assert await catalog.versions("appium") == ["2.11.5", "2.11.9"]
    assert await catalog.versions("appium") == ["2.11.5", "2.11.9"]
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_npm_version_catalog_returns_empty_on_timeout() -> None:
    async def runner(cmd: list[str], timeout: float) -> str:
        raise TimeoutError

    catalog = NpmVersionCatalog(runner=runner, ttl_seconds=60)

    assert await catalog.versions("appium") == []
