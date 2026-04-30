from __future__ import annotations

import hashlib
import re
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import IO, TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

_SAFE_RE = re.compile(r"[^a-zA-Z0-9._-]")


class PackStorageError(RuntimeError):
    """Raised when tarball storage fails or detects an integrity conflict."""


@dataclass
class StorageRecord:
    path: str
    sha256: str
    size: int


class PackStorageService:
    def __init__(self, root: Path) -> None:
        self._root = Path(root).resolve()
        self._root.mkdir(parents=True, exist_ok=True)

    def _safe_segment(self, value: str) -> str:
        cleaned = _SAFE_RE.sub("_", value)
        if not cleaned:
            raise PackStorageError(f"empty segment: {value!r}")
        return cleaned

    def _path_for(self, pack_id: str, release: str) -> Path:
        return self._root / self._safe_segment(pack_id) / f"{self._safe_segment(release)}.tar.gz"

    def store(self, *, pack_id: str, release: str, data: bytes) -> StorageRecord:
        target = self._path_for(pack_id, release)
        target.parent.mkdir(parents=True, exist_ok=True)
        sha = hashlib.sha256(data).hexdigest()
        if target.exists():
            existing = hashlib.sha256(target.read_bytes()).hexdigest()
            if existing != sha:
                raise PackStorageError(
                    f"hash mismatch for existing artifact at {target}: existing={existing} new={sha}"
                )
            return StorageRecord(path=str(target), sha256=sha, size=len(data))
        target.write_bytes(data)
        return StorageRecord(path=str(target), sha256=sha, size=len(data))

    @contextmanager
    def open(self, path: str) -> Iterator[IO[bytes]]:
        resolved = Path(path).resolve()
        if not str(resolved).startswith(str(self._root)):
            raise PackStorageError(f"path {resolved} escapes storage root {self._root}")
        with resolved.open("rb") as handle:
            yield handle
