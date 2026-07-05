"""Driver-pack adapter loader.

Extract the adapter wheel that ships in a driver-pack tarball, install it
into a per-runtime ``site/`` directory and import the ``adapter`` package
under a unique per-(pack, release, runtime) module name, returning the
``Adapter`` instance declared by the pack.

Convention (B.2): the tarball ships a single wheel under
``adapter/<wheel-name>.whl``. The wheel exposes a top-level package named
``adapter`` containing a class ``Adapter`` that satisfies the
``DriverPackAdapter`` protocol. Adapter-internal imports must be relative
(``from .health import ...``): every adapter is imported under a unique
module name, so the literal name ``adapter`` never exists in ``sys.modules``
at runtime and absolute self-imports are rejected at load time.

The agent's uv environment ships without ``pip``, so we treat the wheel as
what PEP 427 says it is — a zip archive — and extract its contents directly
into the per-runtime ``site/`` directory. For pure-Python ``py3-none-any``
wheels (which is all an adapter wheel needs to be), this matches what
``pip install --no-deps --target=site/ wheel`` would produce.

Loaded adapters are cached by ``(pack_id, release, runtime_dir)`` so that
repeated dispatches reuse the same instance instead of paying the install +
import cost on every call. Because each adapter owns a distinct module tree,
loading never touches ``sys.path`` and hook calls are not serialized —
hooks from any packs may run concurrently.
"""

from __future__ import annotations

import ast
import asyncio
import hashlib
import importlib.util
import re
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


def _module_name(key: _CacheKey) -> str:
    """Unique, valid module name for one (pack, release, runtime) adapter tree.

    The sanitized pack id keeps tracebacks readable; the digest guarantees
    uniqueness across releases and runtime dirs.
    """
    digest = hashlib.sha256(f"{key.pack_id}\n{key.release}\n{key.runtime_dir}".encode()).hexdigest()[:12]
    slug = re.sub(r"[^0-9A-Za-z_]", "_", key.pack_id)
    return f"gridfleet_adapter_{slug}_{digest}"


def _drop_module_tree(module_name: str) -> None:
    prefix = module_name + "."
    for name in list(sys.modules):
        if name == module_name or name.startswith(prefix):
            sys.modules.pop(name, None)


def _assert_relative_imports(package_dir: Path) -> None:
    """Reject absolute self-imports (``from adapter.x import y``) at load time.

    Adapters are imported under a unique module name, so the literal
    ``adapter`` package never exists at runtime; an absolute self-import
    would surface as a confusing ``ModuleNotFoundError`` mid-hook. Fail the
    install with an actionable message instead.
    """
    for source_path in sorted(package_dir.rglob("*.py")):
        rel = source_path.relative_to(package_dir.parent)
        try:
            tree = ast.parse(source_path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError) as exc:
            raise AdapterLoadError(f"adapter module {rel} is not valid Python: {exc}") from exc
        for node in ast.walk(tree):
            offending: tuple[int, str] | None = None
            if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module is not None:
                if node.module == "adapter" or node.module.startswith("adapter."):
                    offending = (node.lineno, node.module)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "adapter" or alias.name.startswith("adapter."):
                        offending = (node.lineno, alias.name)
            if offending is not None:
                lineno, name = offending
                raise AdapterLoadError(
                    f"adapter module {rel} line {lineno} imports {name!r} absolutely; "
                    "adapter-internal imports must be relative (e.g. 'from .health import ...')"
                )


def _import_adapter(module_name: str, install_dir: Path) -> Any:  # noqa: ANN401 - dynamically loaded module
    package_dir = install_dir / "adapter"
    init_py = package_dir / "__init__.py"
    if not init_py.is_file():
        raise AdapterLoadError(f"adapter wheel did not install adapter/__init__.py under {install_dir}")
    spec = importlib.util.spec_from_file_location(
        module_name,
        init_py,
        submodule_search_locations=[str(package_dir)],
    )
    if spec is None or spec.loader is None:
        raise AdapterLoadError(f"failed to build import spec for adapter package at {package_dir}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        _drop_module_tree(module_name)
        raise
    return module


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
) -> Any:  # noqa: ANN401 dynamically loaded adapter has no static type
    """Load (or fetch from cache) the adapter shipped inside ``tarball_path``.

    Returns the live ``Adapter`` instance ready to be dispatched against.
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
        await asyncio.to_thread(_assert_relative_imports, install_dir / "adapter")

        module_name = _module_name(key)
        try:
            module = _import_adapter(module_name, install_dir)
        except ImportError as exc:
            raise AdapterLoadError(f"failed to import adapter module: {exc}") from exc

        cls = getattr(module, "Adapter", None)
        if cls is None:
            _drop_module_tree(module_name)
            raise AdapterLoadError("adapter module does not expose class Adapter")

        instance = cls()
        # Stamp identity from the load context so dispatch error reporting
        # never depends on the adapter author setting these correctly.
        instance.pack_id = pack_id
        instance.pack_release = release
        _cache[key] = instance
        return instance
