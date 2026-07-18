# Driver Pack Tarball Upload Guide

GridFleet supports custom driver packs as tarballs — compressed archives that contain a `manifest.yaml` at the root and, optionally, an adapter wheel (`adapter/` directory). Uploaded packs are the path for custom Python code running on host machines, non-standard probe families, imperative pre-session capability logic, or portable pack distribution across multiple GridFleet instances.

## Scope and Prerequisites

**Scope.** This guide covers packs delivered as `.tar.gz` archives uploaded from the Drivers page, stored on the backend, fetched by agents, and loaded into adapter hooks at runtime. Read the Security Warning section before proceeding.

**What you need before starting:**

- **Admin role** in GridFleet. The upload endpoint requires admin credentials; non-admin requests are rejected with HTTP 403.
- A driver pack archive (`.tar.gz`, `.tgz`, or `.tar`) with `manifest.yaml` at the archive root.
- If the pack includes an adapter: a pure-Python wheel file (`adapter/<your_package>-py3-none-any.whl`) inside the archive.
- The adapter package must expose a class named `Adapter` in a top-level module also named `adapter` (i.e. importable as `from adapter import Adapter`).

## When to Use Upload

Use the upload path when:

| Scenario | Upload needed? |
|----------|---------------|
| Custom device discovery that needs an adapter `discover` hook | Yes |
| A manifest `lifecycle_actions` declaration, which requires the adapter's `lifecycle_action` hook | Yes |
| Imperative pre-session capability logic (capability values computed at session-request time) | Yes |
| Portable distribution across GridFleet instances | Yes |
| Simple discovery config, static capabilities, no custom code | Usually no — use a curated pack instead |

## Security Warning

**Adapter wheels execute Python code directly on every host machine where the pack is active.** There is no sandbox. An adapter can:

- Read and write the host filesystem.
- Spawn subprocesses.
- Make network calls.
- Access any credential or file accessible to the agent process.

Because of this, the upload endpoint is restricted to admin users and every upload is recorded as a `driver_pack.upload` event in the System Event stream. Agent tarball fetches verify the pinned sha256 locally and are not recorded in the System Event stream; a mismatch surfaces as a host install error (an agent-side `TarballSha256MismatchError`). Review the upload audit events regularly to confirm only authorized packs are in use.

Never upload adapter wheels sourced from untrusted parties without auditing their source code first.

## Tarball Anatomy

A valid driver pack archive must contain at minimum:

```
manifest.yaml          # required — the pack manifest
```

An archive with an adapter additionally contains:

```
manifest.yaml
adapter/
  <package>-py3-none-any.whl   # exactly one .whl file; pure-Python only
```

## Repository Fixture: Curated Roku as Uploaded Tarball

The repository does not check in binary `.tar.gz` artifacts. To exercise the
upload path with a real manifest, build a deterministic tarball from the
curated Roku manifest:

```bash
python scripts/build_driver_pack_tarball.py \
  --pack-dir driver-packs/curated/appium-roku-dlenroc \
  --out /tmp/appium-roku-dlenroc-upload.tar.gz \
  --id uploaded/appium-roku-dlenroc \
  --release 2026.04.0-upload
```

The generated archive contains `manifest.yaml` at the root and can be uploaded
from the Drivers page with **Upload Driver**.

### `manifest.yaml`

The manifest YAML is driver-agnostic and does not include an origin. The backend assigns origin from ingestion context: curated imports become `curated`, tarball uploads become `uploaded`. The full manifest schema is defined in `backend/app/packs/manifest.py`. Key top-level fields:

```yaml
id: "acme/my-custom-driver"        # required; no "local/" prefix for uploaded packs
display_name: "ACME Custom Driver"
release: "1.2.0"
requires:
  host_os: [linux, macos]          # optional; omit or leave empty for all host OSes
platforms:
  - id: my_platform
    identity:                      # device identity scheme/scope, at the platform top level
      scheme: my_identity_scheme
      scope: global                # "global" or "host"
    # ... rest of platform definition
```

There is no manifest `discovery` block (the `Platform` model uses `extra="forbid"`, so a `discovery:` key is rejected) — `identity` is a top-level platform field. Adapter-based discovery is selected by the presence of a loaded adapter for the platform, which implements the `discover` hook; it is not configured via a `discovery.kind` field.

Use `requires.host_os` for packs that can only run on specific agent operating systems. For example, Apple-only XCUITest packs should set `host_os: [macos]` so Linux agents do not install them during desired-state sync.

