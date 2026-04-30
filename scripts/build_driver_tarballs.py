#!/usr/bin/env python3
"""Build uploadable driver pack tarballs from curated manifests.

Usage:
    python scripts/build_driver_tarballs.py [--output-dir dist/driver-packs]
"""

from __future__ import annotations

import argparse
import gzip
import io
import os
import subprocess
import tarfile
import tempfile
from pathlib import Path
from typing import BinaryIO

ADAPTERS_DIR = Path(__file__).resolve().parent.parent / "driver-packs" / "adapters"
CURATED_DIR = Path(__file__).resolve().parent.parent / "driver-packs" / "curated"
DEFAULT_OUTPUT = Path(__file__).resolve().parent.parent / "dist" / "driver-packs"
PACK_ADAPTERS = {
    "appium-roku-dlenroc": "roku",
    "appium-uiautomator2": "android",
    "appium-xcuitest": "apple",
}


def _tar_info(name: str, size: int) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.size = size
    info.mtime = 0
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    return info


def _add_file(tar: tarfile.TarFile, arcname: str, handle: BinaryIO, size: int) -> None:
    tar.addfile(_tar_info(arcname, size), handle)


def build_adapter_wheel(adapter_name: str, output_dir: Path) -> Path:
    adapter_dir = ADAPTERS_DIR / adapter_name
    if not adapter_dir.exists():
        raise FileNotFoundError(f"adapter directory not found: {adapter_dir}")
    wheel_dir = output_dir / adapter_name
    wheel_dir.mkdir(parents=True, exist_ok=True)
    env = {
        **os.environ,
        "SOURCE_DATE_EPOCH": os.environ.get("SOURCE_DATE_EPOCH", "0"),
        "UV_CACHE_DIR": os.environ.get("UV_CACHE_DIR", "/tmp/uv-cache-adapters"),
    }
    subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(wheel_dir)],
        cwd=adapter_dir,
        check=True,
        env=env,
    )
    wheels = sorted(wheel_dir.glob("*.whl"))
    if len(wheels) != 1:
        raise RuntimeError(f"expected one wheel for adapter {adapter_name}, found {len(wheels)}")
    return wheels[0]


def _pack_release(pack_dir: Path) -> str:
    for line in (pack_dir / "manifest.yaml").read_text().splitlines():
        key, separator, value = line.partition(":")
        if separator and key.strip() == "release":
            release = value.strip().strip("\"'")
            if release:
                return release
    raise RuntimeError(f"manifest for {pack_dir.name} is missing release")


def build_tarball(pack_dir: Path, *, adapter_wheel: Path | None = None) -> tuple[str, str, bytes]:
    release = _pack_release(pack_dir)
    buf = io.BytesIO()
    with (
        gzip.GzipFile(filename="", mode="wb", fileobj=buf, mtime=0) as gzip_file,
        tarfile.open(fileobj=gzip_file, mode="w", format=tarfile.PAX_FORMAT) as tar,
    ):
        for file_path in sorted(pack_dir.rglob("*")):
            if not file_path.is_file():
                continue
            rel = file_path.relative_to(pack_dir)
            if rel.parts[0] == "templates":
                continue
            if "__pycache__" in rel.parts or rel.suffix == ".pyc":
                continue
            with file_path.open("rb") as handle:
                _add_file(tar, str(rel), handle, file_path.stat().st_size)
        if adapter_wheel is not None:
            with adapter_wheel.open("rb") as handle:
                _add_file(
                    tar,
                    f"adapter/{adapter_wheel.name}",
                    handle,
                    adapter_wheel.stat().st_size,
                )
    return pack_dir.name, release, buf.getvalue()


def main() -> None:
    parser = argparse.ArgumentParser(description="Build driver pack tarballs")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="Output directory for tarballs",
    )
    args = parser.parse_args()

    if not CURATED_DIR.exists():
        print(f"No curated directory found at {CURATED_DIR}")
        return

    args.output_dir.mkdir(parents=True, exist_ok=True)
    pack_dirs = sorted(d for d in CURATED_DIR.iterdir() if d.is_dir() and (d / "manifest.yaml").exists())

    if not pack_dirs:
        print("No curated packs found")
        return

    with tempfile.TemporaryDirectory(prefix="gridfleet-driver-pack-build-") as temp_dir_name:
        wheel_root = Path(temp_dir_name)
        adapter_wheels: dict[str, Path] = {}
        for pack_dir in pack_dirs:
            adapter_name = PACK_ADAPTERS.get(pack_dir.name)
            if adapter_name is not None:
                adapter_wheels[pack_dir.name] = build_adapter_wheel(adapter_name, wheel_root)

        for pack_dir in pack_dirs:
            pack_id, _release, data = build_tarball(pack_dir, adapter_wheel=adapter_wheels.get(pack_dir.name))
            latest_path = args.output_dir / f"{pack_id}.tar.gz"
            latest_path.write_bytes(data)
            print(f"  {latest_path} ({len(data)} bytes)")

    print(f"\nBuilt {len(pack_dirs)} tarball(s) in {args.output_dir}")


if __name__ == "__main__":
    main()
