# Driver Pack Tool Dependencies Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the host detail overview's Tool Versions section data-driven from driver pack manifests instead of hardcoded, grouping tools by pack with always-visible descriptions.

**Architecture:** Manifests declare tool dependencies (name + description). Adapters detect versions. The agent merges manifest declarations with adapter detections into a structured `{host, packs}` response. Backend passes it through. Frontend renders dynamically.

**Tech Stack:** Python (Pydantic, FastAPI), React 19, TypeScript, Tailwind v4, Vitest, pytest

---

### Task 1: Add `ToolDependency` to manifest schema (backend)

**Files:**
- Modify: `backend/app/packs/manifest.py:258-265`
- Test: `backend/tests/pack/test_manifest_loader.py`

- [ ] **Step 1: Write failing test for `tool_dependencies` in manifest**

Add to `backend/tests/pack/test_manifest_loader.py`:

```python
def test_manifest_accepts_tool_dependencies() -> None:
    yaml_text = _valid_yaml().replace(
        "requires:\n          gridfleet: \">=1.7\"\n          host_os: [linux, macos]",
        "requires:\n          gridfleet: \">=1.7\"\n          host_os: [linux, macos]\n          tool_dependencies:\n            - name: adb\n              description: \"Communicates with Android devices over USB and TCP\"\n            - name: java\n              description: \"Required by UIAutomator2 test server build tools\"",
    )
    manifest = load_manifest_yaml(yaml_text)
    assert len(manifest.requires.tool_dependencies) == 2
    assert manifest.requires.tool_dependencies[0].name == "adb"
    assert manifest.requires.tool_dependencies[0].description == "Communicates with Android devices over USB and TCP"
    assert manifest.requires.tool_dependencies[1].name == "java"


def test_manifest_tool_dependencies_defaults_to_empty() -> None:
    manifest = load_manifest_yaml(_valid_yaml())
    assert manifest.requires.tool_dependencies == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && uv run pytest -q tests/pack/test_manifest_loader.py::test_manifest_accepts_tool_dependencies tests/pack/test_manifest_loader.py::test_manifest_tool_dependencies_defaults_to_empty -v`

Expected: FAIL — `tool_dependencies` not a valid field (extra="forbid" on Requires)

- [ ] **Step 3: Add `ToolDependency` model and update `Requires`**

In `backend/app/packs/manifest.py`, add before the `Requires` class (around line 258):

```python
class ToolDependency(BaseModel):
    """A host tool required by this driver pack."""

    model_config = ConfigDict(extra="forbid")

    name: str
    description: str
```

Update `Requires` to include `tool_dependencies`:

```python
class Requires(BaseModel):
    """Version requirements for runtime dependencies."""

    model_config = ConfigDict(extra="forbid")

    gridfleet: str | None = None
    node: str | None = None
    host_os: list[Literal["linux", "macos"]] = Field(default_factory=list)
    tool_dependencies: list[ToolDependency] = Field(default_factory=list)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && uv run pytest -q tests/pack/test_manifest_loader.py -v`

Expected: ALL PASS

- [ ] **Step 5: Run full backend checks**

Run: `cd backend && uv run ruff check app/packs/manifest.py && uv run mypy app/packs/manifest.py`

Expected: Clean

- [ ] **Step 6: Commit**

```bash
git add backend/app/packs/manifest.py backend/tests/pack/test_manifest_loader.py
git commit -m "feat(backend): add tool_dependencies to driver pack manifest schema"
```

---

### Task 2: Add `tool_dependencies` to curated manifests

**Files:**
- Modify: `driver-packs/curated/appium-uiautomator2/manifest.yaml`
- Modify: `driver-packs/curated/appium-xcuitest/manifest.yaml`

The Roku pack has no host tool dependencies — no changes needed there.

- [ ] **Step 1: Add tool_dependencies to appium-uiautomator2 manifest**

In `driver-packs/curated/appium-uiautomator2/manifest.yaml`, add `requires` section (this manifest currently has no `requires` block):

```yaml
requires:
  tool_dependencies:
    - name: adb
      description: "Communicates with Android devices over USB and TCP"
    - name: java
      description: "Required by UIAutomator2 test server build tools"
```

Add it after the `license:` line and before `appium_server:`.

- [ ] **Step 2: Add tool_dependencies to appium-xcuitest manifest**

In `driver-packs/curated/appium-xcuitest/manifest.yaml`, update existing `requires` block from:

```yaml
requires:
  host_os: [macos]
```

To:

```yaml
requires:
  host_os: [macos]
  tool_dependencies:
    - name: xcodebuild
      description: "Builds and tests iOS/tvOS apps via Xcode"
    - name: go_ios
      description: "iOS real-device battery and hardware telemetry"
```

