"""Manage host-level Appium tool versions."""

from __future__ import annotations

import asyncio
import glob
import logging
import os
import re
import shutil
from dataclasses import dataclass, field
from typing import Any

from agent_app.appium.process import _build_env
from agent_app.tool_paths import _parse_node_version

logger = logging.getLogger(__name__)

TOOL_VERSION_TIMEOUT_SEC = 10


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    output: str


@dataclass(frozen=True)
class NodeProvider:
    name: str
    node_path: str | None
    npm_path: str | None
    bin_paths: list[str] = field(default_factory=list)
    command_prefix: list[str] = field(default_factory=list)
    error: str | None = None

    def command(self, executable: str, *args: str) -> list[str]:
        if self.command_prefix:
            return [*self.command_prefix, executable, *args]
        if executable == "node" and self.node_path:
            return [self.node_path, *args]
        if executable == "npm" and self.npm_path:
            return [self.npm_path, *args]
        return [executable, *args]


def _provider_env(provider: NodeProvider | None = None) -> dict[str, str]:
    env = _build_env()
    if provider and provider.bin_paths:
        existing = env.get("PATH", "")
        paths = [path for path in provider.bin_paths if path and path not in existing.split(os.pathsep)]
        if paths:
            env["PATH"] = os.pathsep.join([*paths, existing])
    return env


def _prepend_process_path(paths: list[str]) -> None:
    existing = os.environ.get("PATH", "")
    existing_parts = existing.split(os.pathsep) if existing else []
    new_paths = [path for path in paths if path and path not in existing_parts]
    if new_paths:
        os.environ["PATH"] = os.pathsep.join([*new_paths, existing])


async def _run_command(
    cmd: list[str],
    *,
    timeout: int = TOOL_VERSION_TIMEOUT_SEC,
    env: dict[str, str] | None = None,
) -> CommandResult:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    output = stdout.decode(errors="replace").strip() or stderr.decode(errors="replace").strip()
    return CommandResult(proc.returncode or 0, output)


async def _run_optional(
    cmd: list[str],
    *,
    timeout: int = TOOL_VERSION_TIMEOUT_SEC,
    env: dict[str, str] | None = None,
) -> CommandResult | None:
    try:
        return await _run_command(cmd, timeout=timeout, env=env)
    except (FileNotFoundError, TimeoutError, OSError):
        return None


def _first_version(output: str) -> str | None:
    match = re.search(r"v?(\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?)", output)
    return match.group(1) if match else None


def _is_executable(path: str) -> bool:
    return os.path.isfile(path) and os.access(path, os.X_OK)


def _find_fnm_binary() -> str | None:
    found = shutil.which("fnm")
    if found:
        return found
    for path in [
        "/opt/homebrew/bin/fnm",
        "/usr/local/bin/fnm",
        os.path.expanduser("~/.local/bin/fnm"),
        os.path.expanduser("~/Library/Application Support/fnm/fnm"),
    ]:
        if _is_executable(path):
            return path
    return None


def _fnm_base_dirs() -> list[str]:
    dirs: list[str] = []
    if os.getenv("FNM_DIR"):
        dirs.append(os.path.expanduser(os.environ["FNM_DIR"]))
    if os.getenv("XDG_DATA_HOME"):
        dirs.append(os.path.join(os.path.expanduser(os.environ["XDG_DATA_HOME"]), "fnm"))
    dirs.extend(
        [
            os.path.expanduser("~/.local/share/fnm"),
            os.path.expanduser("~/Library/Application Support/fnm"),
        ]
    )
    unique: list[str] = []
    for path in dirs:
        if path not in unique:
            unique.append(path)
    return unique


def _fnm_default_bin_dirs() -> list[str]:
    bins: list[str] = []
    for base in _fnm_base_dirs():
        bins.append(os.path.join(base, "aliases", "default", "bin"))
    return [path for path in bins if os.path.isdir(path)]


