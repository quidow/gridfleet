"""Tests for the agent feature-action route and sidecar status in the status payload.

Tests cover:
- POST /agent/pack/features/{feature_id}/actions/{action_id} — happy path,
  404 when adapter absent, and error propagation.
- The status payload built by PackStateLoop.run_once() gains a top-level
  ``sidecars`` key from the SidecarSupervisor's snapshot.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal, cast

import pytest
from httpx import ASGITransport, AsyncClient

from agent_app.main import app
from agent_app.pack.adapter_registry import AdapterRegistry
from agent_app.pack.adapter_types import DriverPackAdapter, FeatureActionResult, SidecarStatus
from agent_app.pack.sidecar_supervisor import SidecarSupervisor

if TYPE_CHECKING:
    from agent_app.pack.runtime import RuntimeEnv, RuntimeSpec

# ---------------------------------------------------------------------------
# Fake adapter
# ---------------------------------------------------------------------------


class _FakeAdapter:
    """Fake adapter that returns a scripted FeatureActionResult."""

    pack_id = "vendor-fake"
    pack_release = "1.0.0"

    def __init__(self, result: FeatureActionResult | None = None) -> None:
        self._result = result or FeatureActionResult(ok=True, detail="done", data={"key": "value"})
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    async def feature_action(
        self,
        feature_id: str,
        action_id: str,
        args: dict[str, Any],
        ctx: object,
    ) -> FeatureActionResult:
        self.calls.append((feature_id, action_id, args))
        return self._result

    async def discover(self, ctx: object) -> list[object]:  # pragma: no cover
        return []

    async def doctor(self, ctx: object) -> list[object]:  # pragma: no cover
        return []

    async def health_check(self, ctx: object) -> list[object]:  # pragma: no cover
        return []

    async def lifecycle_action(self, action_id: object, args: object, ctx: object) -> object:  # pragma: no cover
        return None

    async def pre_session(self, spec: object) -> dict[str, object]:  # pragma: no cover
        return {}

    async def post_session(self, spec: object, outcome: object) -> None:  # pragma: no cover
        return None

    async def sidecar_lifecycle(
        self, feature_id: str, action: Literal["start", "stop", "status"]
    ) -> object:  # pragma: no cover
        return None


# ---------------------------------------------------------------------------
# Helper: override app.state.adapter_registry for each test
# ---------------------------------------------------------------------------


@pytest.fixture()
def registry_with_fake_adapter() -> AdapterRegistry:
    """Returns a fresh registry with the fake adapter pre-loaded."""
    registry = AdapterRegistry()
    adapter = _FakeAdapter()
    registry.set("vendor-fake", "1.0.0", cast("DriverPackAdapter", adapter))
    return registry


@pytest.fixture()
def empty_registry() -> AdapterRegistry:
    """Returns a fresh registry with no adapters loaded."""
    return AdapterRegistry()


# ---------------------------------------------------------------------------
# Feature-action route tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_feature_action_route_happy_path(registry_with_fake_adapter: AdapterRegistry) -> None:
    """POST /agent/pack/features/{fid}/actions/{aid} returns FeatureActionResult as JSON."""
    app.state.adapter_registry = registry_with_fake_adapter
    app.state.host_identity = None

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/agent/pack/features/tunnel/actions/check",
            json={"pack_id": "vendor-fake", "args": {"foo": "bar"}},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["detail"] == "done"
    assert body["data"] == {"key": "value"}


@pytest.mark.asyncio
async def test_feature_action_route_returns_404_when_adapter_absent(empty_registry: AdapterRegistry) -> None:
    """POST returns 404 when no adapter is loaded for the given pack_id."""
    app.state.adapter_registry = empty_registry
    app.state.host_identity = None

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/agent/pack/features/tunnel/actions/check",
            json={"pack_id": "no-such-pack", "args": {}},
        )

    assert resp.status_code == 404
    assert "no-such-pack" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_feature_action_route_passes_args_to_adapter(registry_with_fake_adapter: AdapterRegistry) -> None:
    """The route forwards args from the request body to the adapter."""
    adapter = registry_with_fake_adapter.get_current("vendor-fake")
    assert adapter is not None
    fake_adapter = cast("_FakeAdapter", adapter)
    app.state.adapter_registry = registry_with_fake_adapter
    app.state.host_identity = None

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await client.post(
            "/agent/pack/features/my-feature/actions/my-action",
            json={"pack_id": "vendor-fake", "args": {"x": 1, "y": 2}},
        )

    assert len(fake_adapter.calls) == 1
    feature_id, action_id, args = fake_adapter.calls[0]
    assert feature_id == "my-feature"
    assert action_id == "my-action"
    assert args == {"x": 1, "y": 2}


@pytest.mark.asyncio
async def test_feature_action_route_uses_host_identity_for_context(
    registry_with_fake_adapter: AdapterRegistry,
) -> None:
    """The route uses host_id from host_identity when available."""
    from agent_app.pack.host_identity import HostIdentity

    host_identity = HostIdentity()
    host_identity.set("test-host-123")
    app.state.adapter_registry = registry_with_fake_adapter
    app.state.host_identity = host_identity

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/agent/pack/features/tunnel/actions/check",
            json={"pack_id": "vendor-fake", "args": {}},
        )

    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_feature_action_route_missing_pack_id_field() -> None:
    """POST with missing pack_id returns 422 validation error."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/agent/pack/features/tunnel/actions/check",
            json={"args": {}},
        )

    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Status payload includes sidecars key
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_status_payload_includes_sidecars_key() -> None:
    """PackStateLoop.run_once() includes a top-level ``sidecars`` key in the posted payload."""
    from agent_app.pack.state import PackStateLoop

    class _FakeStateClient:
        def __init__(self) -> None:
            self.posted: list[dict[str, Any]] = []

        async def fetch_desired(self) -> dict[str, Any]:
            return {
                "host_id": "00000000-0000-0000-0000-000000000001",
                "packs": [],
            }

        async def post_status(self, payload: dict[str, Any]) -> None:
            self.posted.append(payload)

    class _FakeRuntimeMgr:
        async def reconcile(
            self, desired_by_pack: dict[str, RuntimeSpec]
        ) -> tuple[dict[str, RuntimeEnv], dict[str, str]]:
            return {}, {}

    supervisor = SidecarSupervisor()
    client = _FakeStateClient()
    loop = PackStateLoop(
        client=client,
        runtime_mgr=_FakeRuntimeMgr(),
        host_id="00000000-0000-0000-0000-000000000001",
        sidecar_supervisor=supervisor,
    )
    await loop.run_once()

    assert len(client.posted) == 1
    payload = client.posted[0]
    assert "sidecars" in payload
    assert isinstance(payload["sidecars"], list)