A pack **must** request the `session_discovery` insecure feature (e.g. `insecure_features: ["*:session_discovery"]`) for orphan-session reaping to work. The backend observation sweep enumerates a node's live Appium sessions via `GET /appium/sessions`, which Appium only exposes when the driver is started with `--allow-insecure …:session_discovery`. The agent appends `--allow-insecure` only for the features listed here. A pack that omits it silently disables orphan reaping: a leaked Appium session with no DB row can pin its device busy until the idle timeout (`grid.session_idle_timeout_sec`). Upload does not reject such a pack, but ingest logs a `pack_ingest_missing_session_discovery` warning.

### Adapter Wheel Constraints

The agent loads adapter wheels by extracting each archive member with `zipfile.ZipFile` via a path-traversal-guarded `_safe_extract_zip` (no `extractall`) into a runtime directory, followed by a dynamic `importlib` import. **pip is not involved.** This means:

- The wheel **must be pure-Python** (`py3-none-any` tag preferred).
- No compiled C extensions (no `.so`, `.pyd` files).
- No installable console scripts or entry-point metadata that requires pip install to activate.
- All dependencies your adapter needs must either be part of the Python standard library or already installed in the agent's virtual environment. The wheel's `RECORD` is not consulted for transitive deps.
- Only one `.whl` file is permitted in the `adapter/` directory. Multiple wheels are rejected by the agent at adapter load time (the host status panel shows an install error), not during server-side upload validation.

If your adapter needs third-party libraries, either vendor them into the wheel (include their source in your package) or coordinate with the host operator to pre-install them in the agent environment.

## Adapter Convention

### Package Structure

Your adapter must be a Python package named `adapter` with a class `Adapter` that implements the `DriverPackAdapter` Protocol defined in `agent/agent_app/pack/adapter_types.py`.

Minimal project layout:

```
my_adapter/
  adapter/
    __init__.py         # must export Adapter
    adapter.py          # or inline Adapter in __init__.py
  pyproject.toml        # or setup.py / setup.cfg
```

After building, `adapter/__init__.py` (or an import in it) must make `Adapter` importable as:

```python
from adapter import Adapter
```

**Intra-package imports must be relative** (`from .health import ...`, not `from adapter.health import ...`). The agent imports each adapter under a unique per-(pack, release) module name, so the literal name `adapter` never exists at runtime; the loader rejects wheels containing absolute self-imports at install time with an `adapter-internal imports must be relative` error.

**Hooks may be invoked concurrently.** Hook calls are not serialized — health checks, discovery, and session hooks for different devices (and different packs) can interleave on the agent's event loop. An adapter that needs to serialize access to shared state must hold its own `asyncio.Lock`.

### `DriverPackAdapter` Protocol

The protocol (from `agent_app/pack/adapter_types.py`) defines the hooks the agent may call. Only two are **required** — `discover` and `normalize_device`; implement only the optional hooks your pack needs. Curated adapters are plain classes that do **not** subclass the Protocol, so the agent probes each hook with a truthful `hasattr` check (`adapter_supports`): a missing optional hook is treated exactly like a pack that ships no adapter for that concern — the dispatch site takes its no-adapter branch (`None`, an empty capabilities dict, or an HTTP 404 `NO_ADAPTER`), never surfacing as an `AdapterHookExecutionError`. An exception a hook raises *while running* is still caught and re-raised as `AdapterHookExecutionError`, which aborts that operation. See **Minimal Packs: Three Tiers** below for the declare-it-then-implement-it rule that ties manifest declarations to adapter hooks.

```python
from typing import Any, Literal, Protocol

class DriverPackAdapter(Protocol):
    pack_id: str
    pack_release: str

    async def discover(self, ctx: DiscoveryContext) -> list[DiscoveryCandidate]: ...
    async def doctor(self, ctx: DoctorContext) -> list[DoctorCheckResult]: ...
    async def health_check(self, ctx: HealthContext) -> list[HealthCheckResult]: ...
    async def lifecycle_action(
        self,
        action_id: Literal["reconnect", "release_forwarded_ports"],
        args: dict[str, Any],
        ctx: LifecycleContext,
    ) -> LifecycleActionResult: ...
    async def pre_session(self, spec: SessionSpec) -> dict[str, Any]: ...
    async def post_session(self, spec: SessionSpec, outcome: SessionOutcome) -> None: ...
    async def normalize_device(self, ctx: NormalizeDeviceContext) -> NormalizedDevice: ...
    async def telemetry(self, ctx: TelemetryContext) -> HardwareTelemetry: ...
    def subprocess_env(self) -> SubprocessEnvContribution: ...
    def tool_versions(self) -> dict[str, str | None]: ...
```

