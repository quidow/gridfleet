# Driver Pack Templates, Feature Actions, Sidecars, and Export Guide

This guide covers GridFleet driver pack templates, feature actions, sidecar management, export-to-tarball, and the diverged-from-upstream badge.

## Scope and Prerequisites

**Scope.** Templates are curated manifest recipes that create uploaded packs through the same tarball ingestion path as manual uploads. Feature actions and sidecars require an uploaded pack whose adapter wheel implements the relevant hooks. See `docs/guides/driver-pack-tarball-upload.md` for archive format and adapter security details.

**What you need before starting:**

- **Admin role** in GridFleet. All template, export, and feature-action endpoints require admin credentials; non-admin requests are rejected with HTTP 403.
- Familiarity with driver pack manifests (schema in `backend/app/pack/manifest.py`).
- For feature actions and sidecars: an uploaded pack whose adapter wheel implements the `feature_action` and/or `sidecar_lifecycle` hooks.

---

## Templates

### What Templates Are

Templates are curated YAML recipes that live under `driver-packs/templates/` by default. Set `GRIDFLEET_DRIVER_PACK_TEMPLATES_DIR` to point the backend at a different template directory. Each template is a complete, validated manifest that targets a specific platform configuration — for example, Android real-device testing or iOS real-device testing. Templates are read from disk and cached in memory for the process lifetime.

A template is not a driver pack itself. It is a recipe that an operator turns into an uploaded pack by choosing a pack id and release. The backend builds a tarball from the template and ingests it through the shared upload pipeline.

Each template YAML carries a `template_metadata:` top-level block (stripped before manifest validation) with these keys:

| Key | Purpose |
|-----|---------|
| `id` | Unique template identifier (e.g. `appium-uiautomator2-android-real`) |
| `display_name` | Human-readable name returned by the template descriptor API |
| `target_driver_summary` | One-line description returned by the template descriptor API (e.g. `"Android real device — UiAutomator2"`) |
| `prerequisite_host_tools` | List of host tools the template requires (e.g. `["adb"]`) |

### Shipped Templates

GridFleet ships two templates under `driver-packs/templates/`:

| Template ID | File | Target |
|-------------|------|--------|
| `appium-uiautomator2-android-real` | `appium-uiautomator2/templates/android-real.yaml` | Android real device — UiAutomator2; discovery via `adb`; identity scheme `android_serial` |
| `appium-xcuitest-ios-real` | `appium-xcuitest/templates/ios-real.yaml` | iOS real device — XCUITest; discovery via `apple_devicectl`; identity scheme `apple_udid` |

Both templates produce a manifest with a single platform entry and no adapter wheel. If your target device class requires an adapter (for example, a sidecar or custom probe), upload a full tarball instead.

### How to Use Templates

The current UI exposes direct driver-pack archive upload from the Drivers page. Template creation remains available through the admin API for scripted or internal workflows.

Use `POST /api/driver-packs/from-template/{template_id}` to create a pack from a template:

```bash
curl -X POST "http://localhost:8000/api/driver-packs/from-template/appium-uiautomator2-android-real" \
  -H "Authorization: Basic <base64 machine credentials>" \
  -H "Content-Type: application/json" \
  -d '{"pack_id": "acme/my-android", "release": "1.0.0", "display_name": "My Android Driver"}'
```

A successful call returns HTTP 201 with a `DriverPackOut` body.

### Listing Templates via API

```bash
curl "http://localhost:8000/api/driver-packs/templates" \
  -H "Authorization: Basic <base64 machine credentials>"
```

Returns a list of template descriptors:

```json
[
  {
    "template_id": "appium-uiautomator2-android-real",
    "source_pack_id": "appium-uiautomator2",
    "display_name": "Android Real Device (UiAutomator2)",
    "target_driver_summary": "Android real device — UiAutomator2",
    "prerequisite_host_tools": ["adb"]
  }
]
```

### Adding New Templates

To add a new template, drop a YAML file into `driver-packs/templates/` or the directory configured by `GRIDFLEET_DRIVER_PACK_TEMPLATES_DIR`, then restart the backend. The file must:

- Contain a `template_metadata:` top-level block with at minimum `id` and `display_name`.
- Validate as a driver pack manifest after the `template_metadata` block is stripped.
- Use a unique `template_id` (the `template_metadata.id` value) that does not collide with any existing template.

The backend loads templates at startup; there is no hot-reload mechanism. A restart is required after adding, removing, or modifying template files.

---

## Feature Actions

### What Features Are

Features are optional capability bundles declared in a pack manifest's `features:` block. Each feature has:

- A `display_name` and optional `description_md` / `help_url`.
- An `applies_when` block that filters by platform and device type.
- A `requirements` block listing host tools or privileges needed.
- An optional `sidecar` entry for long-running processes (see Sidecars section).
- An `actions` list of buttons exposed in the UI.

