"""PATH-agnostic host tool resolution helpers."""

from __future__ import annotations

import glob
import logging
import os
import shutil

logger = logging.getLogger(__name__)


def _parse_node_version(path: str) -> tuple[int, ...]:
    """Extract a sortable version tuple from an nvm node path like .../v24.12.0/bin/appium."""
    try:
        parts = path.split(os.sep)
        for part in parts:
            if part.startswith("v") and "." in part:
                return tuple(int(x) for x in part.lstrip("v").split("."))
    except (ValueError, IndexError):
        logger.debug("Failed to parse node version from %r", path, exc_info=True)
    return (0,)


def find_appium() -> str:
    """Find the appium binary, checking PATH and common install locations."""
    found = shutil.which("appium")
    if found:
        return found
    fnm_dirs: list[str] = []
    if os.getenv("FNM_DIR"):
        fnm_dirs.append(os.path.expanduser(os.environ["FNM_DIR"]))
    if os.getenv("XDG_DATA_HOME"):
        fnm_dirs.append(os.path.join(os.path.expanduser(os.environ["XDG_DATA_HOME"]), "fnm"))
    fnm_dirs.extend(
        [
            os.path.expanduser("~/.local/share/fnm"),
            os.path.expanduser("~/Library/Application Support/fnm"),
        ]
    )
    fnm_candidates: list[str] = []
    for base in dict.fromkeys(fnm_dirs):
        fnm_candidates.append(os.path.join(base, "aliases", "default", "bin", "appium"))
    fnm_candidates = [p for p in fnm_candidates if os.access(p, os.X_OK)]
    if fnm_candidates:
        fnm_candidates.sort(key=_parse_node_version, reverse=True)
        return fnm_candidates[0]
    candidates = [
        p for p in glob.glob(os.path.expanduser("~/.nvm/versions/node/*/bin/appium")) if os.access(p, os.X_OK)
    ]
    if candidates:
        candidates.sort(key=_parse_node_version, reverse=True)
        return candidates[0]
    for path in ["/usr/local/bin/appium"]:
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return "appium"