The hooks are `async` and take typed dataclass/Protocol context objects (not a `context: dict`), returning dataclasses rather than raw dicts. The protocol is not `@runtime_checkable`. See `agent_app/pack/adapter_types.py` for the context and result dataclass definitions.

## Minimal Packs: Three Tiers

A pack scales its adapter surface to what it actually needs. There are three supported shapes, smallest first:

1. **Manifest-only (no adapter).** A `manifest.yaml` with platforms and static capabilities, and no `adapter/` wheel. Every adapter hook is treated as "no adapter loaded". Use this when static config is enough.
2. **Core two-hook adapter.** An `Adapter` implementing exactly the required core — `discover` and `normalize_device` — and nothing else. The pack gets custom device enumeration and canonicalization while every optional hook (health, lifecycle, sessions, telemetry) transparently degrades to the no-adapter branch. The end-to-end example is the agent test `agent/tests/pack/test_adapter_loader.py::test_minimal_two_hook_adapter_loads_and_dispatches`, which loads a real two-hook wheel and drives it through discover + normalize.
3. **Capability add-ons.** Start from the core two hooks and add optional hooks as the pack needs them — `health_check`/`doctor`, `lifecycle_action`, `pre_session`/`post_session`, `telemetry`. The curated adapters under `driver-packs/adapters/` are living examples of this tier.

### The declare-it-then-implement-it rule

If the manifest **declares** a capability, the adapter **must** carry its hook, or the pack is blocked at load:

| Manifest declaration | Required adapter hook |
|----------------------|-----------------------|
| a platform `lifecycle_actions` entry | `lifecycle_action` |

At adapter load the agent cross-checks these declarations against the loaded `Adapter` (`missing_declared_hooks`). A pack that declares a capability its adapter does not implement is reported `blocked`, with the missing hook named in `blocked_reason` — the same status pathway a runtime-resolution failure uses. (`health_check` has no manifest declaration, so it is never cross-checked: it is dispatched whenever an adapter is present and skipped when absent.)

## Hooks Dispatched in B.2

The following hooks are wired and dispatched as of Phase B.2:

| Hook | When called |
|------|-------------|
| `discover` | When the agent runs device discovery for a platform that has a loaded adapter |
| `doctor` | When a host doctor probe is triggered for this pack by the agent |
| `health_check` | On each periodic device health check cycle for a device governed by this pack |
| `lifecycle_action` | When a lifecycle action (`reconnect`, `release_forwarded_ports`) is dispatched for a device governed by this pack, or when the platform's `connection_behavior.host_resolution_action` resolves a transport identity (`resolve`) |
| `pre_session` | Immediately before an Appium session is started; return value is merged into the capability set |
| `post_session` | After an Appium session ends; return value is ignored (cleanup / telemetry hook) |

Return-value contracts:

- `discover` — returns `list[DiscoveryCandidate]`; the agent diffs against the current device registry.
- `doctor` — returns `list[DoctorCheckResult]` (fields `check_id`, `ok`, `message`).
- `health_check` — returns `list[HealthCheckResult]` (fields `check_id`, `ok`, `detail`, `recommended_action`, `debounce`).
- `lifecycle_action` — returns a `LifecycleActionResult` (fields `ok`, `detail`, `resolved_connection_target`; `state` is a legacy free-form field no longer used for power-state readout).
- `pre_session` — returns a capabilities dict (merged over the incoming caps; keys from the adapter take precedence).
- `post_session` — return value is ignored.

## Step-by-Step: Building and Uploading a Pack

### 1. Build the adapter package

Create your adapter package following the structure in the Adapter Convention section above.

```bash
# Example using hatchling or setuptools
cd my_adapter
pip install build
python -m build --wheel
```

This produces `dist/my_adapter-1.0.0-py3-none-any.whl`. Confirm the wheel tag is `py3-none-any` (no platform-specific compiled code).

### 2. Assemble the tarball

```bash
# Create working directory
mkdir pack_build
cp path/to/manifest.yaml pack_build/
mkdir pack_build/adapter
cp dist/my_adapter-1.0.0-py3-none-any.whl pack_build/adapter/

# Create the archive with manifest.yaml at root
cd pack_build
tar -czf ../my-custom-driver-1.2.0.tar.gz .
```

Verify the tarball structure before uploading:

```bash
tar -tzf my-custom-driver-1.2.0.tar.gz | head -20
# Expected output includes:
#   manifest.yaml
#   adapter/my_adapter-1.0.0-py3-none-any.whl
```

### 3. Upload via the UI

