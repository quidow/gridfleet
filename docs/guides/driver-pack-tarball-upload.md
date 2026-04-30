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
| Custom probe family not in `{manual, network_endpoint, adb, apple_devicectl, roku_ecp}` | Yes |
| Any `*_hook` field in the manifest (`sidecar.adapter_hook`, `actions[].adapter_hook`, `doctor[].adapter_hook`) | Yes |
| Imperative pre-session capability logic (capability values computed at session-request time) | Yes |
| Long-running sidecar service alongside the Appium node | Yes (B.3) |
| Portable distribution across GridFleet instances | Yes |
| Simple discovery config, static capabilities, no custom code | Usually no — create from a curated template instead |

Curated templates are a lighter starting point for manifest-only packs. They still create uploaded packs internally, using the same tarball validation and storage path as manual uploads.

Sidecar CI coverage verifies manifest upload, feature persistence,
desired-state serialization, and agent dispatch contracts. It does not start
an arbitrary long-running host process in unit tests; live-agent validation
remains part of release smoke testing.

## Security Warning

**Adapter wheels execute Python code directly on every host machine where the pack is active.** There is no sandbox. An adapter can:

- Read and write the host filesystem.
- Spawn subprocesses.
- Make network calls.
- Access any credential or file accessible to the agent process.

Because of this, the upload endpoint is restricted to admin users and every upload is recorded as a `driver_pack.upload` event in the System Event stream. Every tarball fetch by an agent is recorded as `driver_pack.tarball_fetched`. Review audit events regularly to confirm only authorized packs are in use.

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

The manifest YAML is driver-agnostic and does not include an origin. The backend assigns origin from ingestion context: curated imports become `curated`, tarball uploads and template forks become `uploaded`. The full manifest schema is defined in `backend/app/pack/manifest.py`. Key top-level fields:

```yaml
id: "acme/my-custom-driver"        # required; no "local/" prefix for uploaded packs
display_name: "ACME Custom Driver"
release: "1.2.0"
requires:
  host_os: [linux, macos]          # optional; omit or leave empty for all host OSes
platforms:
  - id: my_platform
    discovery:
      kind: adapter                # "adapter" is only valid for uploaded packs
      identity:
        scheme: my_identity_scheme
        scope: global
    # ... rest of platform definition
```

Uploaded packs may use `discovery.kind: adapter` when the tarball carries an adapter wheel. They may also use the standard probe families (`adb`, `apple_devicectl`, etc.) in their manifest if the adapter handles a different responsibility (for example, pre-session logic only).

Use `requires.host_os` for packs that can only run on specific agent operating systems. For example, Apple-only XCUITest packs should set `host_os: [macos]` so Linux agents do not install them during desired-state sync.

### Adapter Wheel Constraints

The agent loads adapter wheels using `zipfile.extractall` (hand-rolled extraction into a runtime directory) followed by a dynamic `importlib` import. **pip is not involved.** This means:

- The wheel **must be pure-Python** (`py3-none-any` tag preferred).
- No compiled C extensions (no `.so`, `.pyd` files).
- No installable console scripts or entry-point metadata that requires pip install to activate.
- All dependencies your adapter needs must either be part of the Python standard library or already installed in the agent's virtual environment. The wheel's `RECORD` is not consulted for transitive deps.
- Only one `.whl` file is permitted in the `adapter/` directory. Multiple wheels are rejected during upload validation.

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

### `DriverPackAdapter` Protocol

The protocol (from `agent_app/pack/adapter_types.py`) defines the hooks the agent may call. All hooks are optional; implement only the ones your pack needs. The loader checks for attribute existence before calling a hook — missing attributes raise `NotImplementedError` in the dispatch layer, which is logged and treated as a no-op (it does not abort the operation).

```python
from typing import Protocol, runtime_checkable, Any

@runtime_checkable
class DriverPackAdapter(Protocol):
    def discover(self, *, context: dict[str, Any]) -> list[dict[str, Any]]: ...
    def doctor(self, *, context: dict[str, Any]) -> list[dict[str, Any]]: ...
    def health_check(self, *, device_id: str, context: dict[str, Any]) -> dict[str, Any]: ...
    def lifecycle_action(self, *, device_id: str, action: str, context: dict[str, Any]) -> dict[str, Any]: ...
    def pre_session(self, *, device_id: str, capabilities: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]: ...
    def post_session(self, *, device_id: str, session_id: str, context: dict[str, Any]) -> None: ...
```

All arguments are keyword-only. The `context` dict carries pack metadata and agent-level configuration that the dispatch layer injects (pack id, release, agent config, etc.).

## Hooks Dispatched in B.2

