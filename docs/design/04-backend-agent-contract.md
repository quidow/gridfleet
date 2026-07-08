# Doc 4: Backend ↔ Agent Contract

> HTTP contract between the FastAPI manager and the FastAPI host agents. Covers endpoint catalog, ack semantics, failure model, circuit breaker, auth surface, and idempotency.

GridFleet has two HTTP-speaking processes per host: the centralised backend and the per-host agent. Most traffic flows backend to agent. The agent also registers itself, pulls desired driver-pack and Appium-node state, reports pack status, and downloads pack tarballs. All host-aware backend calls use the `agent_operations` typed wrapper (`backend/app/agent_comm/operations.py`, imported as `from app.agent_comm import operations as agent_operations`).

This doc specifies that contract.

## Topology

```mermaid
flowchart LR
    classDef be fill:#fef9e7,stroke:#a37c00,color:#000
    classDef ag fill:#e7f0fe,stroke:#1c4ea3,color:#000
    classDef ext fill:#e9f7ec,stroke:#1f7a3a,color:#000

    backend["FastAPI backend<br/>multi-worker API plus scheduler"]:::be
    agent1["Host agent A<br/>FastAPI 5100"]:::ag
    agent2["Host agent B<br/>FastAPI 5100"]:::ag
    router["WebDriver router<br/>4444"]:::ext
    appium1["Appium<br/>4723 to 4823"]:::ext
    appium2["Appium<br/>4723 to 4823"]:::ext

    backend -->|"agent HTTP"| agent1
    backend -->|"agent HTTP"| agent2
    agent1 -->|"desired state, status, host registration"| backend
    agent2 -->|"desired state, status, host registration"| backend
    agent1 -.spawns.-> appium1
    agent2 -.spawns.-> appium2
    router -->|"allocate (internal grid API)"| backend
    router --> appium1
    router --> appium2
```

The CI runner / test client speaks **only** to the WebDriver router for sessions. The backend never proxies WebDriver traffic. For each new session the router calls the backend's internal grid API to allocate a device by capability match, then proxies the session's commands directly to that device's Appium server. Allocation and capability matching are owned by the backend; request forwarding is owned by the router.

## Auth surface

The two directions are asymmetric today.

- **Backend → agent.** Optional HTTP Basic auth is supported. The backend sends credentials from `GRIDFLEET_AGENT_AUTH_USERNAME` / `GRIDFLEET_AGENT_AUTH_PASSWORD` via `build_agent_basic_auth` in `backend/app/agent_comm/http_pool.py`, applied per request by the pool. The agent enforces Basic auth on every `/agent/*` HTTP route when `AGENT_API_AUTH_USERNAME` / `AGENT_API_AUTH_PASSWORD` are set, through `agent/agent_app/api_auth.py:BasicAuthMiddleware`. Leave all four unset for local dev or a trusted private lab network.
- **Agent → backend.** `agent/agent_app/lifespan.py` and `agent/agent_app/registration.py` construct `httpx.BasicAuth(manager_auth_username, manager_auth_password)` from `AGENT_MANAGER_AUTH_USERNAME` / `AGENT_MANAGER_AUTH_PASSWORD` when configured. The agent uses these credentials for desired-state polling, pack status, and host registration. This satisfies backend machine auth when `GRIDFLEET_AUTH_ENABLED=true`.
- **Browser → backend** (out of scope for this doc). Session cookie + CSRF for non-GET; that path never hits agents directly.

There is no HMAC or message signing. When the optional backend→agent Basic-auth credentials are unset, transport security relies entirely on the network boundary documented in `docs/guides/security.md`.

## Endpoint catalog (backend → agent)

All paths are under `http://<host_ip>:<host.agent_port>`. The wrapper module is `backend/app/agent_comm/operations.py` (imported as `agent_operations`).

