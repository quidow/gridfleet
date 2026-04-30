"""Tests for ``agent_app.pack.adapter_loader``.

The driver-pack tarball ships a wheel under ``adapter/<name>.whl``. The loader
extracts that wheel, installs it into a per-runtime ``site/`` directory and
dynamically imports the ``adapter`` package, returning the ``Adapter``
instance.

To avoid taking a build-tool dependency on ``build`` (not in the agent's dev
deps), the tests construct a minimal pure-Python ``py3-none-any`` wheel by
hand. PEP 427 wheels are plain zip archives, so a hand-crafted zip with the
required ``dist-info`` metadata is sufficient for the loader to install and
import.
"""

from __future__ import annotations

import io
import sys
import tarfile
import zipfile
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from agent_app.pack.adapter_loader import (
    AdapterLoadError,
    _adapter_cache_clear,
    load_adapter,
)

_ADAPTER_PY = """\
from typing import Any


class Adapter:
    pack_id = "vendor-foo"
    pack_release = "0.1.0"

    async def discover(self, ctx: Any) -> list[Any]:
        return []

    async def doctor(self, ctx: Any) -> list[Any]:
        return []

    async def health_check(self, ctx: Any) -> list[Any]:
        return []

    async def feature_action(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def lifecycle_action(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def pre_session(self, spec: Any) -> dict[str, str]:
        return {"appium:vendorMagic": "set"}

    async def post_session(self, spec: Any, outcome: Any) -> None:
        return None

    async def sidecar_lifecycle(self, *args: Any, **kwargs: Any) -> None:
        return None
"""

_WHEEL_METADATA = """\
Metadata-Version: 2.1
Name: fake-adapter
Version: 0.1.0
Summary: hand-crafted test adapter wheel
"""

_WHEEL_WHEEL = """\
Wheel-Version: 1.0
Generator: gridfleet-agent-tests
Root-Is-Purelib: true
Tag: py3-none-any
"""

_WHEEL_RECORD = ""  # contents written dynamically below


def _build_handcrafted_wheel(
    out_dir: Path,
    *,
    body: str = _ADAPTER_PY,
    package_files: dict[str, str] | None = None,
) -> Path:
    """Build a minimal pure-Python wheel zip suitable for ``adapter_loader``.

    Pure ``py3-none-any`` wheels are just zip archives that, when extracted
    onto ``sys.path``, expose their top-level packages directly. This bypasses
    the need for ``pip`` (the agent's uv environment ships without pip) and
    the ``build`` tool.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    wheel_path = out_dir / "fake_adapter-0.1.0-py3-none-any.whl"
    dist_info = "fake_adapter-0.1.0.dist-info"
    with zipfile.ZipFile(wheel_path, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("adapter/__init__.py", body)
        for path, contents in (package_files or {}).items():
            zf.writestr(f"adapter/{path}", contents)
        zf.writestr(f"{dist_info}/METADATA", _WHEEL_METADATA)
        zf.writestr(f"{dist_info}/WHEEL", _WHEEL_WHEEL)
        zf.writestr(f"{dist_info}/RECORD", "")
    return wheel_path


def _build_tarball_with_adapter_wheel(tmp_path: Path) -> Path:
    wheel = _build_handcrafted_wheel(tmp_path / "dist")
    tarball = tmp_path / "pack.tar.gz"
    with tarfile.open(tarball, mode="w:gz") as tar:
        tar.add(wheel, arcname=f"adapter/{wheel.name}")
        manifest = tmp_path / "manifest.yaml"
        manifest.write_text("schema_version: 1\nid: vendor-foo\n")
        tar.add(manifest, arcname="manifest.yaml")
    return tarball


def _build_named_tarball_with_adapter_wheel(
    tmp_path: Path,
    name: str,
    *,
    body: str,
    package_files: dict[str, str] | None = None,
) -> Path:
    wheel = _build_handcrafted_wheel(tmp_path / f"{name}-dist", body=body, package_files=package_files)
    tarball = tmp_path / f"{name}.tar.gz"
    with tarfile.open(tarball, mode="w:gz") as tar:
        tar.add(wheel, arcname=f"adapter/{wheel.name}")
    return tarball


@pytest.mark.asyncio
async def test_load_adapter_extracts_and_imports(tmp_path: Path) -> None:
    _adapter_cache_clear()
    tarball = _build_tarball_with_adapter_wheel(tmp_path)
    runtime_dir = tmp_path / "runtime"

    adapter = await load_adapter(
        pack_id="vendor-foo",
        release="0.1.0",
        tarball_path=tarball,
        runtime_dir=runtime_dir,
        venv_python=sys.executable,
    )

    assert adapter is not None
    extra = await adapter.pre_session(spec=type("S", (), {"capabilities": {}})())
    assert extra == {"appium:vendorMagic": "set"}
    # site/ is on sys.path
    assert str((runtime_dir / "site").resolve()) in sys.path


@pytest.mark.asyncio
async def test_load_adapter_stamps_pack_identity_from_load_context(tmp_path: Path) -> None:
    _adapter_cache_clear()
    body = _ADAPTER_PY.replace('pack_id = "vendor-foo"', 'pack_id = ""').replace(
        'pack_release = "0.1.0"',
        'pack_release = ""',
    )
    tarball = _build_named_tarball_with_adapter_wheel(tmp_path, "vendor-foo", body=body)

    adapter = await load_adapter(
        pack_id="vendor-foo",
        release="0.1.0",
        tarball_path=tarball,
        runtime_dir=tmp_path / "runtime",
        venv_python=sys.executable,
    )

    assert adapter.pack_id == "vendor-foo"
    assert adapter.pack_release == "0.1.0"


@pytest.mark.asyncio
async def test_load_adapter_uses_cache(tmp_path: Path) -> None:
    _adapter_cache_clear()
    tarball = _build_tarball_with_adapter_wheel(tmp_path)
    runtime_dir = tmp_path / "runtime"

    first = await load_adapter(
        pack_id="vendor-foo",
        release="0.1.0",
        tarball_path=tarball,
        runtime_dir=runtime_dir,
        venv_python=sys.executable,
    )
    second = await load_adapter(
        pack_id="vendor-foo",
        release="0.1.0",
        tarball_path=tarball,
        runtime_dir=runtime_dir,
        venv_python=sys.executable,
    )
    assert first is second


@pytest.mark.asyncio
async def test_load_adapter_drops_stale_adapter_submodules_between_packs(tmp_path: Path) -> None:
    _adapter_cache_clear()
    body = """\