- [ ] **Step 3: Verify manifests parse correctly**

Run: `cd backend && uv run python -c "from app.packs.manifest import load_manifest_yaml; import pathlib; m = load_manifest_yaml(pathlib.Path('../driver-packs/curated/appium-uiautomator2/manifest.yaml').read_text()); print(m.requires.tool_dependencies)"`

Run: `cd backend && uv run python -c "from app.packs.manifest import load_manifest_yaml; import pathlib; m = load_manifest_yaml(pathlib.Path('../driver-packs/curated/appium-xcuitest/manifest.yaml').read_text()); print(m.requires.tool_dependencies)"`

Expected: Both print the expected list of ToolDependency objects.

- [ ] **Step 4: Commit**

```bash
git add driver-packs/curated/appium-uiautomator2/manifest.yaml driver-packs/curated/appium-xcuitest/manifest.yaml
git commit -m "feat(backend): add tool_dependencies to curated driver pack manifests"
```

---

### Task 3: Add java detection to android adapter

**Files:**
- Modify: `driver-packs/adapters/android/adapter/__init__.py:107-122`
- Test: `driver-packs/adapters/android/tests/test_adapter.py`

- [ ] **Step 1: Write failing test for java detection**

Add to `driver-packs/adapters/android/tests/test_adapter.py`:

```python
def test_tool_versions_returns_java_version() -> None:
    adb_result = type("R", (), {"stdout": "Android Debug Bridge version 1.0.41\n...", "returncode": 0})()
    java_result = type("R", (), {"stdout": 'openjdk version "17.0.9" 2023-10-17\n...', "returncode": 0})()

    with patch("subprocess.run", side_effect=[adb_result, java_result]):
        result = Adapter().tool_versions()

    assert result["adb"] == "1.0.41"
    assert result["java"] == "17.0.9"


def test_tool_versions_returns_none_when_java_missing() -> None:
    adb_result = type("R", (), {"stdout": "Android Debug Bridge version 1.0.41\n...", "returncode": 0})()

    with patch("subprocess.run", side_effect=[adb_result, FileNotFoundError()]):
        result = Adapter().tool_versions()

    assert result["adb"] == "1.0.41"
    assert result["java"] is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd driver-packs/adapters/android && uv run pytest tests/test_adapter.py::test_tool_versions_returns_java_version tests/test_adapter.py::test_tool_versions_returns_none_when_java_missing -v`

Expected: FAIL — `java` key not in result

- [ ] **Step 3: Add java detection to android adapter `tool_versions()`**

Replace the `tool_versions` method in `driver-packs/adapters/android/adapter/__init__.py:107-122`:

```python
    def tool_versions(self) -> dict[str, str | None]:
        import re
        import subprocess

        from adapter.tools import find_adb

        versions: dict[str, str | None] = {"adb": None, "java": None}

        adb = find_adb()
        try:
            result = subprocess.run(
                [adb, "--version"], capture_output=True, text=True, timeout=5
            )
            match = re.search(r"(\d+\.\d+\.\d+)", result.stdout)
            if match:
                versions["adb"] = match.group(1)
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            pass

        try:
            result = subprocess.run(
                ["java", "-version"], capture_output=True, text=True, timeout=5
            )
            combined = result.stdout + result.stderr
            match = re.search(r'"(\d+\.\d+\.\d+)', combined)
            if match:
                versions["java"] = match.group(1)
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            pass

        return versions
```

Note: `java -version` outputs to stderr in most JDK implementations, so we check both stdout and stderr.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd driver-packs/adapters/android && uv run pytest tests/test_adapter.py -v`

Expected: ALL PASS

- [ ] **Step 5: Run adapter checks**

Run: `cd driver-packs/adapters/android && uv run ruff check adapter/ tests/ && uv run mypy adapter/`

Expected: Clean

- [ ] **Step 6: Commit**

```bash
git add driver-packs/adapters/android/adapter/__init__.py driver-packs/adapters/android/tests/test_adapter.py
git commit -m "feat(agent): add java version detection to android adapter"
```

---

### Task 4: Pass `tool_dependencies` through desired state to agent

The backend already includes `"requires": manifest.get("requires", {})` in the desired payload (`backend/app/packs/services/desired_state.py:58`). The agent's `DesiredPack` dataclass needs a `tool_dependencies` field, and `parse_desired_payload` needs to extract it from `requires`.

**Files:**
- Modify: `agent/agent_app/pack/manifest.py:43-51` (DesiredPack dataclass)
- Modify: `agent/agent_app/pack/manifest.py:79-98` (parse_desired_payload)
- Test: `agent/tests/test_tools_manager.py` (will verify in Task 5)

- [ ] **Step 1: Add `ToolDependency` dataclass and update `DesiredPack`**

In `agent/agent_app/pack/manifest.py`, add before `DesiredPack` (around line 42):

```python
@dataclass(frozen=True)
class ToolDependency:
    name: str
    description: str