The following hooks are wired and dispatched as of Phase B.2:

| Hook | When called |
|------|-------------|
| `discover` | When the agent runs device discovery for a platform whose `discovery.kind` is `adapter` |
| `doctor` | When a host doctor probe is triggered for this pack by the agent |
| `health_check` | On each periodic device health check cycle for a device governed by this pack |
| `lifecycle_action` | When a lifecycle action (`state`, `reconnect`, `boot`, `shutdown`) is dispatched for a device governed by this pack |
| `pre_session` | Immediately before an Appium session is started; return value is merged into the capability set |
| `post_session` | After an Appium session ends; return value is ignored (cleanup / telemetry hook) |

Return-value contracts:

- `discover` — returns a list of device dicts; the agent diffs against the current device registry.
- `doctor` — returns a list of `{check_id, ok, message}` dicts.
- `health_check` — returns a `{ok, reason}` dict.
- `lifecycle_action` — returns a `{ok, message}` dict.
- `pre_session` — returns a capabilities dict (merged over the incoming caps; keys from the adapter take precedence).
- `post_session` — return value is ignored.

## Hooks Deferred to B.3 and Later

The following dispatch paths exist in the protocol but are **not wired** in B.2. Calling them will raise `NotImplementedError` in the dispatch layer:

| Hook | Phase |
|------|-------|
| `feature_action` | B.3 — route `POST /api/hosts/{id}/driver-packs/{pack}/features/{feature}/actions/{action}` and corresponding UI not yet built |
| `sidecar_lifecycle` | B.3 — supervisor for long-running sidecar processes not yet implemented |

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

### 4. Enable the pack

After upload, the pack is in `draft` state. Navigate to the pack detail and click **Enable** to activate it. Agents will fetch the tarball on the next desired-state synchronisation cycle and install the adapter wheel into their runtime directory.

### 5. (Optional) Upload via API

```bash
curl -X POST "http://localhost:8000/api/driver-packs/uploads" \
  -H "Authorization: Basic <base64 machine credentials>" \
  -F "pack_id=acme/my-custom-driver" \
  -F "release=1.2.0" \
  -F "tarball=@my-custom-driver-1.2.0.tar.gz"
```

A successful upload returns HTTP 201 with a `DriverPackReleaseOut` body. If the release already exists with a different sha256, the endpoint returns HTTP 409 (conflict).

## Troubleshooting

### HTTP 400 — Manifest validation error

The tarball was accepted by the server but the `manifest.yaml` inside failed schema validation. The response body includes a `detail` field with the specific constraint that was violated. Common causes:

- `id` in `manifest.yaml` does not match the `pack_id` query parameter sent with the upload.
- `release` in `manifest.yaml` does not match the `release` query parameter.
- Required fields missing (`id`, `release`, `display_name`, or `platforms`).

Fix the manifest and re-build the tarball before retrying.

### HTTP 409 — Release already exists with different sha256

A release with the same `pack_id` and `release` string was already uploaded and its sha256 does not match the new file. Release identifiers are immutable once stored: you cannot overwrite a release in place.

Fix: increment the `release` version in your `manifest.yaml`, re-build the tarball, and upload with the new release identifier. If you need to replace an accidentally corrupted upload, contact a system administrator to delete the existing release record from the database before re-uploading.

### Agent does not install the pack / host status shows install error

After upload and enable, agents fetch the tarball on the next desired-state sync (typically within 30 seconds, depending on the heartbeat interval). If the pack fails to install, the host's status panel will show an error under the pack entry. Common causes:

- **sha256 mismatch** — The tarball was modified in transit or storage. Check the `driver_pack.tarball_fetched` event in the System Event log; a mismatch is recorded there. Re-upload a fresh tarball.
- **Multiple wheel files in `adapter/`** — Only one `.whl` file is permitted. The loader will fail if it finds more than one. Re-build with a single wheel.
- **Compiled extension in wheel** — The loader uses `zipfile.extractall`; compiled `.so`/`.pyd` files will extract but will fail to import if they were compiled for a different platform/Python version. Use pure-Python wheels only.
- **`Adapter` class not found** — After extraction, the loader does `from adapter import Adapter`. Ensure your wheel's top-level package is named `adapter` (not your project name) and that `Adapter` is exported from `adapter/__init__.py`.

### Audit event not appearing in System Events

Upload and fetch events (`driver_pack.upload`, `driver_pack.tarball_fetched`) are written to the System Event stream by `PackAuditService`. If events are missing, check that:

- The upload completed with HTTP 201 (not a 4xx).
- The backend log does not show an exception in `pack_audit_service.record_pack_upload`.
- The System Events page is not filtered to exclude `driver_pack` category events.