| Method | Path | Caller (backend) | Purpose | Ack semantics |
| --- | --- | --- | --- | --- |
| GET | `/agent/health` | `host_sweep_loop` | liveness, version, missing prerequisites, and Appium convergence snapshot | 200 → ok; non-200 → `None` (treated as missed heartbeat) |
| GET | `/agent/host/telemetry` | `host_sweep` host-resource-telemetry stage | CPU/memory/disk numbers | 200 → snapshot; non-200 → `None` |
| GET | `/agent/pack/devices` | `host_sweep` connectivity stage, intake/discovery | currently-visible devices per pack | 2xx required (raises on non-2xx) |
| GET | `/agent/pack/devices/{ct}/properties` | `host_sweep` property-refresh stage | per-device props (OS version, model, etc.) | 200 → dict, 404 → `None`, other → raise |
| GET | `/agent/pack/devices/{ct}/health` | verification flow | adapter-driven health probe | 200 → dict, otherwise → raise |
| GET | `/agent/pack/devices/{ct}/telemetry` | `host_sweep` hardware-telemetry stage | adapter-driven hardware telemetry | 200 → dict, 404 → `None` |
| POST | `/agent/pack/devices/{ct}/lifecycle/{action}` | lifecycle/operator actions | run a pack-defined lifecycle action (e.g. boot, shutdown) | 2xx required |
| POST | `/agent/pack/devices/normalize` | intake/discovery | normalise raw input to canonical device fields | 200 → dict, 404 → `None` |
| POST | `/agent/pack/{pack_id}/doctor` | host onboarding/diagnostics (`hosts/router`, wrapper `pack_doctor`) | run pack adapter doctor checks | 2xx → checks list |
| POST | `/agent/pack/features/{feat}/actions/{act}` | feature dispatch | dispatch arbitrary pack feature action | 2xx required |
| POST | `/agent/appium/start` | `reconciler_agent` (`appium_start`, via the `start_node` service method) | spawn an Appium node | 2xx → `{pid, port, connection_target}` |
| POST | `/agent/appium/stop` | `reconciler_agent` (`appium_stop`, via the `stop_node` service method) and the orphan-reconcile `_stop` helper in `reconciler` | kill an Appium node | wrapper returns `httpx.Response`; consumers call `response.raise_for_status()`, so 2xx → success and transport/HTTP error raises |
| POST | `/agent/appium/{port}/reconfigure` | `reconfigure_delivery` (wrapper `agent_appium_reconfigure`) | toggle accepting-new-sessions / stop-pending / run scope | 2xx → `dict` |
| GET | `/agent/appium/{port}/status` | `node_health` reconcile path | "is the Appium on this port up?" | 200 → `{running: bool}`; non-200 → `None` |
| GET | `/agent/appium/{port}/logs` | host detail UI | return last N lines | 2xx required |
| POST | `/agent/appium-nodes/refresh` | reserved for the pull-mode backend path in phase 8b | wake `NodeStateLoop`; correctness still comes from polling | 202, including when pull mode is disabled |
| GET | `/agent/tools/status` | host onboarding | Node provider and host helper versions | 2xx required |

Most rows have a typed function in `agent_operations.py`. The function signature pins the response shape and the ack contract (`bool`, `bool | None`, `dict | None`, etc.). The one exception is the feature-dispatch endpoint (`/agent/pack/features/{feat}/actions/{act}`), which has no wrapper in `operations.py`: it is issued from `app/packs/services/feature_dispatch.py` via the shared `app.agent_comm.client.request`, so the circuit breaker and metrics still fire. Routers and services should never call `httpx` directly. Go through these wrappers (or that shared `request`) so the circuit breaker and metrics fire.

### `/agent/appium/start` cap surfaces

The Appium start payload sends two capability surfaces to the agent. They have separate sources of truth and separate consumers; keep them disentangled when extending the contract.

| Field             | Source of truth                                                                                                                                                             | Consumer                                                                                |
| ----------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------- |
| `extra_caps`      | `_build_session_aligned_start_caps(...)` in `app/appium_nodes/services/reconciler_agent.py`: full device dump (platform, os_version, manufacturer, model, ip, deviceName, sanitized `device_config.appium_caps`, tags, allocated caps) | Agent: merged into the Appium `/session` request body (`agent/agent_app/appium/process.py`) |
| `allocated_caps`  | `appium_node_resource_service.get_capabilities(...)` (UDID + reserved ports)                                                                                                | Agent → Appium driver                                                                   |

