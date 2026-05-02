from __future__ import annotations

import argparse
from unittest.mock import AsyncMock

import pytest

from app.seeding import __main__ as cli
from app.seeding.runner import SeedResult


def test_parse_args_uses_demo_defaults() -> None:
    args = cli._parse_args([])

    assert args.scenario == "full_demo"
    assert args.seed == 42
    assert args.wipe is True
    assert args.skip_telemetry is False


def test_resolve_db_url_prefers_explicit_arg(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GRIDFLEET_DATABASE_URL", "postgresql+asyncpg://localhost/env_demo")
    args = argparse.Namespace(db_url="postgresql+asyncpg://localhost/arg_demo")

    assert cli._resolve_db_url(args) == "postgresql+asyncpg://localhost/arg_demo"


def test_resolve_db_url_requires_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GRIDFLEET_SEED_DATABASE_URL", raising=False)
    monkeypatch.delenv("GRIDFLEET_DATABASE_URL", raising=False)

    with pytest.raises(SystemExit, match="no database URL"):
        cli._resolve_db_url(argparse.Namespace(db_url=None))


@pytest.mark.asyncio
async def test_main_async_rejects_non_demo_database(capsys: pytest.CaptureFixture[str]) -> None:
    result = await cli._main_async(["--db-url", "postgresql+asyncpg://localhost/gridfleet"])

    assert result == 2
    assert "database name must end with '_demo'" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_main_async_runs_selected_scenario(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    engine = AsyncMock()
    engine.dispose = AsyncMock()
    run_scenario = AsyncMock(return_value=SeedResult("minimal", {"hosts": 1, "devices": 0}, 1.25))

    monkeypatch.setattr(cli, "create_async_engine", lambda url, future=True: engine)
    monkeypatch.setattr(cli, "async_sessionmaker", lambda engine, **_kwargs: "factory")
    monkeypatch.setattr(cli, "run_scenario", run_scenario)

    result = await cli._main_async(
        [
            "--scenario",
            "minimal",
            "--db-url",
            "postgresql+asyncpg://localhost/gridfleet_demo",
            "--seed",
            "7",
            "--no-wipe",
            "--skip-telemetry",
        ]
    )

    assert result == 0
    run_scenario.assert_awaited_once_with(
        session_factory="factory",
        scenario="minimal",
        seed=7,
        wipe=False,
        skip_telemetry=True,
    )
    engine.dispose.assert_awaited_once()
    output = capsys.readouterr().out
    assert "seed complete: scenario=minimal elapsed=1.2s" in output
    assert "hosts" in output


def test_main_wraps_async_entrypoint(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_main_async(argv: list[str]) -> int:
        assert argv == ["--scenario", "minimal"]
        return 0

    monkeypatch.setattr(cli, "_main_async", fake_main_async)

    assert cli.main(["--scenario", "minimal"]) == 0