```

Update `DesiredPack` to include tool_dependencies:

```python
@dataclass
class DesiredPack:
    id: str
    release: str
    appium_server: AppiumInstallable
    appium_driver: AppiumInstallable
    platforms: list[DesiredPlatform]
    features: list[DesiredFeature] = field(default_factory=list)
    runtime_policy: RuntimePolicy = field(default_factory=RuntimePolicy)
    tarball_sha256: str | None = None
    tool_dependencies: list[ToolDependency] = field(default_factory=list)
```

- [ ] **Step 2: Update `parse_desired_payload` to extract `tool_dependencies`**

In the `parse_desired_payload` function, update the `DesiredPack` construction (around line 83):

```python
        requires = raw.get("requires") or {}
        tool_deps = [
            ToolDependency(name=td["name"], description=td["description"])
            for td in (requires.get("tool_dependencies") or [])
        ]
        packs.append(
            DesiredPack(
                id=raw["id"],
                release=raw["release"],
                appium_server=_installable(raw["appium_server"]),
                appium_driver=_installable(raw["appium_driver"]),
                platforms=[_platform(p) for p in raw["platforms"]],
                features=_features(raw.get("features") or {}),
                runtime_policy=_runtime_policy(raw.get("runtime_policy") or {"strategy": "recommended"}),
                tarball_sha256=raw.get("tarball_sha256"),
                tool_dependencies=tool_deps,
            )
        )
```

- [ ] **Step 3: Run agent checks**

Run: `cd agent && uv run ruff check agent_app/pack/manifest.py && uv run mypy agent_app/pack/manifest.py`

Expected: Clean

- [ ] **Step 4: Commit**

```bash
git add agent/agent_app/pack/manifest.py
git commit -m "feat(agent): parse tool_dependencies from desired pack payload"
```

---

### Task 5: Restructure agent `get_tool_status()` response

**Files:**
- Modify: `agent/agent_app/tools/manager.py:236-267`
- Modify: `agent/agent_app/tools/schemas.py`
- Modify: `agent/agent_app/tools/dependencies.py`
- Test: `agent/tests/test_tools_manager.py`

- [ ] **Step 1: Write failing tests for new response shape**

Replace the adapter-related tests in `agent/tests/test_tools_manager.py` and add new ones:

```python
from agent_app.pack.manifest import DesiredPack, ToolDependency
from agent_app.pack.runtime_types import AppiumInstallable, RuntimePolicy


def _stub_desired_pack(
    pack_id: str,
    tool_deps: list[ToolDependency],
) -> DesiredPack:
    return DesiredPack(
        id=pack_id,
        release="1.0",
        appium_server=AppiumInstallable(
            source="npm", package="appium", version="==3.0.0",
        ),
        appium_driver=AppiumInstallable(
            source="npm", package="appium-fake", version="==1.0.0",
        ),
        platforms=[],
        tool_dependencies=tool_deps,
    )


async def test_get_tool_status_structured_response() -> None:
    class FakeAdapter:
        pack_id = "test-pack"
        pack_release = "1.0"

        def tool_versions(self) -> dict[str, str | None]:
            return {"go_ios": "1.0.207", "xcodebuild": "16.2"}

    registry = AdapterRegistry()
    registry.set("test-pack", "1.0", FakeAdapter())  # type: ignore[arg-type]

    desired = [
        _stub_desired_pack("test-pack", [
            ToolDependency(name="go_ios", description="iOS telemetry"),
            ToolDependency(name="xcodebuild", description="Xcode build tools"),
        ]),
    ]

    with (
        patch("agent_app.tools.manager.detect_node_provider", new_callable=AsyncMock, return_value=None),
        patch("agent_app.tools.manager._get_node_version", new_callable=AsyncMock, return_value="20.0.0"),
    ):
        status = await get_tool_status(adapter_registry=registry, desired_packs=desired)

    assert status["host"]["node"]["version"] == "20.0.0"
    assert status["host"]["node"]["name"] == "node"
    assert status["host"]["node_provider"]["version"] is None

    assert len(status["packs"]["test-pack"]) == 2
    go_ios_entry = next(e for e in status["packs"]["test-pack"] if e["name"] == "go_ios")
    assert go_ios_entry["version"] == "1.0.207"
    assert go_ios_entry["description"] == "iOS telemetry"


