# Driver Pack Tool Dependencies — Design Spec

## Problem

The host detail overview page hardcodes `go_ios` in the "Tool Versions" section (backend schema, frontend component). Other driver pack dependencies like `adb`, `java`, and `xcodebuild` are either missing or only appear in a separate "Missing Prerequisites" warning section with hardcoded descriptions. There is no visual connection between a tool and the driver pack that requires it. This violates GridFleet's driver-agnostic principle.

## Goals

1. Make tool dependency display fully data-driven from driver pack manifests — no hardcoded tool names in backend or frontend.
2. Show which driver pack each dependency belongs to.
3. Always show tool descriptions so operators understand why each tool is installed.
4. Integrate the "Missing Prerequisites" concept into the new display (remove the separate warning section).

## Architecture: Separation of Concerns

| Layer | Responsibility |
|-------|---------------|
| **Manifest** | Declares *what* tools a pack needs and *why* (name + description) |
| **Adapter** | Provides *how* to detect them (version detection logic) |
| **Agent** | Combines manifest declarations with adapter detections into a structured response |
| **Backend** | Passthrough — forwards the agent response to the frontend |
| **Frontend** | Renders whatever the API returns, grouped by pack |

## Manifest Changes

Add `tool_dependencies` to the `Requires` section of driver pack manifests.

### Schema

```python
class ToolDependency(BaseModel):
    name: str
    description: str

class Requires(BaseModel):
    gridfleet: str | None = None
    node: str | None = None
    host_os: list[Literal["linux", "macos"]] = Field(default_factory=list)
    tool_dependencies: list[ToolDependency] = Field(default_factory=list)
```

### Manifest examples

```yaml
# appium-uiautomator2/manifest.yaml
requires:
  tool_dependencies:
    - name: adb
      description: "Communicates with Android devices over USB and TCP"
    - name: java
      description: "Required by UIAutomator2 test server build tools"

# appium-xcuitest/manifest.yaml
requires:
  tool_dependencies:
    - name: xcodebuild
      description: "Builds and tests iOS/tvOS apps via Xcode"
    - name: go_ios
      description: "iOS real-device battery and hardware telemetry"
```

## Adapter Changes

The adapter `tool_versions()` contract stays unchanged: `dict[str, str | None]`. Adapters detect all tools they know about and return name-to-version mappings. Descriptions are NOT the adapter's responsibility — they come from the manifest.

### Android adapter

Add `java` version detection alongside existing `adb` detection:

```python
def tool_versions(self) -> dict[str, str | None]:
    return {"adb": _detect_adb_version(), "java": _detect_java_version()}
```

### Apple adapter

No change to what it detects — already reports `xcodebuild` and `go_ios`.

## Agent Changes

`get_tool_status()` in `agent_app/tools/manager.py` produces a structured response keyed by source.

### Response shape

```json
{
  "host": {
    "node": {"name": "node", "version": "24.14.1", "description": "JavaScript runtime for Appium server"},
    "node_provider": {"name": "node_provider", "version": "fnm", "description": "Node.js version manager"}
  },
  "packs": {
    "appium-xcuitest": [
      {"name": "xcodebuild", "version": "16.2", "description": "Builds and tests iOS/tvOS apps via Xcode"},
      {"name": "go_ios", "version": "1.0.188", "description": "iOS real-device battery and hardware telemetry"}
    ],
    "appium-uiautomator2": [
      {"name": "adb", "version": "35.0.2", "description": "Communicates with Android devices over USB and TCP"},
      {"name": "java", "version": "17.0.9", "description": "Required by UIAutomator2 test server build tools"}
    ]
  }
}
```

### Merge logic

For each installed pack:
1. Read `tool_dependencies` from the pack's manifest (names + descriptions).
2. Call `adapter.tool_versions()` (returns `{name: version}`).
3. For each manifest-declared dependency, look up the version from the adapter result. If the adapter didn't report it, version is `null`.

Only manifest-declared tools appear in the response. Extra tools detected by the adapter but not declared in the manifest are ignored.

Host tool descriptions are static strings in the agent — `node` and `node_provider` are fixed host-level concerns.

## Backend Changes

### Schema

Replace the hardcoded `HostToolStatusRead`:

```python
class ToolEntry(BaseModel):
    name: str
    version: str | None = None
    description: str

class HostToolStatusRead(BaseModel):
    host: dict[str, ToolEntry]
    packs: dict[str, list[ToolEntry]]
```

### Route

The `GET /api/hosts/{host_id}/tools/status` endpoint remains a passthrough — calls the agent and returns the structured response. No tool-specific logic.

## Frontend Changes

### Host Tools card

Same position as the current "Tool Versions" card. Shows core infrastructure tools (node, node provider) in a horizontal grid. Each tool shows its label, version (monospace), and description (muted text below).

### Driver Pack Dependencies card

New section below Host Tools. Groups tools by pack_id.

Each pack gets a sub-header with its pack name, followed by its tools in a horizontal grid. Each tool shows:
- **Label** — tool name in uppercase
- **Version** — monospace, or warning-styled "not found" when `null`
- **Description** — muted text below the version, always visible

Empty states:
- No packs installed: "No driver packs installed."
- Pack has no `tool_dependencies`: pack does not appear in the section.
- Host offline: "Host must be online to read tool versions." (same as today)

### Removed

- The "Missing Prerequisites" warning section — its information is now integrated into the Driver Pack Dependencies card (a missing tool shows inline with warning styling).
- `frontend/src/lib/hostPrerequisites.ts` — hardcoded descriptions no longer needed.
- Hardcoded tool list in `HostToolVersionsPanel.tsx`.

## Edge Cases

- **`node_error`**: The current schema has a `node_error` field shown when node provider detection fails (e.g., "fnm not found"). In the new model, `node_provider` renders with `version: null` and warning-styled "not found" — same as any missing pack dependency. The specific error string is dropped.
- **Shared tools across packs**: If two packs both declare `adb` as a dependency, each pack shows its own `adb` entry with the same detected version. This is correct — each pack independently declares its dependency.
- **`missing_prerequisites` in host capabilities**: The `missing_prerequisites` list stored in `host.capabilities` is unchanged. It may be used in other views (host list, etc.). This spec only changes how tool information is displayed on the host detail overview page.

## What stays the same

- Adapter `tool_versions()` return type: `dict[str, str | None]` — no interface change.
- Agent `/agent/tools/status` endpoint path — unchanged.
- Backend `/api/hosts/{host_id}/tools/status` endpoint path — unchanged.
- Node/node provider detection logic in the agent — unchanged.
- The host detail page layout outside of these two cards — unchanged.
- `missing_prerequisites` stored in host capabilities — unchanged, only display changes.
