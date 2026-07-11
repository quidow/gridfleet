"""Prepare the adapter wheel shipped in a driver-pack tarball for a worker.

The tarball ships a single pure-Python wheel under ``adapter/<wheel-name>.whl``.
The worker subprocess imports the extracted ``adapter`` package in its own
interpreter, so the supervisor does not import or cache adapter modules.

The agent's uv environment ships without ``pip``, so we treat the wheel as
what PEP 427 says it is — a zip archive — and extract its contents directly
into the per-runtime ``site/`` directory. For pure-Python ``py3-none-any``
wheels (which is all an adapter wheel needs to be), this matches what
``pip install --no-deps --target=site/ wheel`` would produce.

"""

from __future__ import annotations

import asyncio
import tarfile
import zipfile
from pathlib import Path, PurePosixPath


class AdapterLoadError(RuntimeError):
    """Raised when an adapter wheel cannot be extracted, installed or imported."""


def _extract_wheel(tarball_path: Path, dest_dir: Path) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    with tarfile.open(tarball_path, mode="r:*") as tar:
        wheels = [m for m in tar.getmembers() if m.name.endswith(".whl") and m.name.startswith("adapter/")]
        if not wheels:
            raise AdapterLoadError(
                f"tarball {tarball_path} contains no adapter wheel under adapter/*.whl",
            )
        if len(wheels) > 1:
            raise AdapterLoadError(
                f"tarball {tarball_path} contains multiple adapter wheels; not supported",
            )
        member = wheels[0]
        return _safe_extract_file_from_tar(tar, member, dest_dir)


def _safe_archive_path(name: str) -> PurePosixPath:
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise AdapterLoadError(f"unsafe archive path: {name!r}")
    return path


def _safe_extract_file_from_tar(tar: tarfile.TarFile, member: tarfile.TarInfo, dest_dir: Path) -> Path:
    if not member.isreg():
        raise AdapterLoadError(f"adapter wheel member {member.name!r} must be a regular file")
    try:
        member_path = _safe_archive_path(member.name)
    except AdapterLoadError as exc:
        raise AdapterLoadError(f"unsafe adapter wheel path: {member.name!r}") from exc
    if len(member_path.parts) != 2 or member_path.parts[0] != "adapter" or not member_path.name.endswith(".whl"):
        raise AdapterLoadError(f"unsafe adapter wheel path: {member.name!r}")
    target = (dest_dir / Path(*member_path.parts)).resolve()
    root = dest_dir.resolve()
    if root not in target.parents:
        raise AdapterLoadError(f"unsafe adapter wheel path: {member.name!r}")
    target.parent.mkdir(parents=True, exist_ok=True)
    source = tar.extractfile(member)
    if source is None:
        raise AdapterLoadError(f"adapter wheel {member.name!r} is not extractable")
    with source, target.open("wb") as handle:
        handle.write(source.read())
    return target


def _safe_extract_zip(zf: zipfile.ZipFile, target_dir: Path) -> None:
    root = target_dir.resolve()
    for info in zf.infolist():
        try:
            member_path = _safe_archive_path(info.filename)
        except AdapterLoadError as exc:
            raise AdapterLoadError(f"unsafe wheel entry: {info.filename!r}") from exc
        target = (root / Path(*member_path.parts)).resolve()
        if root not in target.parents and target != root:
            raise AdapterLoadError(f"unsafe wheel entry: {info.filename!r}")
        if info.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        with zf.open(info) as source, target.open("wb") as handle:
            handle.write(source.read())


def _install_wheel_sync(wheel: Path, target_dir: Path) -> None:
    """Install a pure-Python wheel into ``target_dir`` by zip extraction.

    PEP 427 wheels are zip archives whose top-level entries are the package
    directories that should land on ``sys.path``. For a
    ``Root-Is-Purelib: true`` ``py3-none-any`` wheel (the only flavour we
    support for adapters in B.2), extracting the archive into ``target_dir``
    is equivalent to ``pip install --no-deps --target=target_dir wheel``.

    Compiled-extension wheels and wheels carrying ``*.data/scripts`` would
    need the full pip install machinery; B.2 explicitly scopes adapters to
    pure-Python wheels.
    """

    target_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(wheel) as zf:
        _safe_extract_zip(zf, target_dir)


async def _install_wheel(wheel: Path, target_dir: Path) -> None:
    await asyncio.to_thread(_install_wheel_sync, wheel, target_dir)


async def prepare_adapter_site(*, tarball_path: Path, runtime_dir: Path) -> Path:
    """Extract and install the adapter wheel, returning its worker ``site``."""
    wheel_dir = runtime_dir / "wheels"
    site_dir = (runtime_dir / "site").resolve()
    wheel = _extract_wheel(tarball_path, wheel_dir)
    await _install_wheel(wheel, site_dir)
    return site_dir