async def test_get_tool_status_missing_tool_version_is_null() -> None:
    class FakeAdapter:
        pack_id = "test-pack"
        pack_release = "1.0"

        def tool_versions(self) -> dict[str, str | None]:
            return {"go_ios": None}

    registry = AdapterRegistry()
    registry.set("test-pack", "1.0", FakeAdapter())  # type: ignore[arg-type]

    desired = [
        _stub_desired_pack("test-pack", [
            ToolDependency(name="go_ios", description="iOS telemetry"),
            ToolDependency(name="xcodebuild", description="Xcode build tools"),
        ]),
    ]

    with (
        patch("agent_app.tools.manager.detect_node_provider", new_callable=AsyncMock, return_value=None),
        patch("agent_app.tools.manager._get_node_version", new_callable=AsyncMock, return_value=None),
    ):
        status = await get_tool_status(adapter_registry=registry, desired_packs=desired)

    pack_tools = status["packs"]["test-pack"]
    go_ios_entry = next(e for e in pack_tools if e["name"] == "go_ios")
    xcode_entry = next(e for e in pack_tools if e["name"] == "xcodebuild")
    assert go_ios_entry["version"] is None
    assert xcode_entry["version"] is None


async def test_get_tool_status_no_packs_returns_empty_packs() -> None:
    with (
        patch("agent_app.tools.manager.detect_node_provider", new_callable=AsyncMock, return_value=None),
        patch("agent_app.tools.manager._get_node_version", new_callable=AsyncMock, return_value=None),
    ):
        status = await get_tool_status(desired_packs=[])

    assert status["packs"] == {}
    assert "host" in status
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd agent && uv run pytest tests/test_tools_manager.py::test_get_tool_status_structured_response tests/test_tools_manager.py::test_get_tool_status_missing_tool_version_is_null tests/test_tools_manager.py::test_get_tool_status_no_packs_returns_empty_packs -v`

Expected: FAIL — `get_tool_status` doesn't accept `desired_packs` parameter

- [ ] **Step 3: Rewrite `get_tool_status()` with new structured response**

Replace `get_tool_status()` in `agent/agent_app/tools/manager.py:236-267`:

```python
async def get_tool_status(
    *,
    adapter_registry: AdapterRegistry | None = None,
    desired_packs: list[DesiredPack] | None = None,
) -> dict[str, Any]:
    provider = await detect_node_provider()
    if provider and provider.bin_paths:
        _prepend_process_path(provider.bin_paths)
    node_version = await _get_node_version(provider)

    host: dict[str, dict[str, Any]] = {
        "node": {
            "name": "node",
            "version": node_version,
            "description": "JavaScript runtime for Appium server",
        },
        "node_provider": {
            "name": "node_provider",
            "version": provider.name if provider and not provider.error else None,
            "description": "Node.js version manager",
        },
    }

    packs: dict[str, list[dict[str, Any]]] = {}
    if adapter_registry is not None and desired_packs:
        for pack in desired_packs:
            if not pack.tool_dependencies:
                continue
            adapter = adapter_registry.get_current(pack.id)
            detected: dict[str, str | None] = {}
            if adapter is not None and hasattr(adapter, "tool_versions"):
                result = adapter.tool_versions()
                if inspect.isawaitable(result):
                    result = await result
                detected = result

            packs[pack.id] = [
                {
                    "name": dep.name,
                    "version": detected.get(dep.name),
                    "description": dep.description,
                }
                for dep in pack.tool_dependencies
            ]

    return {"host": host, "packs": packs}
```

Also add the import at the top of the file (with the other TYPE_CHECKING imports):

```python
if TYPE_CHECKING:
    from agent_app.pack.adapter_registry import AdapterRegistry
    from agent_app.pack.manifest import DesiredPack
```

- [ ] **Step 4: Update `get_tool_status_dep` to pass desired_packs**

In `agent/agent_app/tools/dependencies.py`, update to pass desired packs from the pack state loop:

```python
async def get_tool_status_dep(request: Request) -> dict[str, Any]:
    registry = getattr(request.app.state, "adapter_registry", None)
    pack_state_loop = getattr(request.app.state, "pack_state_loop", None)
    desired_packs = pack_state_loop.latest_desired_packs if pack_state_loop else None
    return await get_tool_status(adapter_registry=registry, desired_packs=desired_packs)
```

- [ ] **Step 5: Update existing tests to match new response shape**

Update `test_get_tool_status_returns_nulls_for_absent_tools`:

```python
async def test_get_tool_status_returns_nulls_for_absent_tools() -> None:
    with (
        patch("agent_app.tools.manager.detect_node_provider", new_callable=AsyncMock, return_value=None),
        patch("agent_app.tools.manager._get_node_version", new_callable=AsyncMock, return_value=None),
    ):
        status = await get_tool_status()

    assert status["host"]["node"]["version"] is None
    assert status["host"]["node_provider"]["version"] is None
    assert status["packs"] == {}