async def _detect_fnm_provider() -> NodeProvider | None:
    fnm = _find_fnm_binary()
    if not fnm:
        return None

    node_result = await _run_optional([fnm, "exec", "--using", "default", "which", "node"])
    npm_result = await _run_optional([fnm, "exec", "--using", "default", "which", "npm"])
    version_result = await _run_optional([fnm, "exec", "--using", "default", "node", "--version"])
    if (
        node_result
        and node_result.returncode == 0
        and npm_result
        and npm_result.returncode == 0
        and version_result
        and version_result.returncode == 0
    ):
        node_path = node_result.output.splitlines()[0].strip()
        npm_path = npm_result.output.splitlines()[0].strip()
        bin_paths = [os.path.dirname(node_path), os.path.dirname(npm_path)]
        return NodeProvider(
            name="fnm",
            node_path=node_path,
            npm_path=npm_path,
            bin_paths=list(dict.fromkeys(bin_paths)),
            command_prefix=[fnm, "exec", "--using", "default"],
        )

    for bin_dir in _fnm_default_bin_dirs():
        node_path = os.path.join(bin_dir, "node")
        npm_path = os.path.join(bin_dir, "npm")
        if _is_executable(node_path) and _is_executable(npm_path):
            return NodeProvider(name="fnm", node_path=node_path, npm_path=npm_path, bin_paths=[bin_dir])

    return NodeProvider(name="fnm", node_path=None, npm_path=None, error="node_not_configured")


def _detect_nvm_provider() -> NodeProvider | None:
    candidates = [
        path for path in glob.glob(os.path.expanduser("~/.nvm/versions/node/*/bin/node")) if _is_executable(path)
    ]
    if not candidates:
        return None
    candidates.sort(key=_parse_node_version, reverse=True)
    node_path = candidates[0]
    bin_dir = os.path.dirname(node_path)
    npm_path = os.path.join(bin_dir, "npm")
    if not _is_executable(npm_path):
        return None
    return NodeProvider(name="nvm", node_path=node_path, npm_path=npm_path, bin_paths=[bin_dir])


def _detect_system_provider() -> NodeProvider | None:
    node_path = shutil.which("node")
    npm_path = shutil.which("npm")
    if node_path and npm_path:
        return NodeProvider(
            name="system",
            node_path=node_path,
            npm_path=npm_path,
            bin_paths=list(dict.fromkeys([os.path.dirname(node_path), os.path.dirname(npm_path)])),
        )
    for bin_dir in ["/usr/local/bin", "/usr/bin"]:
        node_candidate = os.path.join(bin_dir, "node")
        npm_candidate = os.path.join(bin_dir, "npm")
        if _is_executable(node_candidate) and _is_executable(npm_candidate):
            return NodeProvider(name="system", node_path=node_candidate, npm_path=npm_candidate, bin_paths=[bin_dir])
    return None


async def detect_node_provider() -> NodeProvider | None:
    fnm_provider = await _detect_fnm_provider()
    if fnm_provider is not None:
        return fnm_provider
    nvm_provider = _detect_nvm_provider()
    if nvm_provider is not None:
        return nvm_provider
    return _detect_system_provider()


async def _get_node_version(provider: NodeProvider | None) -> str | None:
    if provider is None or provider.error:
        return None
    result = await _run_optional(provider.command("node", "--version"), env=_provider_env(provider))
    if result is None or result.returncode != 0:
        return None
    return _first_version(result.output)


async def _get_go_ios_version(provider: NodeProvider | None = None) -> str | None:
    env = _provider_env(provider)
    result = await _run_optional(["ios", "--version"], env=env)
    if result is None or result.returncode != 0:
        if provider and provider.command_prefix:
            result = await _run_optional(provider.command("ios", "--version"), env=env)
        if result is None or result.returncode != 0:
            return None
    return _first_version(result.output) or result.output.splitlines()[0].strip()


async def get_tool_status() -> dict[str, Any]:
    provider = await detect_node_provider()
    if provider and provider.bin_paths:
        _prepend_process_path(provider.bin_paths)
    node_version = await _get_node_version(provider)
    go_ios_version = await _get_go_ios_version(provider)
    return {
        "node": node_version,
        "node_provider": provider.name if provider and not provider.error else None,
        "node_error": provider.error if provider and provider.error else None,
        "go_ios": go_ios_version,
    }
