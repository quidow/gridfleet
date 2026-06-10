# Driver Pack Feature Actions, Sidecars, and Export Guide

This guide covers GridFleet driver pack feature actions, sidecar management, and export-to-tarball.

## Scope and Prerequisites

**Scope.** Feature actions and sidecars require an uploaded pack whose adapter wheel implements the relevant hooks. See `docs/guides/driver-pack-tarball-upload.md` for archive format and adapter security details.

**What you need before starting:**

- **Admin role** in GridFleet. All export and feature-action endpoints require admin credentials; non-admin requests are rejected with HTTP 403.
- Familiarity with driver pack manifests (schema in `backend/app/packs/manifest.py`).
- For feature actions and sidecars: an uploaded pack whose adapter wheel implements the `feature_action` and/or `sidecar_lifecycle` hooks.

---

## Feature Actions

### What Features Are

Features are optional capability bundles declared in a pack manifest's `features:` block. Each feature has:

- A `display_name`, a `description_md` that defaults to an empty string, and an optional (nullable) `help_url`.
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
    feature_id: str,
    action_id: str,
    args: dict[str, Any],
    ctx: LifecycleContext,
) -> FeatureActionResult: ...
```

`FeatureActionResult` is defined in `agent/agent_app/pack/adapter_types.py`:

```python
@dataclass
class FeatureActionResult:
    ok: bool
    detail: str = ""
    data: dict[str, Any] = field(default_factory=dict)
```

The action is POST-only and returns immediately. There is no long-poll or async job mechanism; the adapter is expected to complete synchronously.

### How Feature Action Buttons Appear

Feature action buttons appear in the **Host Detail → Drivers panel**, under each driver pack that is currently enabled on that host. The buttons are only shown when:

1. The pack has at least one feature with at least one action (features come from the catalog; the panel does not filter them by `applies_when`).
2. The host's status for the pack is `installed`.

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
{"ok": true, "detail": "tunnel restarted", "data": {}}
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

Status is updated after each feature action call and from the sidecar snapshot the agent POSTs to the backend's status-ingest endpoint. The backend's `PackStatusService.apply_status` calls `FeatureService.record_feature_status`, which upserts the row and, on a transition, queues the event. (The agent's sidecar supervisor only surfaces sidecar status in that snapshot; it has no DB access and emits no events.) When the `ok` flag transitions:

| Transition | Webhook event |
|-----------|--------------|
| `ok` → `not ok` | `pack_feature.degraded` |
| `not ok` → `ok` | `pack_feature.recovered` |

Both events appear in the System Event stream (category `operations_and_settings`) and are delivered to any webhooks subscribed to those event kinds. The webhook payload carries `host_id`, `pack_id`, `feature_id`, `ok`, and `detail` fields.

### Lifecycle action names are platform-specific

Action names in a manifest's `lifecycle_actions` (and the `recommended_action`
string an adapter's health result may carry) are opaque, platform-specific
identifiers. The backend matches them verbatim against the device's resolved
manifest (`platform_has_lifecycle_action`) and attaches no semantics to the
string itself — `"reconnect"` is an Android/adb idiom, not a core concept. A
pack that wants adapter-recommended repair must declare the action under the
exact name its adapter recommends. There is currently no canonical cross-pack
action taxonomy; if a second pack family needs shared repair semantics,
introduce one in the manifest schema rather than special-casing names in core.

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
    feature_id: str,
    action: Literal["start", "stop", "status"],
) -> SidecarStatus: ...
```

`SidecarStatus` is defined in `agent/agent_app/pack/adapter_types.py`:

```python
@dataclass
class SidecarStatus:
    ok: bool
    detail: str = ""
    state: str = ""
```

### Sidecar Supervisor

Each agent process runs a single `SidecarSupervisor` instance (started in the agent lifespan). The supervisor:

- Maintains a `dict[(pack_id, release, feature_id)] -> SidecarHandle` mapping.
- On `start(pack_id, release, feature_id)`: calls the adapter `sidecar_lifecycle("start")` once. If successful, schedules a background polling task that calls `sidecar_lifecycle("status")` every 30 seconds.
- On `stop(...)`: calls `sidecar_lifecycle("stop")` and cancels the polling task.
- On status polling: when a poll observes `SidecarStatus.ok == False` (or the poll raises), the supervisor records the bad state on its in-memory handle, logs it, and stops the poll loop. It has no DB access and emits no events; the not-ok state is surfaced in the status payload the agent POSTs to the backend, and the backend's status-ingest path (`apply_status` → `record_feature_status`) is what updates `host_pack_feature_status` and emits `pack_feature.degraded` / `pack_feature.recovered`. The supervisor never detects a flip back to `True` on its own — recovery is observed only after a restart re-runs `start()`.

The supervisor's current snapshot is included in the body the agent POSTs to the backend's `/agent/driver-packs/status` endpoint (a backend POST that returns `204`, not a GET the agent serves). The body has top-level keys `host_id`, `runtimes`, `packs`, `doctor`, and a flat `sidecars` list:

```json
{
  "host_id": "...",
  "runtimes": [],
  "packs": [],
  "doctor": [],
  "sidecars": [
    {
      "pack_id": "acme/my-driver",
      "release": "1.0.0",
      "feature_id": "remotexpc_tunnel",
      "ok": true,
      "detail": "",
      "state": "",
      "last_error": null
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

On a driver pack's detail page, the header has an **Export Tarball** button that downloads the pack's current release as a `.tar.gz` file. The browser download name is `<pack_id>-<release>.tar.gz`, where the browser only replaces the first `/` in `pack_id` with an underscore (the release is left as-is). The server's `Content-Disposition` filename is stricter: it replaces every character outside `[a-zA-Z0-9._-]` with an underscore in both `pack_id` and `release`.

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

## Troubleshooting

### Feature action returns `{"ok": false, "detail": "..."}`

This is an adapter-level response, not an HTTP error. The adapter ran but reported failure. Check the `detail` field and the host's system log for more context. The `host_pack_feature_status` row for this host/pack/feature will be updated with `ok=false` and the detail, and a `pack_feature.degraded` webhook event will fire.

### Feature action returns HTTP 404

The backend could not resolve the host, pack, release, or feature id in its database. Confirm that:

1. The host exists and the pack is `enabled` (not `draft` or `disabled`).
2. The pack has a current release that declares the feature id you are invoking.
3. The host has the pack in its installed set (visible on the Host Detail page under Drivers).

Note: a missing or unloaded adapter on the agent does **not** surface as 404. The backend treats any agent error or unreachable agent as a failed dispatch and returns HTTP 502, recording the feature as degraded.

### Sidecar shows `ok: false` immediately after start

The adapter's `sidecar_lifecycle("start")` call completed, but the first `sidecar_lifecycle("status")` poll returned `ok: false`. This typically means the underlying process started but encountered an error within the first 30-second poll window. Check the host's agent logs for output from the sidecar process, and use the feature's `restart` action to attempt recovery.

### Export returns a tarball that fails to upload on the target instance

For synthesised tarballs exported and re-uploaded on another instance:

- The synthesised tarball contains only `manifest.yaml`; it has no adapter wheel. If you then attempt to use features backed by `adapter_hook` fields, those calls will fail because no adapter is present. Use the tarball upload path with a real adapter wheel for adapter-backed packs.
- Ensure the target instance does not already have a release with the same `pack_id` and `release` version (HTTP 409 conflict). Increment the release string before exporting if you need to distinguish versions.
