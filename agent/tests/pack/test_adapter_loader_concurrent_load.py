import asyncio
import tarfile
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest

from agent_app.pack import adapter_loader

pytestmark = pytest.mark.asyncio


async def _build_minimal_tarball(tmp_path: Path) -> Path:
    """Build a minimal tarball with one wheel containing a top-level
    ``adapter`` package exposing class ``Adapter``.
    """
    pkg_dir = tmp_path / "pkg"
    pkg_dir.mkdir()
    (pkg_dir / "adapter").mkdir()
    (pkg_dir / "adapter" / "__init__.py").write_text("class Adapter:\n    pass\n")
    wheel = tmp_path / "adapter-0.0.1-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as zf:
        zf.write(pkg_dir / "adapter" / "__init__.py", "adapter/__init__.py")
    tarball = tmp_path / "pack.tgz"
    with tarfile.open(tarball, "w:gz") as tar:
        tar.add(wheel, arcname="adapter/adapter-0.0.1-py3-none-any.whl")
    return tarball


async def test_concurrent_load_adapter_for_same_key_serializes_install(
    tmp_path: Path,
) -> None:
    """Two concurrent ``load_adapter`` calls for the same
    ``(pack_id, release, runtime_dir)`` must serialize on the install. The
    first caller extracts + installs; the second caller observes the cache
    hit and skips the install.
    """
    adapter_loader._adapter_cache_clear()
    tarball = await _build_minimal_tarball(tmp_path)
    runtime_dir = tmp_path / "runtime"

    install_calls = 0
    real_install = adapter_loader._install_wheel

    async def counting_install(wheel: Path, target_dir: Path) -> None:
        nonlocal install_calls
        install_calls += 1
        await asyncio.sleep(0.05)
        await real_install(wheel, target_dir)

    with patch("agent_app.pack.adapter_loader._install_wheel", counting_install):
        results = await asyncio.gather(
            adapter_loader.load_adapter(
                pack_id="appium-uiautomator2",
                release="1.0.0",
                tarball_path=tarball,
                runtime_dir=runtime_dir,
                venv_python="/unused/python",
            ),
            adapter_loader.load_adapter(
                pack_id="appium-uiautomator2",
                release="1.0.0",
                tarball_path=tarball,
                runtime_dir=runtime_dir,
                venv_python="/unused/python",
            ),
        )

    assert install_calls == 1, (
        f"Expected exactly 1 install call but got {install_calls} — "
        "concurrent load_adapter for the same key did not serialize "
        "(missing per-key install lock)"
    )
    assert results[0] is results[1], (
        "Both load_adapter calls returned different instances — "
        "the cache write race produced two distinct cached adapters"
    )
