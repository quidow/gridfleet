"""Tests for ``agent_app.pack.adapter_loader``.

The driver-pack tarball ships a wheel under ``adapter/<name>.whl``. The loader
extracts that wheel, installs it into a per-runtime ``site/`` directory and
imports the ``adapter`` package under a unique per-(pack, release, runtime)
module name, returning the ``Adapter`` instance.

To avoid taking a build-tool dependency on ``build`` (not in the agent's dev
deps), the tests construct a minimal pure-Python ``py3-none-any`` wheel by
hand. PEP 427 wheels are plain zip archives, so a hand-crafted zip with the
required ``dist-info`` metadata is sufficient for the loader to install and
import.
"""

from __future__ import annotations

import asyncio
import io
import sys
import tarfile
import zipfile
from typing import TYPE_CHECKING, ClassVar

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from agent_app.pack.adapter_dispatch import (
    adapter_supports,
    dispatch_discover,
    dispatch_normalize_device,
    missing_declared_hooks,
)
from agent_app.pack.adapter_loader import (
    AdapterLoadError,
    load_adapter,
)
from agent_app.pack.manifest import DesiredPack, DesiredPlatform
from agent_app.pack.runtime_types import AppiumInstallable

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

    async def lifecycle_action(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def pre_session(self, spec: Any) -> dict[str, str]:
        return {"appium:vendorMagic": "set"}

    async def post_session(self, spec: Any, outcome: Any) -> None:
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


def _adapter_module_names() -> list[str]:
    return [name for name in sys.modules if name == "adapter" or name.startswith("adapter.")]


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
    tarball = _build_tarball_with_adapter_wheel(tmp_path)
    runtime_dir = tmp_path / "runtime"

    adapter = await load_adapter(
        pack_id="vendor-foo",
        release="0.1.0",
        tarball_path=tarball,
        runtime_dir=runtime_dir,
    )

    assert adapter is not None
    site_dir = str((runtime_dir / "site").resolve())
    assert site_dir not in sys.path

    extra = await adapter.pre_session(spec=type("S", (), {"capabilities": {}})())

    assert extra == {"appium:vendorMagic": "set"}
    # Loading never touches sys.path and never claims the literal name
    # ``adapter`` — each pack imports under its own unique module name.
    assert site_dir not in sys.path
    assert _adapter_module_names() == []


@pytest.mark.asyncio
async def test_load_adapter_stamps_pack_identity_from_load_context(tmp_path: Path) -> None:
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
    )

    assert adapter.pack_id == "vendor-foo"
    assert adapter.pack_release == "0.1.0"


@pytest.mark.asyncio
async def test_load_adapter_uses_cache(tmp_path: Path) -> None:
    tarball = _build_tarball_with_adapter_wheel(tmp_path)
    runtime_dir = tmp_path / "runtime"

    first = await load_adapter(
        pack_id="vendor-foo",
        release="0.1.0",
        tarball_path=tarball,
        runtime_dir=runtime_dir,
    )
    second = await load_adapter(
        pack_id="vendor-foo",
        release="0.1.0",
        tarball_path=tarball,
        runtime_dir=runtime_dir,
    )
    assert first is second


@pytest.mark.asyncio
async def test_load_adapter_isolates_lazy_submodule_imports_between_packs(tmp_path: Path) -> None:
    body = """\
from typing import Any


class Adapter:
    async def normalize_device(self, ctx: Any) -> str:
        from .normalize import normalize_device

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
    )
    assert await first.normalize_device(None) == "roku"

    second = await load_adapter(
        pack_id="android",
        release="1.0.0",
        tarball_path=second_tarball,
        runtime_dir=tmp_path / "runtime-android",
    )

    assert await second.normalize_device(None) == "android"
    assert await first.normalize_device(None) == "roku"


@pytest.mark.asyncio
async def test_hooks_from_different_packs_run_concurrently(tmp_path: Path) -> None:
    """Hook calls are not serialized: a blocked hook on one pack must not
    prevent another pack's hook from starting."""
    body = """\
from typing import Any


class Adapter:
    async def health_check(self, ctx: Any) -> str:
        ctx.started.set()
        await ctx.release.wait()
        return ctx.label
"""
    first = await load_adapter(
        pack_id="pack-a",
        release="1.0.0",
        tarball_path=_build_named_tarball_with_adapter_wheel(tmp_path, "pack-a", body=body),
        runtime_dir=tmp_path / "runtime-a",
    )
    second = await load_adapter(
        pack_id="pack-b",
        release="1.0.0",
        tarball_path=_build_named_tarball_with_adapter_wheel(tmp_path, "pack-b", body=body),
        runtime_dir=tmp_path / "runtime-b",
    )

    class _Ctx:
        def __init__(self, label: str) -> None:
            self.label = label
            self.started = asyncio.Event()
            self.release = asyncio.Event()

    ctx_a, ctx_b = _Ctx("a"), _Ctx("b")
    task_a = asyncio.create_task(first.health_check(ctx_a))
    task_b = asyncio.create_task(second.health_check(ctx_b))
    try:
        # Both hooks must be in flight at the same time.
        await asyncio.wait_for(
            asyncio.gather(ctx_a.started.wait(), ctx_b.started.wait()),
            timeout=1,
        )
    finally:
        ctx_a.release.set()
        ctx_b.release.set()

    assert await task_a == "a"
    assert await task_b == "b"


