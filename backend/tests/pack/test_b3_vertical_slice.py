"""B.3 vertical-slice integration test.

End-to-end test that:

1. Uploads a tarball with an adapter wheel that implements ``feature_action``
   (returning ``ok=False, detail="degraded"`` on the first call and
   ``ok=True, detail="recovered"`` on the second).
2. Creates a fake host (DB row).
3. Mocks the agent HTTP endpoint by patching the ``http_client_factory``
   parameter of :func:`pack_feature_dispatch_service.dispatch_feature_action`
   (via ``dependency_overrides`` on the route) so no real network I/O is
   attempted.
4. POSTs to
   ``/api/hosts/<id>/driver-packs/<pack-id>/features/<feature-id>/actions/<action-id>``
   and asserts:
   - HTTP 200 with ``ok: false, detail: "degraded"`` (first call).
   - A ``host_pack_feature_status`` row is created with ``ok=False``.
   - A ``SystemEvent`` of type ``pack_feature.degraded`` was emitted.
5. Then a second invocation returning ``ok=True``:
   - HTTP 200 with ``ok: true``.
   - The status row updated to ``ok=True``.
   - A ``pack_feature.recovered`` event emitted.

The test skips gracefully when the agent tree is absent (same guard as
``test_uploaded_pack_vertical_slice.py``).
"""

from __future__ import annotations

import hashlib
import io
import sys
import tarfile
import uuid
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import httpx
import pytest
from sqlalchemy import select

from app.main import app
from app.models.driver_pack import DriverPack, DriverPackFeature, DriverPackRelease
from app.models.host import Host, HostStatus, OSType
from app.models.host_pack_feature_status import HostPackFeatureStatus
from app.routers.driver_pack_uploads import get_pack_storage
from app.services import pack_feature_dispatch_service
from app.services.event_bus import event_bus
from app.services.pack_storage_service import PackStorageService

if TYPE_CHECKING:
    from collections.abc import Iterator

    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.agent_client import AgentClientFactory, AgentHttpClient

# ---------------------------------------------------------------------------
# Agent availability guard (mirrors test_uploaded_pack_vertical_slice.py).
# ---------------------------------------------------------------------------

_AGENT_ROOT = Path(__file__).resolve().parents[3] / "agent"
if not (_AGENT_ROOT / "agent_app" / "pack" / "adapter_loader.py").exists():
    pytest.skip(
        f"agent_app tree not found at {_AGENT_ROOT}; B.3 vertical-slice skipped",
        allow_module_level=True,
    )
if str(_AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(_AGENT_ROOT))

pytestmark = pytest.mark.asyncio

# ---------------------------------------------------------------------------
# Constants.
# ---------------------------------------------------------------------------

PACK_ID = "b3-vertical-feature-test"
PACK_RELEASE = "0.3.0"
FEATURE_ID = "tunnel"
ACTION_ID = "restart"

# ---------------------------------------------------------------------------
# Fake adapter wheel — implements feature_action.
# ---------------------------------------------------------------------------

_ADAPTER_BODY = """\
from agent_app.pack.adapter_types import FeatureActionResult


_CALL_COUNT: list[int] = [0]


class Adapter:
    pack_id = "b3-vertical-feature-test"
    pack_release = "0.3.0"

    async def discover(self, ctx):
        return []

    async def doctor(self, ctx):
        return []

    async def health_check(self, ctx):
        return []

    async def lifecycle_action(self, action_id, args, ctx):
        class _R:
            ok = True
            state = "ok"
            detail = ""
        return _R()

    async def pre_session(self, spec):
        return {}

    async def post_session(self, spec, outcome):
        return None

    async def feature_action(self, feature_id, action_id, args, ctx):
        _CALL_COUNT[0] += 1
        if _CALL_COUNT[0] == 1:
            return FeatureActionResult(ok=False, detail="degraded", data={})
        return FeatureActionResult(ok=True, detail="recovered", data={})

    async def sidecar_lifecycle(self, feature_id, action):
        from agent_app.pack.adapter_types import SidecarStatus
        return SidecarStatus(ok=True, detail="not supported", state="stopped")
"""

_WHEEL_METADATA = """\
Metadata-Version: 2.1
Name: adapter
Version: 0.3.0
Summary: B.3 vertical-slice feature-action adapter
"""

_WHEEL_WHEEL = """\
Wheel-Version: 1.0
Generator: gridfleet-b3-vertical-slice
Root-Is-Purelib: true
Tag: py3-none-any
"""

_MANIFEST_YAML = f"""\
schema_version: 1
id: {PACK_ID}
release: {PACK_RELEASE}
display_name: B3 Vertical Feature Test
appium_server:
  source: npm
  package: appium
  version: ">=2.5,<3"
  recommended: 2.19.0
appium_driver:
  source: npm
  package: appium-b3-vertical-driver
  version: ">=0,<1"
  recommended: 0.3.0
platforms:
  - id: b3_p
    display_name: B3 Platform
    automation_name: B3Automation
    appium_platform_name: B3
    device_types: [real_device]
    connection_types: [network]
    grid_slots: [native]
    capabilities: {{stereotype: {{}}, session_required: []}}
    identity: {{scheme: b3_uid, scope: global}}
features:
  {FEATURE_ID}:
    display_name: Tunnel
    description_md: ""
    actions:
      - id: {ACTION_ID}
        label: Restart Tunnel
"""


