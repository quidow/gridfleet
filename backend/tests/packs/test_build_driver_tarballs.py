from __future__ import annotations

import importlib.util
import io
import sys
import tarfile
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from types import ModuleType

    import pytest


def _load_build_script() -> ModuleType:
    script_path = Path(__file__).resolve().parents[3] / "scripts" / "build_driver_tarballs.py"
    spec = importlib.util.spec_from_file_location("build_driver_tarballs", script_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_build_tarball_can_embed_adapter_wheel(tmp_path: Path) -> None:
    build_script = _load_build_script()
    pack_dir = tmp_path / "appium-roku-dlenroc"
    pack_dir.mkdir()
    (pack_dir / "manifest.yaml").write_text(
        "schema_version: 1\nid: appium-roku-dlenroc\nrelease: 2026.04.5\n",
    )
    wheel = tmp_path / "gridfleet_adapter_roku-0.1.0-py3-none-any.whl"
    wheel.write_bytes(b"fake wheel")

    pack_id, release, data = build_script.build_tarball(pack_dir, adapter_wheel=wheel)

    assert pack_id == "appium-roku-dlenroc"
    assert release == "2026.04.5"
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        names = tar.getnames()
        assert "manifest.yaml" in names
        assert "adapter/gridfleet_adapter_roku-0.1.0-py3-none-any.whl" in names


def test_build_tarball_uses_deterministic_gzip_header(tmp_path: Path) -> None:
    build_script = _load_build_script()
    pack_dir = tmp_path / "appium-roku-dlenroc"
    pack_dir.mkdir()
    (pack_dir / "manifest.yaml").write_text(
        "schema_version: 1\nid: appium-roku-dlenroc\nrelease: 2026.04.5\n",
    )

    _, _, data = build_script.build_tarball(pack_dir)

    assert int.from_bytes(data[4:8], "little") == 0


def test_main_writes_latest_tarball_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    build_script = _load_build_script()
    curated_dir = tmp_path / "curated"
    pack_dir = curated_dir / "custom-pack"
    output_dir = tmp_path / "dist"
    pack_dir.mkdir(parents=True)
    (pack_dir / "manifest.yaml").write_text(
        "schema_version: 1\nid: custom-pack\nrelease: 2026.04.5\n",
    )
    monkeypatch.setattr(build_script, "CURATED_DIR", curated_dir)
    monkeypatch.setattr(sys, "argv", ["build_driver_tarballs.py", "--output-dir", str(output_dir)])

    build_script.main()

    assert (output_dir / "custom-pack.tar.gz").exists()
    assert not (output_dir / "custom-pack-2026.04.5.tar.gz").exists()