from typing import Any


class Adapter:
    async def normalize_device(self, ctx: Any) -> str:
        from adapter.normalize import normalize_device

        return normalize_device()
"""
    first_tarball = _build_named_tarball_with_adapter_wheel(
        tmp_path,
        "roku",
        body=body,
        package_files={"normalize.py": "def normalize_device() -> str:\n    return 'roku'\n"},
    )
    second_tarball = _build_named_tarball_with_adapter_wheel(
        tmp_path,
        "android",
        body=body,
        package_files={"normalize.py": "def normalize_device() -> str:\n    return 'android'\n"},
    )
    first = await load_adapter(
        pack_id="roku",
        release="1.0.0",
        tarball_path=first_tarball,
        runtime_dir=tmp_path / "runtime-roku",
        venv_python=sys.executable,
    )
    assert await first.normalize_device(None) == "roku"

    second = await load_adapter(
        pack_id="android",
        release="1.0.0",
        tarball_path=second_tarball,
        runtime_dir=tmp_path / "runtime-android",
        venv_python=sys.executable,
    )

    assert await second.normalize_device(None) == "android"
    assert await first.normalize_device(None) == "roku"


@pytest.mark.asyncio
async def test_missing_adapter_wheel_raises(tmp_path: Path) -> None:
    _adapter_cache_clear()
    bare = tmp_path / "bare.tar.gz"
    with tarfile.open(bare, mode="w:gz") as tar:
        manifest = tmp_path / "manifest.yaml"
        manifest.write_text("id: x\n")
        tar.add(manifest, arcname="manifest.yaml")

    with pytest.raises(AdapterLoadError, match="adapter"):
        await load_adapter(
            pack_id="vendor-foo",
            release="0.1.0",
            tarball_path=bare,
            runtime_dir=tmp_path / "runtime",
            venv_python=sys.executable,
        )


@pytest.mark.asyncio
async def test_multiple_wheels_raise(tmp_path: Path) -> None:
    _adapter_cache_clear()
    wheel_a = _build_handcrafted_wheel(tmp_path / "a")
    wheel_b = _build_handcrafted_wheel(tmp_path / "b")
    tarball = tmp_path / "pack.tar.gz"
    with tarfile.open(tarball, mode="w:gz") as tar:
        tar.add(wheel_a, arcname=f"adapter/{wheel_a.name}")
        tar.add(wheel_b, arcname=f"adapter/other-{wheel_b.name}")

    with pytest.raises(AdapterLoadError, match="multiple"):
        await load_adapter(
            pack_id="vendor-foo",
            release="0.1.0",
            tarball_path=tarball,
            runtime_dir=tmp_path / "runtime",
            venv_python=sys.executable,
        )


@pytest.mark.asyncio
async def test_adapter_module_without_class_raises(tmp_path: Path) -> None:
    _adapter_cache_clear()
    body = "marker = 'no-class-here'\n"
    wheel = _build_handcrafted_wheel(tmp_path / "dist", body=body)
    tarball = tmp_path / "pack.tar.gz"
    with tarfile.open(tarball, mode="w:gz") as tar:
        tar.add(wheel, arcname=f"adapter/{wheel.name}")

    with pytest.raises(AdapterLoadError, match="Adapter"):
        await load_adapter(
            pack_id="vendor-foo",
            release="0.1.0",
            tarball_path=tarball,
            runtime_dir=tmp_path / "runtime",
            venv_python=sys.executable,
        )


@pytest.mark.asyncio
async def test_cache_clear_drops_stale_sys_path_entries(tmp_path: Path) -> None:
    _adapter_cache_clear()
    tarball = _build_tarball_with_adapter_wheel(tmp_path)
    runtime_dir = tmp_path / "runtime"
    await load_adapter(
        pack_id="vendor-foo",
        release="0.1.0",
        tarball_path=tarball,
        runtime_dir=runtime_dir,
        venv_python=sys.executable,
    )
    site_dir = (runtime_dir / "site").resolve()
    assert str(site_dir) in sys.path

    # Inject a stale path entry pointing at a now-deleted directory.
    stale = tmp_path / "stale-site"
    stale.mkdir()
    sys.path.insert(0, str(stale))
    stale.rmdir()

    _adapter_cache_clear()

    assert str(stale) not in sys.path


def _tarball_with_member(name: str, data: bytes = b"x", *, member_type: bytes | None = None) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo(name=name)
        info.size = len(data)
        if member_type is not None:
            info.type = member_type
        tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _wheel_with_entry(path: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(path, "x")
    return buf.getvalue()


@pytest.mark.asyncio
async def test_adapter_loader_rejects_traversing_tar_member(tmp_path: Path) -> None:
    tarball = tmp_path / "bad.tar.gz"
    tarball.write_bytes(_tarball_with_member("adapter/../../evil.whl", b"x"))

    with pytest.raises(AdapterLoadError, match="unsafe adapter wheel path"):
        await load_adapter(
            pack_id="bad",
            release="1.0.0",
            tarball_path=tarball,
            runtime_dir=tmp_path / "runtime",
            venv_python="python",
        )


@pytest.mark.asyncio
async def test_adapter_loader_rejects_tar_symlink_member(tmp_path: Path) -> None:
    tarball = tmp_path / "bad.tar.gz"
    tarball.write_bytes(_tarball_with_member("adapter/bad.whl", b"", member_type=tarfile.SYMTYPE))

    with pytest.raises(AdapterLoadError, match="regular file"):
        await load_adapter(
            pack_id="bad",
            release="1.0.0",
            tarball_path=tarball,
            runtime_dir=tmp_path / "runtime",
            venv_python="python",
        )


@pytest.mark.asyncio
async def test_adapter_loader_rejects_traversing_wheel_entry(tmp_path: Path) -> None:
    tarball = tmp_path / "bad.tar.gz"
    wheel_bytes = _wheel_with_entry("../outside.py")
    tarball.write_bytes(_tarball_with_member("adapter/adapter-1.0.0-py3-none-any.whl", wheel_bytes))

    with pytest.raises(AdapterLoadError, match="unsafe wheel entry"):
        await load_adapter(
            pack_id="bad",
            release="1.0.0",
            tarball_path=tarball,
            runtime_dir=tmp_path / "runtime",
            venv_python="python",
        )

    assert not (tmp_path / "outside.py").exists()
