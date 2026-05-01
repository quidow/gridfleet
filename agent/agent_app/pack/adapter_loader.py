"""Driver-pack adapter loader.

Extract the adapter wheel that ships in a driver-pack tarball, install it
into a per-runtime ``site/`` directory and dynamically import the
``adapter`` package, returning the ``Adapter`` instance declared by the
pack.

Convention (B.2): the tarball ships a single wheel under
``adapter/<wheel-name>.whl``. The wheel exposes a top-level package named
``adapter`` containing a class ``Adapter`` that satisfies the
``DriverPackAdapter`` protocol.

The agent's uv environment ships without ``pip``, so we treat the wheel as
what PEP 427 says it is — a zip archive — and extract its contents directly
into the per-runtime ``site/`` directory. For pure-Python ``py3-none-any``
wheels (which is all an adapter wheel needs to be), this matches what
``pip install --no-deps --target=site/ wheel`` would produce.

Loaded adapters are cached by ``(pack_id, release, runtime_dir)`` so that
repeated dispatches reuse the same instance instead of paying the install +
import cost on every call.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import sys
import tarfile
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any


class AdapterLoadError(RuntimeError):
    """Raised when an adapter wheel cannot be extracted, installed or imported."""


@dataclass(frozen=True)
class _CacheKey:
    pack_id: str
    release: str
    runtime_dir: str


_cache: dict[_CacheKey, Any] = {}
_cache_install_locks: dict[_CacheKey, asyncio.Lock] = {}
_cache_lock_factory_lock = asyncio.Lock()
_adapter_call_lock = asyncio.Lock()


def _drop_adapter_modules() -> None:
    for name in list(sys.modules):
        if name == "adapter" or name.startswith("adapter."):
            sys.modules.pop(name, None)


@dataclass(frozen=True)
class _SiteActivation:
    install_dir_str: str


def _activate_adapter_site(install_dir: Path) -> _SiteActivation:
    install_dir_str = str(install_dir)
    sys.path[:] = [entry for entry in sys.path if entry != install_dir_str]
    sys.path.insert(0, install_dir_str)
    _drop_adapter_modules()
    return _SiteActivation(install_dir_str=install_dir_str)


def _deactivate_adapter_site(activation: _SiteActivation) -> None:
    sys.path[:] = [entry for entry in sys.path if entry != activation.install_dir_str]
    _drop_adapter_modules()


class _IsolatedAdapter:
    def __init__(
        self,
        instance: Any,  # noqa: ANN401 - dynamic adapter instance
        install_dir: Path,
        *,
        pack_id: str,
        release: str,
    ) -> None:
        self._instance = instance
        self._install_dir = install_dir
        self.pack_id = pack_id
        self.pack_release = release

    def __getattr__(self, name: str) -> Any:  # noqa: ANN401 - dynamic adapter attribute
        attr = getattr(self._instance, name)
        if not callable(attr):
            return attr

        async def _wrapped(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401 - dynamic adapter return
            async with _adapter_call_lock:
                activation = _activate_adapter_site(self._install_dir)
                try:
                    result = attr(*args, **kwargs)
                    if inspect.isawaitable(result):
                        return await result
                    return result
                finally:
                    _deactivate_adapter_site(activation)

        return _wrapped


def _adapter_cache_clear() -> None:
    """Clear the adapter cache and prune stale ``sys.path`` entries.

    Tests in particular create runtime directories under ``tmp_path`` that
    disappear between cases. Leaving their ``site/`` entries on
    ``sys.path`` causes ``importlib`` to resolve a stale ``adapter`` module
    on the next load. Drop any path entries that no longer exist on disk.
    """

    _cache.clear()
    _cache_install_locks.clear()
    sys.path[:] = [entry for entry in sys.path if not entry or Path(entry).exists()]
    # Also forget any previously-imported ``adapter`` package tree so the next
    # load resolves all hooks against the current wheel, including submodules
    # imported lazily from adapter methods.
    _drop_adapter_modules()


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


async def _get_or_create_install_lock(key: _CacheKey) -> asyncio.Lock:
    async with _cache_lock_factory_lock:
        lock = _cache_install_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _cache_install_locks[key] = lock
        return lock


async def load_adapter(
    *,
    pack_id: str,
    release: str,
    tarball_path: Path,
    runtime_dir: Path,
    venv_python: str,
) -> Any:  # noqa: ANN401 dynamically loaded adapter has no static type
    """Load (or fetch from cache) the adapter shipped inside ``tarball_path``.

    Returns the live ``Adapter`` instance ready to be dispatched against.
    The ``venv_python`` argument is accepted for forward compatibility with
    a pip-backed install path; the current implementation extracts the
    wheel zip directly and so does not need it.
    """

    key = _CacheKey(
        pack_id=pack_id,
        release=release,
        runtime_dir=str(runtime_dir.resolve()),
    )
    cached = _cache.get(key)
    if cached is not None:
        return cached

    install_lock = await _get_or_create_install_lock(key)
    async with install_lock:
        # Re-check the cache after acquiring the lock — the prior holder may
        # have finished the install while we waited.
        cached = _cache.get(key)
        if cached is not None:
            return cached

        wheel_dir = runtime_dir / "wheels"
        install_dir = (runtime_dir / "site").resolve()

        wheel = _extract_wheel(tarball_path, wheel_dir)
        await _install_wheel(wheel, install_dir)

        # Drop any cached ``adapter`` package tree imported against a different
        # ``site/`` directory before resolving against the current one.
        _ = _activate_adapter_site(install_dir)
        try:
            module = importlib.import_module("adapter")
        except ImportError as exc:
            raise AdapterLoadError(f"failed to import adapter module: {exc}") from exc

        cls = getattr(module, "Adapter", None)
        if cls is None:
            raise AdapterLoadError("adapter module does not expose class Adapter")

        instance = _IsolatedAdapter(cls(), install_dir, pack_id=pack_id, release=release)
        _cache[key] = instance
        return instance
