from __future__ import annotations

import argparse
import platform
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import uvicorn

from agent_app import __version__
from agent_app.config import agent_settings
from agent_app.installer.identity import resolve_operator_identity
from agent_app.installer.install import (
    LegacyInstallDetectedError,
    install_no_start,
    install_with_start,
)
from agent_app.installer.plan import (
    InstallConfig,
    default_install_config,
    discover_tools,
    format_dry_run,
    load_installed_config,
)
from agent_app.installer.status import collect_status, format_status
from agent_app.installer.uninstall import uninstall
from agent_app.installer.update import (
    UpdateDrainError,
    UpdateHealthError,
    UpdateRestartError,
    UpdateUpgradeError,
    UvNotFoundError,
    format_update_dry_run,
    update_agent,
)
from agent_app.installer.uv_runtime import discover_uv

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
        default=agent_settings.core.agent_port,
        help="Bind port for the agent API service.",
    )

    install = subparsers.add_parser("install", help="Install the GridFleet agent host service.")
    install.add_argument("--dry-run", action="store_true", help="Render the install plan without writing files.")
    install.add_argument("--no-start", action="store_true", help="Write files but do not enable or start the service.")
    install.add_argument("--start", action="store_true", help="Enable and start the service after writing files.")
    install.add_argument("--manager-url", default="http://localhost:8000")
    install.add_argument("--port", type=int, default=agent_settings.core.agent_port)
    install.add_argument("--manager-auth-username", default=None)
    install.add_argument("--manager-auth-password", default=None)
    install.add_argument("--api-auth-username", default=None)
    install.add_argument("--api-auth-password", default=None)
    install.add_argument("--grid-hub-url", default="http://localhost:4444")
    install.add_argument("--grid-publish-url", default="tcp://localhost:4442")
    install.add_argument("--grid-subscribe-url", default="tcp://localhost:4443")
    install.add_argument("--grid-node-port-start", type=int, default=5555)
    install.add_argument("--enable-web-terminal", action="store_true")
    install.add_argument("--terminal-token", default=None)

    subparsers.add_parser("status", help="Show local GridFleet agent installation and health status.")

    uninstall_parser = subparsers.add_parser("uninstall", help="Uninstall the GridFleet agent host service.")
    uninstall_parser.add_argument("--yes", action="store_true", help="Confirm removal of service and agent files.")
    uninstall_parser.add_argument("--keep-config", action="store_true", help="Leave config dir in place.")
    uninstall_parser.add_argument("--keep-agent-dir", action="store_true", help="Leave agent dir in place.")

    update_parser = subparsers.add_parser("update", help="Upgrade the installed GridFleet agent package and restart.")
    update_parser.add_argument("--to", default=None, help="Upgrade to an exact gridfleet-agent version.")
    update_parser.add_argument(
        "--dry-run", action="store_true", help="Render the update plan without changing anything."
    )
    update_parser.add_argument(
        "--uv-bin", default=None, help="Explicit path to uv binary to use for upgrade (advanced)."
    )

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        code = exc.code
        if isinstance(code, int):
            return code
        if code is None:
            return 0
        return 2

    if args.version:
        print(f"gridfleet-agent {__version__}")
        return 0

    if args.command == "serve":
        uvicorn.run("agent_app.main:app", host=args.host, port=args.port)
        return 0

    if args.command == "install":
        selected_modes = [args.dry_run, args.no_start, args.start]
        if sum(bool(mode) for mode in selected_modes) > 1:
            print("ERROR: choose only one of --dry-run, --no-start, or --start.", file=sys.stderr)
            return 2
        if not any(bool(mode) for mode in selected_modes):
            print(
                "ERROR: pass --dry-run to preview, --no-start to write files, or --start to start the service.",
                file=sys.stderr,
            )
            return 2
        try:
            operator = resolve_operator_identity()
        except ValueError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2
        defaults = default_install_config(platform.system())
        try:
            config = InstallConfig(
                agent_dir=defaults.agent_dir,
                config_dir=defaults.config_dir,
                user=operator.login,
                port=args.port,
                manager_url=args.manager_url,
                manager_auth_username=args.manager_auth_username,
                manager_auth_password=args.manager_auth_password,
                api_auth_username=args.api_auth_username,
                api_auth_password=args.api_auth_password,
                grid_hub_url=args.grid_hub_url,
                grid_publish_url=args.grid_publish_url,
                grid_subscribe_url=args.grid_subscribe_url,
                grid_node_port_start=args.grid_node_port_start,
                enable_web_terminal=args.enable_web_terminal,
                terminal_token=args.terminal_token,
            )
        except ValueError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2
        discovery = discover_tools()
        if args.dry_run:
            print(format_dry_run(config, discovery))
            return 0
        try:
            result = (
                install_with_start(config, discovery, operator=operator)
                if args.start
                else install_no_start(config, discovery, operator=operator)
            )
        except (RuntimeError, OSError) as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2
        if result.started:
            print("GridFleet agent service started.")
            if result.linger_warning:
                print(result.linger_warning, file=sys.stderr)
            if result.health is not None and not result.health.ok:
                print(f"ERROR: {result.health.message}", file=sys.stderr)
                return 1
            if result.registration is not None:
                stream = sys.stdout if result.registration.ok else sys.stderr
                prefix = "Registration" if result.registration.ok else "WARNING"
                print(f"{prefix}: {result.registration.message}", file=stream)
        else:
            print("GridFleet agent files installed. Service was not started.")
        return 0

    if args.command == "status":
        try:
            operator = resolve_operator_identity()
        except ValueError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2
        config = default_install_config(platform.system())
        uv_runtime = discover_uv(operator=operator, override=None)
        print(format_status(collect_status(config, operator=operator, uv_runtime=uv_runtime)))
        return 0

    if args.command == "uninstall":
        if not args.yes:
            print("ERROR: uninstall requires --yes.", file=sys.stderr)
            return 2
        try:
            operator = resolve_operator_identity()
        except ValueError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2
        try:
            uninstall(
                default_install_config(platform.system()),
                operator=operator,
                remove_agent_dir=not args.keep_agent_dir,
                remove_config_dir=not args.keep_config,
            )
        except LegacyInstallDetectedError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2
        except (RuntimeError, OSError) as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2
        print("GridFleet agent uninstalled.")
        return 0

    if args.command == "update":
        try:
            operator = resolve_operator_identity()
        except ValueError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2

        try:
            config = load_installed_config(default_install_config(platform.system()))
        except (ValueError, OSError) as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2

        override = Path(args.uv_bin) if args.uv_bin else None
        try:
            uv_runtime = discover_uv(operator=operator, override=override)
        except RuntimeError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1

        if args.dry_run:
            print(format_update_dry_run(config, operator=operator, uv_runtime=uv_runtime, to_version=args.to))
            return 0

        try:
            update_result = update_agent(
                config,
                operator=operator,
                uv_runtime=uv_runtime,
                to_version=args.to,
            )
        except (UpdateDrainError, UvNotFoundError, UpdateHealthError) as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
        except (UpdateUpgradeError, UpdateRestartError) as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2
        print(f"Drain: {update_result.drain.message}")
        print("GridFleet agent updated.")
        return 0

    parser.print_help()
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