1. Navigate to **Drivers**.
2. Click **Upload Driver** in the top-right corner of the Drivers page.
3. In the upload form, select the `.tar.gz` file you assembled in step 2.
4. Confirm that the driver may execute Python code on host machines.
5. Click **Upload driver**. The backend validates the manifest and persists the tarball. On success, the pack appears in the Drivers list.

### 4. The pack is active

Uploaded packs are created in the `enabled` state and are active immediately — there is no separate enable step. Agents fetch the tarball on the next desired-state synchronisation cycle and install the adapter wheel into their runtime directory. An operator can later **Disable** (and re-**Enable**) the pack from the pack detail page.

### 5. (Optional) Upload via API

```bash
curl -X POST "http://localhost:8000/api/driver-packs/uploads" \
  -H "Authorization: Basic <base64 machine credentials>" \
  -F "tarball=@my-custom-driver-1.2.0.tar.gz"
```

The endpoint accepts only the `tarball` upload. The pack id and release are read from the `id`/`release` fields of `manifest.yaml` inside the tarball during ingest — they are not passed as form fields.

A successful upload returns HTTP 201 with a `PackOut` body. If the release already exists with a different sha256, the endpoint returns HTTP 409 (conflict).

## Troubleshooting

### HTTP 400 — Manifest validation error

The response body includes a `detail` field with the specific constraint that was violated. There are no `pack_id`/`release` form or query parameters on the upload endpoint, so there is no cross-check against the manifest; the id and release are read straight from `manifest.yaml`. Common causes:

- `manifest.yaml` fails schema validation (`ManifestValidationError`).
- The tarball is empty.
- `manifest.yaml` is missing at the archive root.
- The manifest declares a field that was removed from the schema in 2026-07 (WS-14.2): the top-level `doctor:` block and the platform `host_fields_schema` no longer exist, and `extra="forbid"` rejects any manifest still carrying them. No curated pack ever declared either; if an externally-built manifest does, delete the keys and re-build the tarball. Runtime doctor checks are unaffected — they are driven by the adapter's advertised `doctor` hook, never by the manifest.
- Required manifest fields are missing (`schema_version`, `id`, `release`, `display_name`, `appium_server`, `appium_driver`, `platforms`).
- The archive violates the member-count, size, or safe-path limits.

Fix the manifest and re-build the tarball before retrying.

### HTTP 409 — Release already exists with different sha256

A release with the same `pack_id` and `release` string was already uploaded and its sha256 does not match the new file. Release identifiers are immutable once stored: you cannot overwrite a release in place.

Fix: increment the `release` version in your `manifest.yaml`, re-build the tarball, and upload with the new release identifier. If you need to replace an accidentally corrupted upload, contact a system administrator to delete the existing release record from the database before re-uploading.

### Agent does not install the pack / host status shows install error

After upload and enable, agents fetch the tarball on the next desired-state sync (typically within 30 seconds, depending on the heartbeat interval). If the pack fails to install, the host's status panel will show an error under the pack entry. Common causes:

- **sha256 mismatch** — The tarball was modified in transit or storage. The agent verifies the pinned sha256 locally and raises `TarballSha256MismatchError`; this surfaces in the host's status panel and the agent logs (not in the System Event stream). Re-upload a fresh tarball.
- **Multiple wheel files in `adapter/`** — Only one `.whl` file is permitted. The loader will fail if it finds more than one. Re-build with a single wheel.
- **Compiled extension in wheel** — The loader extracts each member with `zipfile.ZipFile` (no `extractall`); compiled `.so`/`.pyd` files will extract but will fail to import if they were compiled for a different platform/Python version. Use pure-Python wheels only.
- **`Adapter` class not found** — After extraction, the loader imports the wheel's `adapter` package (under a unique per-pack module name) and looks up the `Adapter` class on it. Ensure your wheel's top-level package is named `adapter` (not your project name) and that `Adapter` is exported from `adapter/__init__.py`.
- **`adapter-internal imports must be relative`** — Your package imports itself absolutely (`from adapter.x import ...`). Switch intra-package imports to relative form (`from .x import ...`) and re-build the wheel.

### Audit event not appearing in System Events

The upload event (`driver_pack.upload`) is written to the System Event stream by `record_pack_upload` in `app.packs.services.ingest` (called via the `PackReleaseService.record_pack_upload` wrapper). Agent tarball fetches are not recorded as System Events. If the upload event is missing, check that:

- The upload completed with HTTP 201 (not a 4xx).
- The backend log does not show an exception in `record_pack_upload` (in `app.packs.services.ingest`).
- The System Events page is not filtered to exclude `driver_pack` category events.