Features are **manifest-declared** and **adapter-driven**. Packs with `sidecar.adapter_hook` or `actions[].adapter_hook` fields must include an adapter wheel so the agent can handle the hooks.

When a pack with features is uploaded and enabled, the backend populates `DriverPackFeature` rows in the database from the manifest. These rows drive the UI panel and status tracking.

### Adapter Requirements

Feature actions require an uploaded pack whose adapter wheel implements:

```python
async def feature_action(
    self,
    *,
    feature_id: str,
    action_id: str,
    args: dict[str, Any],
    context: dict[str, Any],
) -> FeatureActionResult: ...
```

`FeatureActionResult` is defined in `agent/agent_app/pack/adapter_types.py`:

```python
class FeatureActionResult(TypedDict):
    ok: bool
    message: str
```

The action is POST-only and returns immediately. There is no long-poll or async job mechanism; the adapter is expected to complete synchronously.

### How Feature Action Buttons Appear

Feature action buttons appear in the **Host Detail → Drivers panel**, under each driver pack that is currently enabled on that host. The buttons are only shown when:

1. The pack has at least one feature with at least one action.
2. The feature's `applies_when` predicate matches at least one device on the host.

Each button label comes from the action's `label` field in the manifest. Clicking the button sends:

```
POST /api/hosts/{host_id}/driver-packs/{pack_id}/features/{feature_id}/actions/{action_id}
```

The backend resolves the host's agent URL, forwards the call to the agent, and returns the `FeatureActionResult`. On success, a toast is shown. On failure, an inline error message appears under the button.

### Invoking Feature Actions via API

```bash
curl -X POST \
  "http://localhost:8000/api/hosts/<host_id>/driver-packs/<pack_id>/features/<feature_id>/actions/<action_id>" \
  -H "Authorization: Basic <base64 machine credentials>" \
  -H "Content-Type: application/json" \
  -d '{"args": {}}'
```

Returns the `FeatureActionResult` from the adapter:

```json
{"ok": true, "message": "tunnel restarted"}
```

### Feature Status Tracking

The backend tracks per-host, per-feature status in the `host_pack_feature_status` table:

```sql
CREATE TABLE host_pack_feature_status (
  id UUID PRIMARY KEY,
  host_id UUID NOT NULL REFERENCES hosts(id) ON DELETE CASCADE,
  pack_id TEXT NOT NULL,
  feature_id TEXT NOT NULL,
  ok BOOLEAN NOT NULL,
  detail TEXT NOT NULL DEFAULT '',
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (host_id, pack_id, feature_id)
);
```

Status is updated after each feature action call and by the sidecar supervisor's periodic status polling. When the `ok` flag transitions:

| Transition | Webhook event |
|-----------|--------------|
| `ok` → `not ok` | `pack_feature.degraded` |
| `not ok` → `ok` | `pack_feature.recovered` |

Both events appear in the System Event stream (category `driver_pack`) and are delivered to any webhooks subscribed to those event kinds. The webhook payload carries `host_id`, `pack_id`, `feature_id`, `ok`, and `detail` fields.

---

## Sidecars

### What Sidecars Are

A sidecar is a long-running auxiliary process managed by the agent alongside the Appium node. The canonical example from the spec is a RemoteXPC tunnel process for iOS 18+ real devices — a tunnel that must be running continuously while Appium is active.

A sidecar is declared in a manifest feature under the `sidecar:` key:

```yaml
features:
  remotexpc_tunnel:
    display_name: "RemoteXPC tunnel"
    sidecar:
      adapter_hook: ensure_remotexpc_tunnel_registry
    actions:
      - id: restart
        label: "Restart tunnel"
        adapter_hook: restart_remotexpc_tunnel
```

Sidecars are **adapter-driven** and require an uploaded pack. The adapter must implement `sidecar_lifecycle`:

```python
async def sidecar_lifecycle(
    self,
    *,
    feature_id: str,
    action: Literal["start", "stop", "status"],
    context: dict[str, Any],
) -> SidecarStatus: ...
```

`SidecarStatus` is defined in `agent/agent_app/pack/adapter_types.py`:

```python
class SidecarStatus(TypedDict):
    ok: bool
    detail: str
```

### Sidecar Supervisor

Each agent process runs a single `SidecarSupervisor` instance (started in the agent lifespan). The supervisor:

- Maintains a `dict[(pack_id, release, feature_id)] -> SidecarHandle` mapping.
- On `start(pack_id, release, feature_id)`: calls the adapter `sidecar_lifecycle("start")` once. If successful, schedules a background polling task that calls `sidecar_lifecycle("status")` every 30 seconds.
- On `stop(...)`: calls `sidecar_lifecycle("stop")` and cancels the polling task.
- On status polling: if `SidecarStatus.ok` flips to `False`, updates `host_pack_feature_status` and emits `pack_feature.degraded`; if it flips back to `True`, emits `pack_feature.recovered`.