@pytest.mark.asyncio
async def test_status_payload_sidecars_reflects_supervisor_snapshot() -> None:
    """sidecars in the status payload mirrors the supervisor's status_snapshot()."""
    from agent_app.pack.state import PackStateLoop

    class _FakeStateClient:
        def __init__(self) -> None:
            self.posted: list[dict[str, Any]] = []

        async def fetch_desired(self) -> dict[str, Any]:
            return {
                "host_id": "00000000-0000-0000-0000-000000000001",
                "packs": [],
            }

        async def post_status(self, payload: dict[str, Any]) -> None:
            self.posted.append(payload)

    class _FakeRuntimeMgr:
        async def reconcile(
            self, desired_by_pack: dict[str, RuntimeSpec]
        ) -> tuple[dict[str, RuntimeEnv], dict[str, str]]:
            return {}, {}

    class _FakeSidecarAdapter:
        pack_id = "vendor-sidecar"
        pack_release = "1.0.0"

        async def sidecar_lifecycle(self, feature_id: str, action: Literal["start", "stop", "status"]) -> SidecarStatus:
            return SidecarStatus(ok=True, detail="running", state="running")

        async def discover(self, ctx: object) -> list[object]:  # pragma: no cover
            return []

        async def doctor(self, ctx: object) -> list[object]:  # pragma: no cover
            return []

        async def health_check(self, ctx: object) -> list[object]:  # pragma: no cover
            return []

        async def lifecycle_action(self, action_id: object, args: object, ctx: object) -> object:  # pragma: no cover
            return None

        async def pre_session(self, spec: object) -> dict[str, object]:  # pragma: no cover
            return {}

        async def post_session(self, spec: object, outcome: object) -> None:  # pragma: no cover
            return None

        async def feature_action(
            self, _feature_id: str, _action_id: str, args: dict[str, object], ctx: object
        ) -> object:  # pragma: no cover
            return None

    supervisor = SidecarSupervisor(poll_interval_seconds=10.0)
    fake_adapter = _FakeSidecarAdapter()
    await supervisor.start(
        pack_id="vendor-sidecar",
        release="1.0.0",
        feature_id="tunnel",
        adapter=fake_adapter,  # type: ignore[arg-type]
    )

    client = _FakeStateClient()
    loop = PackStateLoop(
        client=client,
        runtime_mgr=_FakeRuntimeMgr(),
        host_id="00000000-0000-0000-0000-000000000001",
        sidecar_supervisor=supervisor,
    )
    await loop.run_once()

    payload = client.posted[0]
    assert "sidecars" in payload
    sidecars = payload["sidecars"]
    assert len(sidecars) == 1
    assert sidecars[0]["feature_id"] == "tunnel"
    assert sidecars[0]["ok"] is True

    await supervisor.shutdown()


@pytest.mark.asyncio
async def test_status_payload_sidecars_empty_when_no_supervisor() -> None:
    """When no supervisor is provided, sidecars defaults to []."""
    from agent_app.pack.state import PackStateLoop

    class _FakeStateClient:
        def __init__(self) -> None:
            self.posted: list[dict[str, Any]] = []

        async def fetch_desired(self) -> dict[str, Any]:
            return {
                "host_id": "00000000-0000-0000-0000-000000000001",
                "packs": [],
            }

        async def post_status(self, payload: dict[str, Any]) -> None:
            self.posted.append(payload)

    class _FakeRuntimeMgr:
        async def reconcile(
            self, desired_by_pack: dict[str, RuntimeSpec]
        ) -> tuple[dict[str, RuntimeEnv], dict[str, str]]:
            return {}, {}

    client = _FakeStateClient()
    # No sidecar_supervisor argument → should default to empty sidecars
    loop = PackStateLoop(
        client=client,
        runtime_mgr=_FakeRuntimeMgr(),
        host_id="00000000-0000-0000-0000-000000000001",
    )
    await loop.run_once()

    payload = client.posted[0]
    assert "sidecars" in payload
    assert payload["sidecars"] == []
