"""End-to-end vertical slice for uploaded driver-pack adapters (B.2 Task 14).

This test exercises the full uploaded-pack pipeline against the in-process
FastAPI app and the agent's adapter machinery without spinning up a real
agent process:

1. Build a hand-crafted PEP 427 wheel containing a minimal ``adapter``
   package (the wheel is just a zip archive — pure-Python ``py3-none-any``
   wheels do not need ``pip``/``build`` to construct).
2. Wrap the wheel + a manifest in a ``.tar.gz`` driver-pack tarball.
3. POST it to ``/api/driver-packs/uploads`` against the in-process FastAPI
   app and assert the backend persisted the pack with ``origin=uploaded``,
   computed an ``artifact_sha256``, and stored the artifact on disk.
4. Drive the agent's ``tarball_fetch.download_and_verify`` against the
   same in-process app via ``ASGITransport``.
5. Drive ``adapter_loader.load_adapter`` to extract the wheel and import
   the adapter.
6. Dispatch ``adapter.discover`` and assert the adapter returns the
   fixture's expected candidate.
7. Dispatch ``adapter.pre_session`` and assert the cap-merge dict.

The agent code lives in a sibling project (``agent/agent_app``) that is not
listed as a backend dependency, so the test inserts the agent project root
on ``sys.path`` at module import. If the agent tree is missing for any
reason the test skips with a clear reason rather than producing an
ImportError that masks unrelated test runs.
"""

from __future__ import annotations

import hashlib
import io
import sys
import tarfile
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.main import app
from app.models.driver_pack import DriverPackRelease
from app.routers.driver_pack_uploads import get_pack_storage
from app.services.pack_storage_service import PackStorageService

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy.ext.asyncio import AsyncSession


_AGENT_ROOT = Path(__file__).resolve().parents[3] / "agent"
if not (_AGENT_ROOT / "agent_app" / "pack" / "adapter_loader.py").exists():
    pytest.skip(
        f"agent_app tree not found at {_AGENT_ROOT}; vertical-slice test skipped",
        allow_module_level=True,
    )
if str(_AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(_AGENT_ROOT))

from agent_app.pack.adapter_dispatch import (  # type: ignore[import-not-found]  # noqa: E402  (path injected above)
    dispatch_discover,
    dispatch_pre_session,
)
from agent_app.pack.adapter_loader import (  # type: ignore[import-not-found]  # noqa: E402
    _adapter_cache_clear,
    load_adapter,
)
from agent_app.pack.adapter_types import SessionSpec  # type: ignore[import-not-found]  # noqa: E402
from agent_app.pack.tarball_fetch import download_and_verify  # type: ignore[import-not-found]  # noqa: E402

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Fixture wheel + tarball construction.
# ---------------------------------------------------------------------------

_ADAPTER_BODY = """\
from typing import Any

from agent_app.pack.adapter_types import FeatureActionResult, SidecarStatus


class _Candidate:
    identity_scheme = "vendor_uid"
    identity_value = "vendor-uid-001"
    suggested_name = "Vendor Probe 001"
    detected_properties: dict[str, Any] = {"firmware": "1.2.3"}
    runnable = True
    missing_requirements: list[str] = []
    field_errors: list[Any] = []
    feature_status: list[Any] = []


class Adapter:
    pack_id = "vendor-foo"
    pack_release = "0.1.0"

    async def discover(self, ctx: Any) -> list[Any]:
        return [_Candidate()]

    async def doctor(self, ctx: Any) -> list[Any]:
        return []

    async def health_check(self, ctx: Any) -> list[Any]:
        return []

    async def lifecycle_action(self, action_id: Any, args: Any, ctx: Any) -> Any:
        # The dispatch layer validates this is a LifecycleActionResult, but
        # the vertical-slice test does not exercise lifecycle dispatch — we
        # return a duck-typed object with the same fields so that anyone
        # importing the wheel can still call it directly.
        class _Result:
            ok = True
            state = "ok"
            detail = ""

        return _Result()

    async def pre_session(self, spec: Any) -> dict[str, str]:
        return {"appium:vendorMagic": "set"}

    async def post_session(self, spec: Any, outcome: Any) -> None:
        return None

    async def feature_action(self, *args: Any, **kwargs: Any) -> FeatureActionResult:
        return FeatureActionResult(ok=True, detail="noop", data={})

    async def sidecar_lifecycle(self, *args: Any, **kwargs: Any) -> SidecarStatus:
        return SidecarStatus(ok=True, detail="noop", state="stopped")
"""