```

Update `test_get_tool_status_includes_adapter_tool_versions` — this test used the old flat shape. Replace with:

```python
async def test_get_tool_status_includes_adapter_tool_versions() -> None:
    class FakeAdapter:
        pack_id = "test-pack"
        pack_release = "1.0"

        def tool_versions(self) -> dict[str, str | None]:
            return {"go_ios": "1.0.207"}

    registry = AdapterRegistry()
    registry.set("test-pack", "1.0", FakeAdapter())  # type: ignore[arg-type]

    desired = [
        _stub_desired_pack("test-pack", [
            ToolDependency(name="go_ios", description="iOS telemetry"),
        ]),
    ]

    with (
        patch("agent_app.tools.manager.detect_node_provider", new_callable=AsyncMock, return_value=None),
        patch("agent_app.tools.manager._get_node_version", new_callable=AsyncMock, return_value=None),
    ):
        status = await get_tool_status(adapter_registry=registry, desired_packs=desired)

    pack_tools = status["packs"]["test-pack"]
    assert pack_tools[0]["version"] == "1.0.207"
```

Update `test_get_tool_status_with_provider_error`:

```python
async def test_get_tool_status_with_provider_error() -> None:
    provider = NodeProvider(
        name="fnm",
        node_path=None,
        npm_path=None,
        error="node_not_configured",
        bin_paths=["/fnm/bin"],
    )
    with (
        patch("agent_app.tools.manager.detect_node_provider", new_callable=AsyncMock, return_value=provider),
        patch("agent_app.tools.manager._get_node_version", new_callable=AsyncMock, return_value=None),
    ):
        status = await get_tool_status()

    assert status["host"]["node_provider"]["version"] is None
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `cd agent && uv run pytest tests/test_tools_manager.py -v`

Expected: ALL PASS

- [ ] **Step 7: Run agent checks**

Run: `cd agent && uv run ruff check agent_app/tools/ && uv run mypy agent_app/tools/`

Expected: Clean

- [ ] **Step 8: Commit**

```bash
git add agent/agent_app/tools/manager.py agent/agent_app/tools/dependencies.py agent/tests/test_tools_manager.py
git commit -m "feat(agent): restructure tool status response with host/packs grouping"
```

---

### Task 6: Update agent `ToolsStatusResponse` schema

The agent's `ToolsStatusResponse` schema is used for OpenAPI generation, which the backend uses for validation. It needs to reflect the new response shape.

**Files:**
- Modify: `agent/agent_app/tools/schemas.py`

- [ ] **Step 1: Update `ToolsStatusResponse`**

Replace `agent/agent_app/tools/schemas.py`:

```python
"""Response schemas for ``/agent/tools/*``."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ToolEntry(BaseModel):
    name: str
    version: str | None = None
    description: str


class ToolsStatusResponse(BaseModel):
    """Detected versions of supporting CLI tools, grouped by host and pack."""

    model_config = ConfigDict(extra="forbid")

    host: dict[str, ToolEntry]
    packs: dict[str, list[ToolEntry]]
```

- [ ] **Step 2: Run agent checks**

Run: `cd agent && uv run ruff check agent_app/tools/schemas.py && uv run mypy agent_app/tools/schemas.py`

Expected: Clean

- [ ] **Step 3: Verify the endpoint still works with the new schema**

Run: `cd agent && uv run pytest tests/test_tools_manager.py -v`

Expected: ALL PASS

- [ ] **Step 4: Commit**

```bash
git add agent/agent_app/tools/schemas.py
git commit -m "feat(agent): update ToolsStatusResponse schema for structured tool status"
```

---

### Task 7: Update backend `HostToolStatusRead` schema

**Files:**
- Modify: `backend/app/hosts/schemas.py:152-156`
- Modify: `backend/app/hosts/router.py:308` (remove `response_model_exclude_none`)
- Test: `backend/tests/test_openapi_response_models.py`
- Test: `backend/tests/test_router_units_more.py`

- [ ] **Step 1: Replace `HostToolStatusRead`**

In `backend/app/hosts/schemas.py`, replace lines 152-156:

```python
class ToolEntry(BaseModel):
    name: str
    version: str | None = None
    description: str


class HostToolStatusRead(BaseModel):
    host: dict[str, ToolEntry]
    packs: dict[str, list[ToolEntry]]
```

- [ ] **Step 2: Remove `response_model_exclude_none` from the route**

In `backend/app/hosts/router.py:308`, change:

```python
@router.get("/{host_id}/tools/status", response_model=HostToolStatusRead, response_model_exclude_none=True)
```

