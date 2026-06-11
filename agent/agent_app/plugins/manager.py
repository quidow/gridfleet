import asyncio
import json
import logging
from typing import Any

from agent_app.appium.process import build_env
from agent_app.pack.runtime import plugin_install_command
from agent_app.tools.paths import find_appium as _find_appium

logger = logging.getLogger(__name__)


async def _run_appium(argv: list[str], *, timeout: float) -> tuple[int | None, str, str]:
    """Run an appium CLI command; returns (returncode, stdout, stderr) decoded."""
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=build_env(),
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    return proc.returncode, stdout.decode(errors="replace"), stderr.decode(errors="replace")


async def get_installed_plugins() -> list[dict[str, str]]:
    """List installed Appium plugins by running ``appium plugin list --installed --json``."""
    appium = _find_appium()
    try:
        returncode, stdout, stderr = await _run_appium([appium, "plugin", "list", "--installed", "--json"], timeout=30)
    except (FileNotFoundError, TimeoutError):
        logger.exception("Failed to list installed plugins")
        return []

    if returncode != 0:
        logger.error("appium plugin list failed: %s", stderr)
        return []

    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        logger.error("Failed to parse appium plugin list output")
        return []

    plugins_map: dict[str, Any] = data
    if isinstance(data.get("installed"), dict):
        plugins_map = data["installed"]

    results: list[dict[str, str]] = []
    for name, info in plugins_map.items():
        if isinstance(info, dict) and info.get("installed", True):
            results.append({"name": name, "version": info.get("version", "unknown")})
    return results


async def install_plugin(name: str, version: str, source: str, package: str | None = None) -> dict[str, Any]:
    """Install an Appium plugin. Handles official, npm, GitHub, git, and local sources."""
    appium = _find_appium()

    installed = await get_installed_plugins()
    for plugin in installed:
        if plugin["name"] == name:
            if plugin["version"] == version:
                return {"success": True, "message": f"{name}@{version} already installed"}
            logger.info("Uninstalling plugin %s@%s before installing %s", name, plugin["version"], version)
            await uninstall_plugin(name)
            break

    try:
        returncode, stdout, stderr = await _run_appium(
            plugin_install_command(appium, name, version, source, package), timeout=120
        )
    except FileNotFoundError:
        return {"success": False, "error": "appium binary not found"}
    except TimeoutError:
        return {"success": False, "error": "install timed out after 120s"}

    output = (stdout + stderr).strip()
    if returncode != 0:
        logger.error("Failed to install plugin %s: %s", name, output)
        return {"success": False, "error": output}

    logger.info("Installed plugin %s@%s", name, version)
    return {"success": True, "message": output}


async def uninstall_plugin(name: str) -> dict[str, Any]:
    appium = _find_appium()
    try:
        returncode, stdout, stderr = await _run_appium([appium, "plugin", "uninstall", name], timeout=30)
    except FileNotFoundError:
        return {"success": False, "error": "appium binary not found"}
    except TimeoutError:
        return {"success": False, "error": "uninstall timed out"}

    output = (stdout + stderr).strip()
    if returncode != 0:
        logger.error("Failed to uninstall plugin %s: %s", name, output)
        return {"success": False, "error": output}

    logger.info("Uninstalled plugin %s", name)
    return {"success": True, "message": output}


async def sync_plugins(plugin_configs: list[dict[str, Any]]) -> dict[str, Any]:
    installed = await get_installed_plugins()
    installed_map = {d["name"]: d["version"] for d in installed}
    required_names = {cfg["name"] for cfg in plugin_configs}

    result: dict[str, Any] = {"installed": [], "updated": [], "removed": [], "errors": {}}

    for cfg in plugin_configs:
        name = cfg["name"]
        version = cfg["version"]
        source = cfg.get("source") or name
        package = cfg.get("package")
        current_version = installed_map.get(name)

        if current_version == version:
            continue

        res = await install_plugin(name, version, source, package)
        if res.get("success"):
            if current_version is None:
                result["installed"].append(name)
            else:
                result["updated"].append(name)
        else:
            result["errors"][name] = res.get("error", "unknown error")

    for name in installed_map:
        if name not in required_names:
            res = await uninstall_plugin(name)
            if res.get("success"):
                result["removed"].append(name)
            else:
                result["errors"][name] = res.get("error", "unknown error")

    return result
