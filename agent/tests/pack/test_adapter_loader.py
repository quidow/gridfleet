from __future__ import annotations

import io
import tarfile
import zipfile
from typing import TYPE_CHECKING

import pytest

from agent_app.pack.adapter_loader import AdapterLoadError, prepare_adapter_site

if TYPE_CHECKING:
    from pathlib import Path


def _build_wheel(out_dir: Path, *, body: str = "class Adapter:\n    pass\n") -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    wheel = out_dir / "fake_adapter-0.1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as zf:
        zf.writestr("adapter/__init__.py", body)
        zf.writestr("fake_adapter-0.1.0.dist-info/WHEEL", "Wheel-Version: 1.0\n")
    return wheel


def _tarball(tmp_path: Path, *, wheel_count: int = 1) -> Path:
    tarball = tmp_path / "pack.tar.gz"
    with tarfile.open(tarball, "w:gz") as tar:
        for index in range(wheel_count):
            wheel = _build_wheel(tmp_path / f"dist-{index}")
            tar.add(wheel, arcname=f"adapter/{index}-{wheel.name}")
    return tarball


@pytest.mark.asyncio
async def test_prepare_adapter_site_extracts_wheel(tmp_path: Path) -> None:
    site = await prepare_adapter_site(tarball_path=_tarball(tmp_path), runtime_dir=tmp_path / "runtime")
    assert site == (tmp_path / "runtime" / "site").resolve()
    assert (site / "adapter" / "__init__.py").is_file()


@pytest.mark.asyncio
async def test_missing_adapter_wheel_raises(tmp_path: Path) -> None:
    tarball = tmp_path / "bare.tar.gz"
    with tarfile.open(tarball, "w:gz"):
        pass
    with pytest.raises(AdapterLoadError, match="adapter"):
        await prepare_adapter_site(tarball_path=tarball, runtime_dir=tmp_path / "runtime")


@pytest.mark.asyncio
async def test_multiple_wheels_raise(tmp_path: Path) -> None:
    with pytest.raises(AdapterLoadError, match="multiple"):
        await prepare_adapter_site(tarball_path=_tarball(tmp_path, wheel_count=2), runtime_dir=tmp_path / "runtime")


def _tar_bytes(name: str, data: bytes = b"x", *, member_type: bytes | None = None) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo(name=name)
        info.size = len(data)
        if member_type is not None:
            info.type = member_type
        tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _wheel_bytes(path: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(path, "x")
    return buf.getvalue()


@pytest.mark.asyncio
async def test_rejects_unsafe_tar_member(tmp_path: Path) -> None:
    tarball = tmp_path / "bad.tar.gz"
    tarball.write_bytes(_tar_bytes("adapter/../../evil.whl"))
    with pytest.raises(AdapterLoadError, match="unsafe adapter wheel path"):
        await prepare_adapter_site(tarball_path=tarball, runtime_dir=tmp_path / "runtime")


@pytest.mark.asyncio
async def test_rejects_tar_symlink_member(tmp_path: Path) -> None:
    tarball = tmp_path / "bad.tar.gz"
    tarball.write_bytes(_tar_bytes("adapter/bad.whl", member_type=tarfile.SYMTYPE))
    with pytest.raises(AdapterLoadError, match="regular file"):
        await prepare_adapter_site(tarball_path=tarball, runtime_dir=tmp_path / "runtime")


@pytest.mark.asyncio
async def test_rejects_unsafe_wheel_entry(tmp_path: Path) -> None:
    tarball = tmp_path / "bad.tar.gz"
    tarball.write_bytes(_tar_bytes("adapter/adapter-1.0.0-py3-none-any.whl", _wheel_bytes("../outside.py")))
    with pytest.raises(AdapterLoadError, match="unsafe wheel entry"):
        await prepare_adapter_site(tarball_path=tarball, runtime_dir=tmp_path / "runtime")
    assert not (tmp_path / "outside.py").exists()
