"""CLI: python -m app.seeding --scenario full_demo --db-url ... --seed 42"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.seeding.runner import (
    DatabaseGuardError,
    SeedResult,
    ensure_demo_database_url,
    run_scenario,
)

_DEFAULT_SEED = 42


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser("app.seeding", description="Seed the demo database.")
    p.add_argument("--scenario", default="full_demo", choices=["full_demo", "minimal", "chaos"])
    p.add_argument("--db-url", default=None, help="override GRIDFLEET_SEED_DATABASE_URL / GRIDFLEET_DATABASE_URL")
    p.add_argument("--seed", type=int, default=_DEFAULT_SEED)
    p.add_argument("--wipe", dest="wipe", action="store_true", default=True)
    p.add_argument("--no-wipe", dest="wipe", action="store_false")
    p.add_argument("--skip-telemetry", action="store_true", default=False)
    return p.parse_args(argv)


def _resolve_db_url(args: argparse.Namespace) -> str:
    url = args.db_url or os.getenv("GRIDFLEET_SEED_DATABASE_URL") or os.getenv("GRIDFLEET_DATABASE_URL")
    if not url:
        raise SystemExit("no database URL: pass --db-url or set GRIDFLEET_SEED_DATABASE_URL / GRIDFLEET_DATABASE_URL")
    return url


async def _main_async(argv: list[str]) -> int:
    args = _parse_args(argv)
    url = _resolve_db_url(args)
    allow_any = os.getenv("GRIDFLEET_SEED_ALLOW_ANY_DB") == "1"
    try:
        ensure_demo_database_url(url, allow_any_db=allow_any)
    except DatabaseGuardError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    engine = create_async_engine(url, future=True)
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        result: SeedResult = await run_scenario(
            session_factory=factory,
            scenario=args.scenario,
            seed=args.seed,
            wipe=args.wipe,
            skip_telemetry=args.skip_telemetry,
        )
    finally:
        await engine.dispose()

    print(f"seed complete: scenario={result.scenario} elapsed={result.elapsed_seconds:.1f}s")
    for table, count in result.row_counts.items():
        if count > 0:
            print(f"  {table:<40} {count}")
    return 0


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(_main_async(argv if argv is not None else sys.argv[1:]))


if __name__ == "__main__":
    raise SystemExit(main())
