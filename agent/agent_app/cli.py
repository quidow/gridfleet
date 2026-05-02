from __future__ import annotations

import argparse
from typing import TYPE_CHECKING

import uvicorn

from agent_app import __version__
from agent_app.config import agent_settings

if TYPE_CHECKING:
    from collections.abc import Sequence


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="gridfleet-agent")
    parser.add_argument("--version", action="store_true", help="Print the installed GridFleet agent version and exit.")
    subparsers = parser.add_subparsers(dest="command")

    serve = subparsers.add_parser("serve", help="Run the GridFleet agent API service.")
    serve.add_argument("--host", default="0.0.0.0", help="Bind host for the agent API service.")
    serve.add_argument(
        "--port",
        type=int,
        default=agent_settings.agent_port,
        help="Bind port for the agent API service.",
    )

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.version:
        print(f"gridfleet-agent {__version__}")
        return 0

    if args.command == "serve":
        uvicorn.run("agent_app.main:app", host=args.host, port=args.port)
        return 0

    parser.print_help()
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
