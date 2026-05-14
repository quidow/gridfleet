import asyncio
import json
import logging
from typing import Any

from agent_app.appium.process import _build_env
from agent_app.tools.paths import find_appium as _find_appium

logger = logging.getLogger(__name__)


async def get_installed_plugins() -> list[dict[str, str]]:
    """List installed Appium plugins by running ``appium plugin list --installed --json``."""
    appium = _find_appium()
    env = _build_env()
    try:
        proc = await asyncio.create_subprocess_exec(
            appium,
            "plugin",
            "list",
            "--installed",
            "--json",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
    except (FileNotFoundError, TimeoutError):
        logger.exception("Failed to list installed plugins")
        return []

    if proc.returncode != 0:
        logger.error("appium plugin list failed: %s", stderr.decode(errors="replace"))
        return []

    try:
        data = json.loads(stdout.decode())
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


def _versioned(value: str, version: str) -> str:
    return value if "@" in value.rsplit("/", 1)[-1] else f"{value}@{version}"


def _install_command(appium: str, name: str, version: str, source: str, package: str | None) -> list[str]:
    if source.startswith("npm:"):
        package_name = source.removeprefix("npm:")
        return [appium, "plugin", "install", _versioned(package_name, version), "--source=npm"]

    if source.startswith("github:"):
        install_spec = source.removeprefix("github:")
        cmd = [appium, "plugin", "install", install_spec, "--source=github"]
        if package:
            cmd.append(f"--package={package}")
        return cmd

    if source.startswith("git:"):
        install_spec = source.removeprefix("git:")
        cmd = [appium, "plugin", "install", install_spec, "--source=git"]
        if package:
            cmd.append(f"--package={package}")
        return cmd

    if source.startswith("local:"):
        install_spec = source.removeprefix("local:")
        cmd = [appium, "plugin", "install", install_spec, "--source=local"]
        if package:
            cmd.append(f"--package={package}")
        return cmd

    return [appium, "plugin", "install", _versioned(name, version)]


async def install_plugin(name: str, version: str, source: str, package: str | None = None) -> dict[str, Any]:
    """Install an Appium plugin. Handles official, npm, GitHub, git, and local sources."""
    appium = _find_appium()
    env = _build_env()

    installed = await get_installed_plugins()
    for plugin in installed:
        if plugin["name"] == name:
            if plugin["version"] == version:
                return {"success": True, "message": f"{name}@{version} already installed"}
            logger.info("Uninstalling plugin %s@%s before installing %s", name, plugin["version"], version)
            await uninstall_plugin(name)
            break

    cmd = _install_command(appium, name, version, source, package)
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
    except FileNotFoundError:
        return {"success": False, "error": "appium binary not found"}
    except TimeoutError:
        return {"success": False, "error": "install timed out after 120s"}

    output = stdout.decode(errors="replace") + stderr.decode(errors="replace")
    if proc.returncode != 0:
        logger.error("Failed to install plugin %s: %s", name, output)
        return {"success": False, "error": output.strip()}

    logger.info("Installed plugin %s@%s", name, version)
    return {"success": True, "message": output.strip()}


async def uninstall_plugin(name: str) -> dict[str, Any]:
    appium = _find_appium()
    env = _build_env()
    try:
        proc = await asyncio.create_subprocess_exec(
            appium,
            "plugin",
            "uninstall",
            name,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
    except FileNotFoundError:
        return {"success": False, "error": "appium binary not found"}
    except TimeoutError:
        return {"success": False, "error": "uninstall timed out"}

    output = stdout.decode(errors="replace") + stderr.decode(errors="replace")
    if proc.returncode != 0:
        logger.error("Failed to uninstall plugin %s: %s", name, output)
        return {"success": False, "error": output.strip()}

    logger.info("Uninstalled plugin %s", name)
    return {"success": True, "message": output.strip()}


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