To:

```python
@router.get("/{host_id}/tools/status", response_model=HostToolStatusRead)
```

- [ ] **Step 3: Update backend tests**

In `backend/tests/test_router_units_more.py`, update the mock return values for `get_agent_tool_status` around line 1093 to use the new shape:

```python
patch("app.hosts.router.get_agent_tool_status", new=AsyncMock(return_value={"host": {}, "packs": {}})),
```

In `backend/tests/test_openapi_response_models.py`, the test at line 55 should still pass as-is since we're keeping the same class name.

- [ ] **Step 4: Run backend checks**

Run: `cd backend && uv run pytest tests/test_openapi_response_models.py tests/test_router_units_more.py -v -k "tool"`

Expected: PASS

Run: `cd backend && uv run ruff check app/hosts/schemas.py app/hosts/router.py && uv run mypy app/hosts/schemas.py app/hosts/router.py`

Expected: Clean

- [ ] **Step 5: Commit**

```bash
git add backend/app/hosts/schemas.py backend/app/hosts/router.py backend/tests/test_router_units_more.py
git commit -m "feat(backend): update HostToolStatusRead schema for structured tool status"
```

---

### Task 8: Regenerate frontend OpenAPI types

After backend schema changes, the generated OpenAPI types must be refreshed.

**Files:**
- Modify: `frontend/src/api/openapi.ts` (auto-generated)
- Modify: `frontend/src/types/hosts.ts`

- [ ] **Step 1: Start the backend and regenerate types**

The backend needs to be running for type generation. Start Postgres and the backend:

```bash
cd docker && docker compose up -d postgres
cd ../backend && uv run alembic upgrade head && uv run uvicorn app.main:app --port 8000 &
sleep 3
cd ../frontend && npm run types:generate
```

- [ ] **Step 2: Verify the generated `HostToolStatusRead` type**

Inspect the generated `frontend/src/api/openapi.ts` — it should now contain nested `ToolEntry` and updated `HostToolStatusRead` types matching the new schema.

- [ ] **Step 3: Update `HostToolStatus` type alias if needed**

In `frontend/src/types/hosts.ts`, the existing type:

```typescript
export type HostToolStatus = Schemas['HostToolStatusRead'];
```

Should continue to work since the schema name hasn't changed — only the shape.

- [ ] **Step 4: Run type check**

Run: `cd frontend && npx tsc --noEmit`