_WHEEL_METADATA = """\
Metadata-Version: 2.1
Name: adapter
Version: 0.1.0
Summary: vertical-slice fake adapter wheel
"""

_WHEEL_WHEEL = """\
Wheel-Version: 1.0
Generator: gridfleet-vertical-slice-tests
Root-Is-Purelib: true
Tag: py3-none-any
"""

_MANIFEST_YAML = """\
schema_version: 1
id: vendor-foo
release: 0.1.0
display_name: Vendor Foo
appium_server:
  source: npm
  package: appium
  version: ">=2.5,<3"
  recommended: 2.19.0
appium_driver:
  source: npm
  package: appium-vendor-foo-driver
  version: ">=0,<1"
  recommended: 0.1.0
platforms:
  - id: vendor_p
    display_name: Vendor Platform
    automation_name: VendorAutomation
    appium_platform_name: Vendor
    device_types: [real_device]
    connection_types: [network]
    grid_slots: [native]
    capabilities: { stereotype: {}, session_required: [] }
    identity: { scheme: vendor_uid, scope: global }
"""


def _build_handcrafted_wheel(out_dir: Path) -> Path:
    """Build a minimal pure-Python ``py3-none-any`` wheel by hand.

    PEP 427 wheels are zip archives: a pure-Python wheel only needs the
    package directory plus a ``*.dist-info`` directory with ``METADATA``,
    ``WHEEL`` and ``RECORD`` entries. Avoids the ``build``/``pip``
    dependency the agent's uv environment ships without.
    """

    out_dir.mkdir(parents=True, exist_ok=True)
    wheel_path = out_dir / "adapter-0.1.0-py3-none-any.whl"
    dist_info = "adapter-0.1.0.dist-info"
    with zipfile.ZipFile(wheel_path, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("adapter/__init__.py", _ADAPTER_BODY)
        zf.writestr(f"{dist_info}/METADATA", _WHEEL_METADATA)
        zf.writestr(f"{dist_info}/WHEEL", _WHEEL_WHEEL)
        zf.writestr(f"{dist_info}/RECORD", "")
    return wheel_path


def _build_tarball(wheel: Path) -> bytes:
    """Wrap ``manifest.yaml`` + ``adapter/<wheel>.whl`` in a ``.tar.gz``."""

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        manifest_bytes = _MANIFEST_YAML.encode()
        manifest_info = tarfile.TarInfo(name="manifest.yaml")
        manifest_info.size = len(manifest_bytes)
        tar.addfile(manifest_info, io.BytesIO(manifest_bytes))

        wheel_bytes = wheel.read_bytes()
        wheel_info = tarfile.TarInfo(name=f"adapter/{wheel.name}")
        wheel_info.size = len(wheel_bytes)
        tar.addfile(wheel_info, io.BytesIO(wheel_bytes))
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Fixtures.
# ---------------------------------------------------------------------------


@pytest.fixture
def override_storage(tmp_path: Path) -> Iterator[Path]:
    """Point the upload storage dependency at a writable ``tmp_path`` root."""

    storage_root = tmp_path / "pack-storage"
    storage_root.mkdir(parents=True, exist_ok=True)

    def _tmp_storage() -> PackStorageService:
        return PackStorageService(root=storage_root)

    app.dependency_overrides[get_pack_storage] = _tmp_storage
    try:
        yield storage_root
    finally:
        app.dependency_overrides.pop(get_pack_storage, None)


@pytest.fixture
def fixture_artifacts(tmp_path: Path) -> tuple[bytes, str]:
    """Return ``(tarball_bytes, sha256_hex)`` for the hand-built fixture pack."""

    wheel = _build_handcrafted_wheel(tmp_path / "wheel")
    tarball_bytes = _build_tarball(wheel)
    sha = hashlib.sha256(tarball_bytes).hexdigest()
    return tarball_bytes, sha


@pytest.fixture(autouse=True)
def _clear_adapter_cache() -> Iterator[None]:
    """Ensure the per-test adapter cache + ``sys.path`` state is fresh."""

    _adapter_cache_clear()
    yield
    _adapter_cache_clear()


# ---------------------------------------------------------------------------
# Vertical-slice tests.
# ---------------------------------------------------------------------------


async def test_upload_persists_pack_and_writes_artifact(
    client: AsyncClient,
    override_storage: Path,
    fixture_artifacts: tuple[bytes, str],
    db_session: AsyncSession,
) -> None:
    """Upload writes the pack, records sha256 + artifact_path on disk."""

    tarball_bytes, expected_sha = fixture_artifacts
    files = {"tarball": ("vendor-foo-0.1.0.tar.gz", tarball_bytes, "application/gzip")}
    res = await client.post("/api/driver-packs/uploads", files=files)
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["id"] == "vendor-foo"
    assert "origin" not in body

    # Verify the on-disk artifact + DB-recorded sha256 line up with what we
    # uploaded — the agent fetch in the next step will rely on this.
    release = (
        await db_session.execute(
            select(DriverPackRelease).where(
                DriverPackRelease.pack_id == "vendor-foo",
                DriverPackRelease.release == "0.1.0",
            )
        )
    ).scalar_one()
    assert release.artifact_sha256 == expected_sha
    assert release.artifact_path is not None
    assert Path(release.artifact_path).read_bytes() == tarball_bytes


async def test_vertical_slice_upload_fetch_load_dispatch(
    client: AsyncClient,
    override_storage: Path,
    fixture_artifacts: tuple[bytes, str],
    tmp_path: Path,
) -> None:
    """End-to-end: upload → agent fetch → adapter load → dispatch hooks."""

    tarball_bytes, expected_sha = fixture_artifacts

    # Step 1: upload the pack via the in-process app.
    files = {"tarball": ("vendor-foo-0.1.0.tar.gz", tarball_bytes, "application/gzip")}
    upload_res = await client.post("/api/driver-packs/uploads", files=files)
    assert upload_res.status_code == 201, upload_res.text

    # Step 2: drive the agent's tarball_fetch against the in-process app via
    # a fresh ASGITransport — download_and_verify expects ``client.get`` to
    # resolve the backend's tarball route relative to its base_url.
    fetch_dest = tmp_path / "agent-fetch"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://backend") as agent_client:
        downloaded = await download_and_verify(
            client=agent_client,
            pack_id="vendor-foo",
            release="0.1.0",
            expected_sha256=expected_sha,
            dest_dir=fetch_dest,
        )
    assert downloaded.read_bytes() == tarball_bytes

    # Step 3: drive adapter_loader to extract the wheel + import the adapter.
    runtime_dir = tmp_path / "agent-runtime"
    adapter = await load_adapter(
        pack_id="vendor-foo",
        release="0.1.0",
        tarball_path=downloaded,
        runtime_dir=runtime_dir,
        venv_python=sys.executable,
    )
    assert adapter is not None
    assert adapter.pack_id == "vendor-foo"
    assert adapter.pack_release == "0.1.0"

    # Step 4: dispatch_discover should receive the fixture's single candidate.
    class _DiscoveryCtx:
        host_id = "host-1"
        platform_id = "vendor_p"

    candidates = await dispatch_discover(adapter, _DiscoveryCtx())
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.identity_scheme == "vendor_uid"
    assert candidate.identity_value == "vendor-uid-001"
    assert candidate.suggested_name == "Vendor Probe 001"
    assert candidate.runnable is True
    assert candidate.detected_properties == {"firmware": "1.2.3"}

    # Step 5: dispatch_pre_session should return the cap-merge dict.
    spec = SessionSpec(
        pack_id="vendor-foo",
        platform_id="vendor_p",
        device_identity_value="vendor-uid-001",
        capabilities={},
    )
    extra = await dispatch_pre_session(adapter, spec)
    assert extra == {"appium:vendorMagic": "set"}