The start payload also carries `accepting_new_sessions`, `stop_pending`, and `grid_run_id`. The agent records these but does not route on them; new-session suppression and run scoping are enforced by the backend allocation service, not at the node.

**Backend-internal routing surface.** Capability matching now happens in the backend, not on the node: `device_match_surface` (`app/grid/allocation.py`, the pack platform's `platformName` scalar plus any identity/tag keys the manifest stereotype base declares, merged with `build_grid_stereotype_caps`'s deviceId + tag fanout) is the per-device routing surface the allocation service matches incoming session requests against when the router asks it to allocate a device. It carries only the keys the matcher consults. The rest of the pack stereotype is rendered only at node-start, never for matching. It is **not** part of the start payload and is never sent to the agent.

**Cross-component invariant.** Keep the routing stereotype and the driver caps disjoint. The backend MUST NOT include Appium-only device metadata (manufacturer, model, ip, deviceName, sanitized `device_config.appium_caps`) in the routing stereotype. That metadata MUST flow to the driver via `extra_caps` only.

## Endpoint catalog (agent → backend)

| Method | Path | Caller (agent) | Purpose | Ack semantics |
| --- | --- | --- | --- | --- |
| POST | `/api/hosts/register` | bootstrap | one-time host registration | 2xx, returns `Host` row id |
| GET | `/agent/driver-packs/desired` | `PackStateLoop` (~10 s) | desired pack list for this host | 200 → `{packs: [...]}` |
| POST | `/agent/driver-packs/status` | `PackStateLoop` after each tick | report runtime/adapter state | 204 |
| GET | `/agent/appium-nodes/desired` | `NodeStateLoop` (5 s when enabled) | desired Appium-node projection for this host | 200 → `{nodes: [...], generation_hint}` |
| GET | `/api/driver-packs/{pack_id}/releases/{release}/tarball` | `tarball_fetch` | download the sha256-pinned pack tarball | 2xx → tarball bytes |

The node desired response contains `device_id`, `generation`, `desired_state`, `port`, drain flags, `grid_run_id`, and transition-token fields. A running node also receives `launch`, the complete payload built by `build_node_launch_payload`. The push start path uses the same builder, and a contract test requires exact equality between both channels. If launch inputs are not runnable, the spec carries `launch: null` and `unrunnable_reason` instead of failing the whole host response.

`NodeStateLoop` is disabled by default in phase 8a. When enabled, it starts, stops, reconfigures, and reaps local orphan processes from the desired projection. An unexpired transition token forces one restart per agent process. The loop records applied generations and transition tokens in memory; `/agent/health` includes them on each running-node entry as `applied_generation` and `applied_transition_token`.

Phase 8a remains dual-channel: the backend push path still controls every host. Agents advertise `node_desired_pull: 1` only when `AGENT_NODE_PULL_ENABLED=true`, but the backend does not switch on that capability until phase 8b. Phase 8b will suppress push commands and use refresh pokes for pull-capable hosts. Phase 8c can remove the push and outbox code after the fleet upgrade is complete.

## Request envelope

Every backend→agent call goes through `request()` in `backend/app/agent_comm/client.py`:

```text
1. agent_circuit_breaker.before_request(host)   # may raise CircuitOpenError
2. attach REQUEST_ID_HEADER (correlation id)    # build_agent_headers
3. perform httpx call                           # GET or POST, with Basic auth when GRIDFLEET_AGENT_AUTH_* is set
4. classify result:
     status >= 500                  → record_failure (transport-like)
     transport exception            → record_failure (transport)
     anything else                  → record_success
5. record_agent_call metric (host, endpoint, outcome, duration)
```

The wrapper guarantees:

- `AgentUnreachableError` for transport failures (DNS, TCP, TLS, idle timeout).
- `AgentResponseError` for non-2xx responses when the wrapper calls `_raise_for_status`.
- `CircuitOpenError` for hosts in the open state: body includes `retry_after_seconds`.
- `httpx.HTTPStatusError` only escapes when a caller chose to inspect the response itself (e.g. `appium_start` to detect "already in use" details).

## Failure taxonomy