Expected: Errors in `HostToolVersionsPanel.tsx` and tests (they reference the old shape) — this is expected and will be fixed in Task 9.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/api/openapi.ts frontend/src/types/hosts.ts
git commit -m "chore(frontend): regenerate openapi types for structured tool status"
```

---

### Task 9: Rewrite `HostToolVersionsPanel` frontend component

**Files:**
- Modify: `frontend/src/components/hostDetail/HostToolVersionsPanel.tsx`
- Delete: `frontend/src/lib/hostPrerequisites.ts`
- Test: `frontend/src/components/hostDetail/HostToolVersionsPanel.test.tsx`
- Test: `frontend/src/components/hostDetail/HostOverviewPanel.test.tsx`

- [ ] **Step 1: Write test for new component**

Replace `frontend/src/components/hostDetail/HostToolVersionsPanel.test.tsx`:

```typescript
import { render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { vi } from 'vitest';
import { HostToolVersionsPanel } from './HostToolVersionsPanel';
import type { HostRead } from '../../types';

vi.mock('../../hooks/useHosts', () => ({
  useHostToolStatus: () => ({
    data: {
      host: {
        node: { name: 'node', version: '24.14.1', description: 'JavaScript runtime for Appium server' },
        node_provider: { name: 'node_provider', version: 'fnm', description: 'Node.js version manager' },
      },
      packs: {
        'appium-xcuitest': [
          { name: 'xcodebuild', version: '16.2', description: 'Builds and tests iOS/tvOS apps via Xcode' },
          { name: 'go_ios', version: '1.0.188', description: 'iOS real-device battery and hardware telemetry' },
        ],
        'appium-uiautomator2': [
          { name: 'adb', version: '35.0.2', description: 'Communicates with Android devices over USB and TCP' },
          { name: 'java', version: null, description: 'Required by UIAutomator2 test server build tools' },
        ],
      },
    },
    isLoading: false,
    error: null,
  }),
}));

vi.mock('../../hooks/useSettings', () => ({
  useSettings: () => ({ data: [] }),
}));

const host = {
  id: 'host-1',
  hostname: 'local-host',
  ip: '127.0.0.1',
  os_type: 'macos',
  agent_port: 5100,
  status: 'online',
  capabilities: {},
  missing_prerequisites: [],
  created_at: '2026-05-12T00:00:00Z',
  updated_at: '2026-05-12T00:00:00Z',
} as HostRead;

function renderPanel() {
  const client = new QueryClient();
  return render(
    <QueryClientProvider client={client}>
      <HostToolVersionsPanel host={host} />
    </QueryClientProvider>,
  );
}

test('renders host tools section with node and node provider', () => {
  renderPanel();
  expect(screen.getByText('Host Tools')).toBeInTheDocument();
  expect(screen.getByText('NODE')).toBeInTheDocument();
  expect(screen.getByText('24.14.1')).toBeInTheDocument();
  expect(screen.getByText('NODE_PROVIDER')).toBeInTheDocument();
  expect(screen.getByText('fnm')).toBeInTheDocument();
});

test('renders driver pack dependencies grouped by pack', () => {
  renderPanel();
  expect(screen.getByText('Driver Pack Dependencies')).toBeInTheDocument();
  expect(screen.getByText('appium-xcuitest')).toBeInTheDocument();
  expect(screen.getByText('appium-uiautomator2')).toBeInTheDocument();
  expect(screen.getByText('XCODEBUILD')).toBeInTheDocument();
  expect(screen.getByText('16.2')).toBeInTheDocument();
  expect(screen.getByText('GO_IOS')).toBeInTheDocument();
  expect(screen.getByText('1.0.188')).toBeInTheDocument();
  expect(screen.getByText('ADB')).toBeInTheDocument();
  expect(screen.getByText('35.0.2')).toBeInTheDocument();
});

test('shows descriptions for all tools', () => {
  renderPanel();
  expect(screen.getByText('JavaScript runtime for Appium server')).toBeInTheDocument();
  expect(screen.getByText('Builds and tests iOS/tvOS apps via Xcode')).toBeInTheDocument();
  expect(screen.getByText('iOS real-device battery and hardware telemetry')).toBeInTheDocument();
});

test('shows warning for missing tools', () => {
  renderPanel();
  expect(screen.getByText('not found')).toBeInTheDocument();
});
```

- [ ] **Step 2: Rewrite `HostToolVersionsPanel.tsx`**

Replace `frontend/src/components/hostDetail/HostToolVersionsPanel.tsx`:

```tsx
import { AlertTriangle } from 'lucide-react';
import { useHostToolStatus } from '../../hooks/useHosts';
import { Card } from '../ui/Card';
import type { HostRead, HostToolStatus } from '../../types';

type ToolEntry = {
  name: string;
  version: string | null;
  description: string;
};

type Props = {
  host: HostRead;
};

function ToolCell({ tool }: { tool: ToolEntry }) {
  const missing = !tool.version;
  return (
    <div className="px-5 py-4">
      <div className="text-xs font-medium uppercase text-text-3">{tool.name}</div>
      <div className={`mt-1 font-mono text-sm ${missing ? 'flex items-center gap-1 text-warning-foreground' : 'text-text-1'}`}>
        {missing ? (
          <>
            <AlertTriangle size={14} />
            <span>not found</span>
          </>
        ) : (
          tool.version
        )}
      </div>
      <div className="mt-1 text-xs text-text-3">{tool.description}</div>
    </div>
  );
}

export function HostToolVersionsPanel({ host }: Props) {
  const hostId = host.id;
  const hostOnline = host.status === 'online';
  const { data: toolStatus, isLoading: toolsLoading } = useHostToolStatus(hostId, hostOnline);

  const offlineMessage = (
    <p className="px-5 py-8 text-center text-sm text-text-3">Host must be online to read tool versions.</p>
  );

  const hostTools = toolStatus?.host ? Object.values(toolStatus.host) : [];
  const packEntries = toolStatus?.packs ? Object.entries(toolStatus.packs) : [];
  const hasPackDeps = packEntries.some(([, tools]) => tools.length > 0);

  return (
    <div className="space-y-6">
      <Card padding="none">
        <div className="border-b border-border px-5 py-4">
          <h2 className="text-sm font-medium text-text-2">Host Tools</h2>
        </div>
        {!hostOnline ? offlineMessage : toolsLoading ? (
          <p className="px-5 py-8 text-center text-sm text-text-3">Loading tool versions...</p>
        ) : !toolStatus ? (
          <p className="px-5 py-8 text-center text-sm text-text-3">Tool versions are currently unavailable.</p>
        ) : (
          <div className="grid grid-cols-1 divide-y divide-border md:grid-cols-2 md:divide-x md:divide-y-0">
            {hostTools.map((tool) => (
              <ToolCell key={tool.name} tool={tool} />
            ))}
          </div>
        )}
      </Card>

      <Card padding="none">
        <div className="border-b border-border px-5 py-4">
          <h2 className="text-sm font-medium text-text-2">Driver Pack Dependencies</h2>
        </div>
        {!hostOnline ? offlineMessage : toolsLoading ? (
          <p className="px-5 py-8 text-center text-sm text-text-3">Loading tool versions...</p>
        ) : !toolStatus ? (
          <p className="px-5 py-8 text-center text-sm text-text-3">Tool versions are currently unavailable.</p>
        ) : !hasPackDeps ? (
          <p className="px-5 py-8 text-center text-sm text-text-3">No driver packs installed.</p>
        ) : (
          <div className="divide-y divide-border">
            {packEntries.map(([packId, tools]) =>
              tools.length > 0 ? (
                <div key={packId}>
                  <div className="px-5 pt-4 pb-2">
                    <span className="font-mono text-sm font-medium text-text-2">{packId}</span>
                  </div>
                  <div className="grid grid-cols-1 divide-y divide-border/50 md:grid-cols-2 md:divide-x md:divide-y-0">
                    {tools.map((tool) => (
                      <ToolCell key={tool.name} tool={tool} />
                    ))}
                  </div>
                </div>
              ) : null,
            )}
          </div>
        )}
      </Card>
    </div>
  );
}
```

- [ ] **Step 3: Delete `hostPrerequisites.ts`**

Remove `frontend/src/lib/hostPrerequisites.ts` — its hardcoded descriptions are no longer needed.

Verify no other files import it:

```bash
grep -rn "hostPrerequisites" frontend/src/ --include="*.ts" --include="*.tsx"
```

Expected: only the now-deleted component references it.

- [ ] **Step 4: Update `HostOverviewPanel.test.tsx`**

In `frontend/src/components/hostDetail/HostOverviewPanel.test.tsx`, update the `useHostToolStatus` mock:

```typescript
  useHostToolStatus: () => ({
    data: {
      host: {
        node: { name: 'node', version: '24.14.1', description: 'JavaScript runtime for Appium server' },
        node_provider: { name: 'node_provider', version: 'fnm', description: 'Node.js version manager' },
      },
      packs: {},
    },
    isLoading: false,
    error: null,
  }),
```

Update the test assertion at line 76-77 from:

```typescript
  expect(screen.getByText('Tool Versions')).toBeInTheDocument();
```

To:

```typescript
  expect(screen.getByText('Host Tools')).toBeInTheDocument();
```

- [ ] **Step 5: Run frontend tests**

Run: `cd frontend && npm run test -- --run src/components/hostDetail/HostToolVersionsPanel.test.tsx src/components/hostDetail/HostOverviewPanel.test.tsx`

Expected: ALL PASS

- [ ] **Step 6: Run lint and type check**

Run: `cd frontend && npm run lint && npx tsc --noEmit`

Expected: Clean

- [ ] **Step 7: Commit**

```bash
git add frontend/src/components/hostDetail/HostToolVersionsPanel.tsx frontend/src/components/hostDetail/HostToolVersionsPanel.test.tsx frontend/src/components/hostDetail/HostOverviewPanel.test.tsx
git rm frontend/src/lib/hostPrerequisites.ts
git commit -m "feat(frontend): data-driven tool versions grouped by driver pack"
```

---

### Task 10: Verify end-to-end and run full test suites

**Files:** None (verification only)

- [ ] **Step 1: Run full backend test suite**

Run: `cd backend && uv run pytest -q -n auto`

Expected: ALL PASS, coverage >= 98%

- [ ] **Step 2: Run full agent test suite**

Run: `cd agent && uv run pytest -q`

Expected: ALL PASS

- [ ] **Step 3: Run full frontend test suite**

Run: `cd frontend && npm run test -- --run && npm run lint && npx tsc --noEmit`

Expected: ALL PASS

- [ ] **Step 4: Check for agent schema drift**

The pre-commit hook "agent schema drift check" verifies the backend's generated agent schemas match the agent's actual OpenAPI. Since we changed `ToolsStatusResponse` in Task 6, this will likely fail. Fix by running the schema sync script referenced in the pre-commit config, then committing the regenerated `backend/app/agent_comm/generated.py`.

- [ ] **Step 5: Start the dev stack and verify visually**

Run:
```bash
cd docker && docker compose up --build -d
```

Open the host detail overview page in a browser. Verify:
1. "Host Tools" card shows Node and Node Provider with descriptions
2. "Driver Pack Dependencies" card shows tools grouped by pack name
3. Each tool shows its description
4. Missing tools show warning-styled "not found"
5. The old "Missing Prerequisites" warning section is gone

- [ ] **Step 6: Final commit if any fixups needed**

If any adjustments were needed during verification, commit them.