The supervisor's current snapshot is included in the agent's `/agent/driver-packs/status` response payload:

```json
{
  "runtime": [
    {
      "pack_id": "acme/my-driver",
      "release": "1.0.0",
      "installed": true,
      "sidecars": [
        {"feature_id": "remotexpc_tunnel", "ok": true, "detail": ""}
      ]
    }
  ]
}
```

### Scope Limits

- One sidecar at a time per `(pack_id, release, feature_id)` tuple — no fan-out, no sharing across packs.
- Sidecars are local to a single agent host; they are not cluster resources.
- If a sidecar's `ok` flips to `False`, the supervisor logs the transition and updates status, but does not automatically restart the sidecar. A restart must be triggered via the feature action UI button (or the `restart` action in the API).

---

## Export-to-Tarball

### What Export Does

The export endpoint produces a `.tar.gz` tarball from any pack:

- Packs with a stored artifact return that artifact as-is, preserving the original sha256.
- Packs without a stored artifact synthesise a fresh tarball on the fly containing a single `manifest.yaml` file serialised from the release's stored `manifest_json`.

Export is useful for:

- Moving a customised uploaded pack from one instance to another.
- Archiving a snapshot of a curated pack's manifest at a specific release.
- Distributing a customised pack to team members who manage their own GridFleet instances.

### Using Export via the UI

On the Drivers page, each pack row has an **Export tarball** button. Clicking it triggers a browser download of the `.tar.gz` file. The download filename is `<pack_id>-<release>.tar.gz` with slashes in `pack_id` replaced by underscores.

### Using Export via API

```bash
curl -X POST \
  "http://localhost:8000/api/driver-packs/{pack_id}/releases/{release}/export" \
  -H "Authorization: Basic <base64 machine credentials>" \
  --output my-driver-1.0.0.tar.gz
```

The response is `application/gzip` with an `X-Pack-Sha256` header containing the sha256 digest of the returned bytes.

To import the exported tarball on another instance, use the standard upload endpoint (see `docs/guides/driver-pack-tarball-upload.md`).

---

## Diverged-from-Upstream Badge

### What the Badge Means

When an operator forks a curated or uploaded pack, the resulting uploaded pack carries a `derived_from` field pointing to the upstream pack id and the release it was forked from.

If the upstream pack later ships a new release, the fork's `derived_from.release` will lag behind the current upstream release. GridFleet surfaces this as a **Diverged from upstream** badge on the pack row on the Drivers page.

### What the Badge Does Not Do

The badge is a visual cue only. It does not:

- Automatically merge upstream changes into your fork.
- Block you from using your fork.
- Delete or deprecate your fork.

To pick up upstream changes, review the new upstream release's manifest, update your pack source, and upload or fork a new release.

### When the Badge Appears

The badge appears on forked pack rows where:

1. `pack.derived_from` is set.
2. The upstream pack's current release in the catalog differs from `pack.derived_from.release`.

If there is no `derived_from` field, or if the upstream pack cannot be found in the catalog (for example, it was deleted), the badge does not appear.

---

## Troubleshooting

### Template creation fails with HTTP 400

The `from-template` endpoint validates the resulting manifest before persisting. The error body's `detail` field names the constraint. Common causes:

- `pack_id` contains characters not allowed in a driver pack id.
- A pack with the requested id and release already exists with different content.

### Feature action returns `{"ok": false, "message": "..."}`

This is an adapter-level response, not an HTTP error. The adapter ran but reported failure. Check the `detail` field and the host's system log for more context. The `host_pack_feature_status` row for this host/pack/feature will be updated with `ok=false` and the message, and a `pack_feature.degraded` webhook event will fire.

### Feature action returns HTTP 404

The backend could not find an active adapter for the pack on this host. Confirm that:

1. The pack is `enabled` and not in `draft` or `disabled` state.
2. The host has the pack in its installed set (visible on the Host Detail page under Drivers).
3. The agent is reachable from the backend (check the host's connectivity status on the Hosts page).

### Sidecar shows `ok: false` immediately after start

The adapter's `sidecar_lifecycle("start")` call completed, but the first `sidecar_lifecycle("status")` poll returned `ok: false`. This typically means the underlying process started but encountered an error within the first 30-second poll window. Check the host's agent logs for output from the sidecar process, and use the feature's `restart` action to attempt recovery.

### Export returns a tarball that fails to upload on the target instance

For synthesised tarballs exported and re-uploaded on another instance:

- The synthesised tarball contains only `manifest.yaml`; it has no adapter wheel. If you then attempt to use features backed by `adapter_hook` fields, those calls will fail because no adapter is present. Use the tarball upload path with a real adapter wheel for adapter-backed packs.
- Ensure the target instance does not already have a release with the same `pack_id` and `release` version (HTTP 409 conflict). Increment the release string before exporting if you need to distinguish versions.