```mermaid
flowchart TD
    call["backend to agent call"] --> q1{"circuit open?"}
    q1 -- yes --> r1["CircuitOpenError"]
    q1 -- no --> q2{"transport ok?"}
    q2 -- no --> r2["AgentUnreachableError"]
    q2 -- yes --> q3{"HTTP status"}
    q3 -- 2xx --> ok["parse payload, caller inspects ack contract"]
    q3 -- 4xx --> r4["caller-specific conflict, pass-through, or AgentResponseError"]
    q3 -- 5xx --> r5["AgentResponseError"]
```

Loop callers map all three terminal errors to `None` (indeterminate). API mutators map them to user-visible 502/503 via the FastAPI exception handlers in `backend/app/core/errors.py`.

## Circuit breaker

`AgentCircuitBreaker` (`backend/app/agent_comm/circuit_breaker.py`).

- **Per host.** State is keyed by host IP/hostname. One bad host does not block others.
- **Failure threshold.** 5 consecutive failures → `open`. Cooldown is 30 s.
- **States.**
  - `closed`: pass through.
  - `open`: short-circuit with `CircuitOpenError(retry_after_seconds=...)`.
  - `half_open`: first probe is allowed through; concurrent probes get `retry_after_seconds=0`. Result decides next state.
- **Counted as failure.** Transport errors and HTTP `>= 500` from the response. 4xx is not a failure (the agent answered, just refused).
- **Events.** `host.circuit_breaker.opened` and `.closed` are published to the event bus when the state transitions, and surface on the dashboard.

This is what insulates the leader from "10 hosts unreachable" cascading into 14 loops × 10 hosts × 3 retries every cycle.

## Idempotency expectations

Per endpoint, a brief contract:

| Endpoint | Idempotent? | Notes |
| --- | --- | --- |
| `/agent/health` | yes | Read-only |
| `/agent/host/telemetry` | yes | Read-only |
| `/agent/pack/devices` (GET) | yes | Snapshot of currently-visible devices |
| `/agent/appium/start` | **no** | Caller must allocate a free port first (`candidate_ports`). Re-issuing with the same port and the agent already running on it → `NodePortConflictError`. Re-issuing with a fresh port → second running node. |
| `/agent/appium/stop` | yes | Stop on a port that has nothing returns 2xx. Safe to retry. |
| `/agent/appium/{port}/status` | yes | Read-only. |
| `/agent/appium/{port}/logs` | yes | Read-only |
| `/agent/driver-packs/desired` | yes | Read-only by host_id |
| `/agent/driver-packs/status` | yes | Replaces previous status; full snapshot |
| `/agent/appium-nodes/desired` | yes | Read-only projection by `host_id` |
| `/agent/appium-nodes/refresh` | yes | Wake hint; a lost request costs at most one poll interval |

The non-idempotent endpoint is `/agent/appium/start`. That is exactly where the split-brain rules from Doc 2 apply: a port is allocated, the agent is asked to start once, and the manager waits for the readiness probe before flipping DB state. If the agent times out mid-start the manager calls `/agent/appium/stop` to undo before raising. The pattern is "allocate, attempt, verify, persist, or rollback".

## Ack semantics for the lifecycle path

This is the most important part of the contract. Every state-changing call between manager and agent obeys a specific ack rule:

```mermaid
sequenceDiagram
    participant Mgr as Backend
    participant Br as Circuit breaker
    participant Ag as Agent
    Mgr->>Br: before_request
    alt circuit open
        Br-->>Mgr: retry_after
        Mgr-->>Mgr: raise CircuitOpenError ⇒ ack = None
    else
        Br-->>Mgr: pass
        Mgr->>Ag: HTTP request
        alt HTTP 2xx
            Ag-->>Mgr: payload
            Mgr-->>Mgr: parse payload → ack = True or definitive False (per endpoint)
        else HTTP non-2xx
            Ag-->>Mgr: non-2xx
            Mgr-->>Mgr: caller-specific projection: conflict/refused, AgentResponseError, or ack = None
        else transport error
            Mgr-->>Mgr: AgentUnreachableError ⇒ ack = None
        end
    end
```

The agent endpoint whose result is a tri-state probe (`/agent/appium/{port}/status`) projects HTTP shapes into `bool | None`:

- **`appium_status`** (`agent_operations.py`). 200 → `dict` (and the consumer reads `running: bool`). Non-200 → `None`. 

- **`appium_stop`**. `agent_operations.appium_stop` returns the raw response. The consumers bridge into the DB-flip rule by calling `response.raise_for_status()`: the `_stop` helper in `app/appium_nodes/services/reconciler.py` does this on the orphan-reconcile path before calling `mark_node_stopped` (defined in `reconciler_agent.py`), and the `stop_node` service method in `reconciler_agent.py` is the other `appium_stop` consumer. Success → proceed; `AgentCallError` or `httpx.HTTPError` → the stop did not take and the DB flip is skipped.

The agent does not expose a WebDriver session probe endpoint. Probe sessions are created by the backend directly against the device's Appium node (`probe_session_direct`, targeting `node_target(device)`), exercising the same Appium endpoint a router-proxied CI session lands on, minus the router hop.

When you add a new state-changing endpoint, follow this pattern: pick an explicit return type (`bool`, `bool | None`, or a dataclass) and document the projection from HTTP into that type at the wrapper layer. Do not let the lifecycle code do its own HTTP error handling; that is what `agent_operations.py` is for.

## Timeouts

Each wrapper picks a default. Override via the `timeout=` argument when the caller's loop has its own deadline:

| Endpoint | Default timeout | Reason |
| --- | --- | --- |
| `/agent/health` | 5 s | liveness ping |
| `/agent/appium/start` | `appium.startup_timeout_sec + 5` (~35 s), or `AVD_LAUNCH_HTTP_TIMEOUT_SECS = 190` for virtual devices | virtual devices boot is slow |
| `/agent/appium/stop` | 10 s | bounded shutdown |
| `/agent/appium/{port}/status` | 5 s | quick check |
| `/agent/appium/{port}/logs` | 10 s | small payload |
| `/agent/tools/status` | 15 s | local probe |
| `/agent/pack/devices` | 45 s | adapter discovery |
| `/agent/appium-nodes/desired` | 15 s | agent pull poll |

Timeouts are deliberately tight on health-path endpoints so a slow agent does not pin the leader's loops. They are deliberately loose on installer endpoints because operator-initiated install is allowed to take minutes.

## Request correlation

Every request carries a `REQUEST_ID_HEADER` (`X-Request-ID`) injected by `RequestContextMiddleware` on both backend and agent. Logs on both sides bind the request id, so operator-facing traces line up across backend + agent.

When a backend loop initiates a request with no inbound request id bound in structlog context, `build_agent_headers` does not synthesize one; the agent's `RequestContextMiddleware` generates one for the agent-side request and returns it on the response.

## Connection pooling

Backend → agent calls reuse `httpx.AsyncClient` instances pooled by `(host_ip, agent_port)` via `app.agent_comm.http_pool.AgentHttpPool`. A pooled client lives for the lifetime of the backend process; on lifespan shutdown the pool drains via `aclose()`.

The pool is opt-in via two guards: `agent.http_pool_enabled` (default `true`) **and** the caller using the default `httpx.AsyncClient` factory. Tests that inject a fake `http_client_factory` always go through the legacy per-call path. This is by design: the explicit-factory seam is used by unit tests and special-purpose call sites, and pooling must not surprise them.

`httpx.Limits(max_keepalive_connections=N, keepalive_expiry=S)` is read once at startup via `AgentHttpPool.configure_limits` and applied to every client the pool creates from then on. `agent.http_pool_max_keepalive` controls N (default 10); `agent.http_pool_idle_seconds` controls S in seconds (default 60). Retuning either setting requires a backend process restart; there is no runtime client-replacement path.

Auth is not part of the pool key because Basic auth is applied per request, not bound to the pooled `httpx.AsyncClient`. Credential changes are process-env changes; restart the backend process after changing `GRIDFLEET_AGENT_AUTH_*`.

Operational note: pooled clients do not refresh DNS until they are closed. If a host's IP changes mid-flight (lab reorg), restart the backend process. Toggling `agent.http_pool_enabled` off only routes new calls through the legacy path; existing pooled clients stay open and resume serving if the toggle is flipped back on. A process restart is the drain for both DNS/IP changes and pool tuning changes.