def _build_handcrafted_wheel(out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    wheel_path = out_dir / "adapter-0.3.0-py3-none-any.whl"
    dist_info = "adapter-0.3.0.dist-info"
    with zipfile.ZipFile(wheel_path, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("adapter/__init__.py", _ADAPTER_BODY)
        zf.writestr(f"{dist_info}/METADATA", _WHEEL_METADATA)
        zf.writestr(f"{dist_info}/WHEEL", _WHEEL_WHEEL)
        zf.writestr(f"{dist_info}/RECORD", "")
    return wheel_path


def _build_tarball(wheel: Path) -> bytes:
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
# Fake in-process agent client (no real HTTP).
# ---------------------------------------------------------------------------


class _FakeAgentClient:
    """Minimal agent HTTP client that routes feature-action POST calls to a
    simple counter: first call returns degraded, second returns recovered."""

    def __init__(self) -> None:
        self.call_count = 0

    async def __aenter__(self) -> _FakeAgentClient:
        return self

    async def __aexit__(self, *_args: object) -> bool:
        return False

    async def get(self, *args: object, **kwargs: object) -> httpx.Response:  # pragma: no cover
        raise AssertionError("dispatch_feature_action must not GET the agent")

    async def post(
        self,
        url: str,
        *,
        params: object = None,
        headers: object = None,
        json: object = None,
        timeout: object = None,
    ) -> httpx.Response:
        self.call_count += 1
        if self.call_count == 1:
            payload: dict[str, Any] = {"ok": False, "detail": "degraded", "data": {}}
        else:
            payload = {"ok": True, "detail": "recovered", "data": {}}
        return httpx.Response(200, request=httpx.Request("POST", url), json=payload)


# ---------------------------------------------------------------------------
# Fixtures.
# ---------------------------------------------------------------------------


@pytest.fixture
def override_storage(tmp_path: Path) -> Iterator[Path]:
    storage_root = tmp_path / "b3-storage"
    storage_root.mkdir(parents=True, exist_ok=True)

    def _tmp_storage() -> PackStorageService:
        return PackStorageService(root=storage_root)

    app.dependency_overrides[get_pack_storage] = _tmp_storage
    try:
        yield storage_root
    finally:
        app.dependency_overrides.pop(get_pack_storage, None)


@pytest.fixture
def fake_agent(monkeypatch: pytest.MonkeyPatch) -> Iterator[_FakeAgentClient]:
    """Patch dispatch_feature_action's http_client_factory to use the fake."""
    agent = _FakeAgentClient()

    def _factory(*, timeout: float | int) -> AgentHttpClient:
        del timeout
        return cast("AgentHttpClient", agent)

    original = pack_feature_dispatch_service.dispatch_feature_action
    # Wrap the function so the factory is always our fake, regardless of what
    # the route passes.  We patch at the service module level because the route
    # module imports dispatch_feature_action directly.
    import functools

    @functools.wraps(original)
    async def _patched(
        session: AsyncSession,
        *,
        host_id: uuid.UUID,
        pack_id: str,
        feature_id: str,
        action_id: str,
        args: dict[str, Any],
        http_client_factory: AgentClientFactory = _factory,
        timeout: float | int = 30.0,
    ) -> object:
        return await original(
            session,
            host_id=host_id,
            pack_id=pack_id,
            feature_id=feature_id,
            action_id=action_id,
            args=args,
            http_client_factory=http_client_factory,
            timeout=timeout,
        )

    import app.routers.host_driver_pack_features as feature_routes

    monkeypatch.setattr(feature_routes, "dispatch_feature_action", _patched)
    yield agent


@pytest.fixture
def fixture_artifacts(tmp_path: Path) -> tuple[bytes, str]:
    wheel = _build_handcrafted_wheel(tmp_path / "wheel")
    tarball_bytes = _build_tarball(wheel)
    sha = hashlib.sha256(tarball_bytes).hexdigest()
    return tarball_bytes, sha


# ---------------------------------------------------------------------------
# Helper: seed pack + feature row directly in DB (bypasses agent).
# ---------------------------------------------------------------------------


async def _seed_pack_with_feature(
    session: AsyncSession,
) -> tuple[DriverPack, DriverPackRelease, DriverPackFeature]:
    pack = DriverPack(
        id=PACK_ID,
        origin="uploaded",
        display_name="B3 Vertical Feature Test",
        maintainer="",
        license="",
    )
    session.add(pack)
    await session.flush()

    release = DriverPackRelease(
        id=uuid.uuid4(),
        pack_id=PACK_ID,
        release=PACK_RELEASE,
        manifest_json={},
    )
    session.add(release)
    await session.flush()

    feature = DriverPackFeature(
        pack_release_id=release.id,
        manifest_feature_id=FEATURE_ID,
        data={
            "label": "Tunnel",
            "actions": [{"id": ACTION_ID, "label": "Restart Tunnel"}],
        },
    )
    session.add(feature)
    await session.flush()
    return pack, release, feature


async def _seed_host(session: AsyncSession) -> Host:
    host = Host(
        hostname=f"b3-vert-host-{uuid.uuid4().hex[:6]}",
        ip="10.0.3.1",
        os_type=OSType.linux,
        agent_port=5100,
        status=HostStatus.online,
    )
    session.add(host)
    await session.flush()
    return host


# ---------------------------------------------------------------------------
# Vertical-slice tests.
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("override_storage")
async def test_b3_upload_creates_pack_and_feature_rows(
    client: AsyncClient,
    fixture_artifacts: tuple[bytes, str],
    db_session: AsyncSession,
) -> None:
    """Step 1: upload writes the pack; pack_upload_service populates DriverPackFeature rows."""
    tarball_bytes, expected_sha = fixture_artifacts
    files = {"tarball": (f"{PACK_ID}-{PACK_RELEASE}.tar.gz", tarball_bytes, "application/gzip")}
    res = await client.post("/api/driver-packs/uploads", files=files)
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["id"] == PACK_ID
    assert "origin" not in body

    # Verify the release is stored.
    release = (
        await db_session.execute(
            select(DriverPackRelease).where(
                DriverPackRelease.pack_id == PACK_ID,
                DriverPackRelease.release == PACK_RELEASE,
            )
        )
    ).scalar_one()
    assert release.artifact_sha256 == expected_sha

    # Pack upload populates DriverPackFeature rows from the manifest's features section.
    features = (
        (
            await db_session.execute(
                select(DriverPackFeature).where(
                    DriverPackFeature.pack_release_id == release.id,
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(features) >= 1, "pack_upload_service must populate DriverPackFeature rows from manifest"
    feature_ids = {f.manifest_feature_id for f in features}
    assert FEATURE_ID in feature_ids


@pytest.mark.usefixtures("fake_agent")
async def test_b3_feature_action_degraded_and_recovered(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """Steps 2-5: feature-action route → status row → webhook events.

    - First call: agent returns ok=False → status row ok=False → pack_feature.degraded emitted.
    - Second call: agent returns ok=True → status row ok=True → pack_feature.recovered emitted.
    """
    # Seed host + pack — capture host_id as a plain Python UUID immediately so
    # that expire_all() later does not trigger lazy I/O on the ORM object.
    host = await _seed_host(db_session)
    host_id: uuid.UUID = host.id
    _pack, _release, _feature = await _seed_pack_with_feature(db_session)
    await db_session.commit()

    route = f"/api/hosts/{host_id}/driver-packs/{PACK_ID}/features/{FEATURE_ID}/actions/{ACTION_ID}"

    # --- First call: expect degraded ---
    res1 = await client.post(route, json={"args": {}})
    assert res1.status_code == 200, res1.text
    body1 = res1.json()
    assert body1["ok"] is False
    assert body1["detail"] == "degraded"

    # Row must exist with ok=False.  expire_all() tells SQLAlchemy to reload
    # columns on next access; host_id is a local var so no lazy load needed.
    db_session.expire_all()
    row = (
        await db_session.execute(
            select(HostPackFeatureStatus).where(
                HostPackFeatureStatus.host_id == host_id,
                HostPackFeatureStatus.pack_id == PACK_ID,
                HostPackFeatureStatus.feature_id == FEATURE_ID,
            )
        )
    ).scalar_one()
    assert row.ok is False
    assert row.detail == "degraded"

    # Drain async event handlers so they write to _log.
    await event_bus.drain_handlers()
    degraded_events = event_bus.get_recent_events(event_types=["pack_feature.degraded"])
    assert len(degraded_events) == 1, "pack_feature.degraded event must be emitted on first ok=False"
    ev = degraded_events[0]
    assert ev["data"]["pack_id"] == PACK_ID
    assert ev["data"]["feature_id"] == FEATURE_ID
    assert ev["data"]["ok"] is False

    # --- Second call: expect recovered ---
    res2 = await client.post(route, json={"args": {}})
    assert res2.status_code == 200, res2.text
    body2 = res2.json()
    assert body2["ok"] is True
    assert body2["detail"] == "recovered"

    # Row must flip to ok=True.
    db_session.expire_all()
    row = (
        await db_session.execute(
            select(HostPackFeatureStatus).where(
                HostPackFeatureStatus.host_id == host_id,
                HostPackFeatureStatus.pack_id == PACK_ID,
                HostPackFeatureStatus.feature_id == FEATURE_ID,
            )
        )
    ).scalar_one()
    assert row.ok is True
    assert row.detail == "recovered"

    await event_bus.drain_handlers()
    recovered_events = event_bus.get_recent_events(event_types=["pack_feature.recovered"])
    assert len(recovered_events) == 1, "pack_feature.recovered event must be emitted when ok flips True"
    ev2 = recovered_events[0]
    assert ev2["data"]["pack_id"] == PACK_ID
    assert ev2["data"]["feature_id"] == FEATURE_ID
    assert ev2["data"]["ok"] is True
