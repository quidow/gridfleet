#!/usr/bin/env python3
from __future__ import annotations

import argparse
import io
import os
import subprocess
import tarfile
import tempfile
from pathlib import Path

import yaml


def _tar_info(name: str, size: int) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.size = size
    info.mtime = 0
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    return info


def _build_adapter_wheel(adapter_dir: Path, output_dir: Path) -> Path:
    wheel_dir = output_dir / "adapter-wheel"
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
        raise RuntimeError(f"expected one adapter wheel, found {len(wheels)}")
    return wheels[0]


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a deterministic driver-pack tarball.")
    parser.add_argument("--pack-dir", required=True, help="Path to pack source directory containing manifest.yaml")
    parser.add_argument("--out", required=True)
    parser.add_argument("--id", default=None)
    parser.add_argument("--release", default=None)
    parser.add_argument("--adapter-dir", type=Path, default=None, help="Optional adapter source directory to wheel into adapter/*.whl")
    args = parser.parse_args()

    pack_dir = Path(args.pack_dir)
    manifest_path = pack_dir / "manifest.yaml"
    data = yaml.safe_load(manifest_path.read_text())
    data.pop("origin", None)
    if args.id is not None:
        data["id"] = args.id
    if args.release is not None:
        data["release"] = args.release
    manifest_bytes = yaml.safe_dump(data, sort_keys=False).encode("utf-8")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="gridfleet-driver-pack-build-") as temp_dir_name:
        adapter_wheel = _build_adapter_wheel(args.adapter_dir, Path(temp_dir_name)) if args.adapter_dir else None
        with tarfile.open(out, "w:gz", format=tarfile.PAX_FORMAT) as tar:
            tar.addfile(_tar_info("manifest.yaml", len(manifest_bytes)), io.BytesIO(manifest_bytes))

            for file_path in sorted(pack_dir.rglob("*")):
                if not file_path.is_file():
                    continue
                rel = file_path.relative_to(pack_dir)
                if rel.name == "manifest.yaml":
                    continue
                if rel.parts[0] == "templates" or "__pycache__" in rel.parts:
                    continue
                with file_path.open("rb") as handle:
                    tar.addfile(_tar_info(str(rel), file_path.stat().st_size), handle)

            if adapter_wheel is not None:
                with adapter_wheel.open("rb") as handle:
                    tar.addfile(
                        _tar_info(f"adapter/{adapter_wheel.name}", adapter_wheel.stat().st_size),
                        handle,
                    )


if __name__ == "__main__":
    main()
