from __future__ import annotations

import argparse
import sys
from typing import TYPE_CHECKING

import uvicorn

from agent_app import __version__
from agent_app.config import agent_settings
from agent_app.installer.plan import InstallConfig, discover_tools, format_dry_run

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

    install = subparsers.add_parser("install", help="Install the GridFleet agent host service.")
    install.add_argument("--dry-run", action="store_true", help="Render the install plan without writing files.")
    install.add_argument("--manager-url", default="http://localhost:8000")
    install.add_argument("--port", type=int, default=agent_settings.agent_port)
    install.add_argument("--user", default=None)
    install.add_argument("--manager-auth-username", default=None)
    install.add_argument("--manager-auth-password", default=None)
    install.add_argument("--grid-hub-url", default="http://localhost:4444")
    install.add_argument("--grid-publish-url", default="tcp://localhost:4442")
    install.add_argument("--grid-subscribe-url", default="tcp://localhost:4443")
    install.add_argument("--grid-node-port-start", type=int, default=5555)
    install.add_argument("--selenium-version", default="4.41.0")
    install.add_argument("--enable-web-terminal", action="store_true")
    install.add_argument("--terminal-token", default=None)

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

    if args.command == "install":
        if not args.dry_run:
            print("ERROR: only install --dry-run is implemented in this release.", file=sys.stderr)
            return 2
        try:
            config = InstallConfig(
                user=args.user or InstallConfig().user,
                port=args.port,
                manager_url=args.manager_url,
                manager_auth_username=args.manager_auth_username,
                manager_auth_password=args.manager_auth_password,
                grid_hub_url=args.grid_hub_url,
                grid_publish_url=args.grid_publish_url,
                grid_subscribe_url=args.grid_subscribe_url,
                grid_node_port_start=args.grid_node_port_start,
                selenium_version=args.selenium_version,
                enable_web_terminal=args.enable_web_terminal,
                terminal_token=args.terminal_token,
            )
        except ValueError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2
        print(format_dry_run(config, discover_tools()))
        return 0

    parser.print_help()
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