## Versioning

There is no formal API version on either side today. The backend records the agent's `version` from `/agent/health` on the `Host` row and computes `agent_version_status` against `agent.min_version` for operator visibility. The bootstrap installer and `/agent/health` `version_guidance` payload help keep agents within compatible ranges. Adding/changing an endpoint requires a coordinated release of backend + agent (`docs/reference/release-policy.md`).

`agent.min_version` is backend-enforced guidance for hosts that report to the current backend. It protects new backend expectations for old agents, but it cannot protect the opposite direction: an old backend calling an endpoint removed from a newer agent. Backend-called endpoint removals are safe only when the backend stops calling the endpoint before or at the same time the agent removes it. Roll those changes out backend-first, or deploy backend and agent together; do not roll newer agents across the fleet while an older backend still depends on the removed endpoint.

When evolving an endpoint:

- Adding a field to a request payload: agents must tolerate unknown fields (FastAPI/Pydantic does by default unless `model_config = {extra: 'forbid'}`).
- Adding a field to a response: backend wrappers must tolerate missing fields (use `payload.get(...)`).
- Renaming or removing: needs a breaking component release in `release-please` and the coordinated rollout model above. Don't.

## Structured error codes

The Appium lifecycle endpoints return structured failure detail as `{"code": "<ENUM_VALUE>", "message": "<human text>"}`. Other agent endpoints may still use ordinary FastAPI `detail` strings or endpoint-specific payloads. The Appium error enum is mirrored on both sides:

- `agent/agent_app/error_codes.py:AgentErrorCode`
- `backend/app/agent_comm/error_codes.py:AgentErrorCode`

`backend/tests/contracts/test_agent_error_code_parity.py` enforces drift detection. Backend matches `code` via `agent_operations.parse_agent_error_detail`; substring matching on `detail.message` is forbidden.

The exception classes below are defined in `agent_app/appium/exceptions.py`; the exception → `AgentErrorCode` / HTTP-status mapping is done in `agent_app/appium/router.py` (and `agent_app/pack/router.py` for the pack-resolution codes). `appium/process.py` raises most of these classes but is not where they are defined.

| Code | Source | Meaning |
| --- | --- | --- |
| `PORT_OCCUPIED` | `appium.exceptions.PortOccupiedError` | External listener already bound the requested port |
| `ALREADY_RUNNING` | `appium.exceptions.AlreadyRunningError` | Managed Appium already running on this port |
| `STARTUP_TIMEOUT` | `appium.exceptions.StartupTimeoutError` | Appium did not become ready in `appium.startup_timeout_sec` |
| `RUNTIME_MISSING` | `appium.exceptions.RuntimeMissingError` / `RuntimeNotInstalledError` | Required runtime tools are absent |
| `DEVICE_NOT_FOUND` | `appium.exceptions.DeviceNotFoundError` | Connection target not visible to the host adapter |
| `INVALID_PAYLOAD` | `appium.exceptions.InvalidStartPayloadError` | Start request missing required fields |
| `NO_ADAPTER` | `pack.dependencies` / `pack.router` | No pack adapter available to serve the request |
| `UNKNOWN_PLATFORM` | `pack.dependencies` | Requested pack/platform is not in the host's desired list |
| `INTERNAL_ERROR` | route catch-all | Agent-side state corruption or unclassified adapter failure |

## Known gaps

- Phase 8a does not select pull mode on the backend. Running with `AGENT_NODE_PULL_ENABLED=true` is an overlap smoke test only until phase 8b adds per-host mode selection and push suppression.
- Port-conflict reporting and backend repinning remain phase 8b work.
- Wrappers do not retry requests. Loops own retry and backoff so a degraded agent cannot amplify traffic across layers.

## What this doc does NOT cover

- Internal node state machine: see Doc 2.
- Loop cadence and reconciliation pattern: see Doc 3.
- Owner allocations, port pools, WebDriver sessions: see Doc 5.
- Operator-facing onboarding flows: see `docs/guides/host-onboarding.md`.