@pytest.mark.asyncio
async def test_load_adapter_rejects_absolute_self_imports(tmp_path: Path) -> None:
    body = """\
from typing import Any

from adapter.normalize import normalize_device


class Adapter:
    async def normalize_device(self, ctx: Any) -> str:
        return normalize_device()
"""
    tarball = _build_named_tarball_with_adapter_wheel(
        tmp_path,
        "absolute",
        body=body,
        package_files={"normalize.py": "def normalize_device() -> str:\n    return 'x'\n"},
    )

    with pytest.raises(AdapterLoadError, match="must be relative"):
        await load_adapter(
            pack_id="absolute",
            release="1.0.0",
            tarball_path=tarball,
            runtime_dir=tmp_path / "runtime",
        )


@pytest.mark.asyncio
async def test_missing_adapter_wheel_raises(tmp_path: Path) -> None:
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
        )


@pytest.mark.asyncio
async def test_multiple_wheels_raise(tmp_path: Path) -> None:
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
        )


@pytest.mark.asyncio
async def test_adapter_module_without_class_raises(tmp_path: Path) -> None:
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
        )


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
        )

    assert not (tmp_path / "outside.py").exists()


# ---------------------------------------------------------------------------
# Minimal two-hook adapter: the supported "core-only" pack shape.
# ---------------------------------------------------------------------------

_MINIMAL_CORE_ADAPTER_PY = """\
from typing import Any

from agent_app.pack.adapter_types import NormalizedDevice


class Adapter:
    pack_id = "vendor-minimal"
    pack_release = "0.1.0"

    async def discover(self, ctx: Any) -> list[Any]:
        return []

    async def normalize_device(self, ctx: Any) -> NormalizedDevice:
        target = ctx.raw_input.get("connection_target", "")
        return NormalizedDevice(
            identity_scheme="android_serial",
            identity_scope="host",
            identity_value=target,
            connection_target=target,
            ip_address="",
            device_type="emulator",
            connection_type="usb",
            os_version="14",
            field_errors=[],
        )
"""


class _MinimalDiscoveryCtx:
    host_id = "host-1"
    platform_id = "android_mobile"


class _MinimalNormalizeCtx:
    host_id = "host-1"
    platform_id = "android_mobile"
    raw_input: ClassVar[dict[str, str]] = {"connection_target": "emulator-5554"}


def _installable() -> AppiumInstallable:
    return AppiumInstallable(source="npm", package="appium", version="2.0.0", recommended=None, known_bad=[])


@pytest.mark.asyncio
async def test_minimal_two_hook_adapter_loads_and_dispatches(tmp_path: Path) -> None:
    """A pack whose adapter implements only the required core (discover +
    normalize_device) is a supported shape: it loads with no cross-check block,
    dispatches its two hooks, and reports every optional hook as unsupported
    rather than raising."""
    tarball = _build_named_tarball_with_adapter_wheel(tmp_path, "vendor-minimal", body=_MINIMAL_CORE_ADAPTER_PY)
    adapter = await load_adapter(
        pack_id="vendor-minimal",
        release="0.1.0",
        tarball_path=tarball,
        runtime_dir=tmp_path / "runtime",
    )

    assert adapter_supports(adapter, "discover") is True
    assert adapter_supports(adapter, "normalize_device") is True
    for optional in ("health_check", "doctor", "lifecycle_action", "telemetry"):
        assert adapter_supports(adapter, optional) is False

    # A bare-platform manifest declares no optional capabilities → clean cross-check.
    pack = DesiredPack(
        id="vendor-minimal",
        release="0.1.0",
        appium_server=_installable(),
        appium_driver=_installable(),
        platforms=[
            DesiredPlatform(
                id="p",
                automation_name="a",
                device_types=["real_device"],
                connection_types=["usb"],
                identity_scheme="s",
                identity_scope="host",
                stereotype={},
            )
        ],
    )
    assert missing_declared_hooks(pack, adapter) == []

    # Both core hooks dispatch end to end through the real loaded wheel.
    assert await dispatch_discover(adapter, _MinimalDiscoveryCtx()) == []
    normalized = await dispatch_normalize_device(adapter, _MinimalNormalizeCtx())
    assert normalized.connection_target == "emulator-5554"
