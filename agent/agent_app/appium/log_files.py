"""Per-port Appium log files.

Appium subprocess stdout/stderr are redirected straight to these files at
spawn time (kernel-level fd redirect — zero per-line work in the agent).
The agent only touches them on demand: ``/agent/appium/{port}/logs``
tail-reads the file, and a periodic maintenance pass copy-truncates files
that exceed the size cap. Files are opened with ``O_APPEND`` so external
truncation never leaves the child writing at a stale offset.
"""

import contextlib
import os
from pathlib import Path
from typing import BinaryIO

from agent_app.config import agent_settings

MAX_LOG_BYTES = 10 * 1024 * 1024
TAIL_READ_BYTES = 1024 * 1024
LOG_MAINTENANCE_INTERVAL_SEC = 60.0


def appium_log_dir() -> Path:
    return Path(agent_settings.runtime.runtime_root) / "appium-logs"


def appium_log_path(port: int) -> Path:
    return appium_log_dir() / f"appium-{port}.log"


LOG_FILES_PER_PORT = 2


def spawn_log_path(port: int, spawn_id: str) -> Path:
    """The log file for one spawn. Two racing processes on a port get two files."""
    return appium_log_dir() / f"appium-{port}-{spawn_id}.log"


def open_spawn_log_file(port: int, spawn_id: str) -> BinaryIO:
    appium_log_dir().mkdir(parents=True, exist_ok=True)
    return spawn_log_path(port, spawn_id).open("ab")


def _matches_port(path: Path, port: int) -> bool:
    """Exact port match: ``appium-4723*.log`` also globs port 47231."""
    return path.stem == f"appium-{port}" or path.stem.startswith(f"appium-{port}-")


def port_log_paths(port: int) -> list[Path]:
    """Every log file for *port*, newest first (spawn files and the legacy name)."""
    log_dir = appium_log_dir()
    if not log_dir.is_dir():
        return []
    dated: list[tuple[float, Path]] = []
    for path in log_dir.glob(f"appium-{port}*.log"):
        if not _matches_port(path, port):
            continue
        try:
            dated.append((path.stat().st_mtime, path))
        except OSError:
            continue
    return [path for _, path in sorted(dated, reverse=True)]


def newest_log_path(port: int) -> Path:
    """What ``/agent/appium/{port}/logs`` reads: the most recent spawn's file."""
    paths = port_log_paths(port)
    return paths[0] if paths else appium_log_path(port)


def remove_logs_for_port(port: int) -> None:
    for path in port_log_paths(port):
        with contextlib.suppress(OSError):
            path.unlink()


def prune_port_logs(port: int, *, keep: int) -> None:
    """Keep the *keep* newest spawn logs for a port; delete the rest."""
    for path in port_log_paths(port)[keep:]:
        with contextlib.suppress(OSError):
            path.unlink()


def tail_lines(path: Path, lines: int) -> list[str]:
    """Return up to the last *lines* lines of *path*, reading at most ``TAIL_READ_BYTES``."""
    try:
        with path.open("rb") as fh:
            size = fh.seek(0, os.SEEK_END)
            fh.seek(max(0, size - TAIL_READ_BYTES))
            data = fh.read()
    except FileNotFoundError:
        return []
    all_lines = data.decode(errors="replace").splitlines()
    if size > TAIL_READ_BYTES and all_lines:
        # A mid-file read almost certainly starts inside a line; drop the fragment.
        all_lines = all_lines[1:]
    return all_lines[-lines:]


def truncate_if_oversized(path: Path) -> bool:
    """Copy-truncate *path* down to its last ``TAIL_READ_BYTES`` if it exceeds ``MAX_LOG_BYTES``.

    The child keeps its fd open across truncation; ``O_APPEND`` on that fd
    means its next write lands at the new EOF. A write racing the
    truncate-then-rewrite window can interleave mid-tail — rare and benign.
    """
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return False
    if size <= MAX_LOG_BYTES:
        return False
    with path.open("r+b") as fh:
        fh.seek(size - TAIL_READ_BYTES)
        tail = fh.read()
        fh.seek(0)
        fh.truncate()
        fh.write(tail)
    return True


def truncate_oversized_logs() -> None:
    """One maintenance pass: copy-truncate every oversized log file."""
    log_dir = appium_log_dir()
    if not log_dir.is_dir():
        return
    for entry in log_dir.glob("*.log"):
        with contextlib.suppress(OSError):
            truncate_if_oversized(entry)


def sweep_log_dir() -> None:
    """Delete all log files. Only safe before any node starts: survivors are
    orphans from a previous agent process whose children are gone."""
    log_dir = appium_log_dir()
    if not log_dir.is_dir():
        return
    for entry in log_dir.glob("*.log"):
        with contextlib.suppress(OSError):
            entry.unlink()
